#!/usr/bin/env python3
"""qc.py — quick read-only metadata census of whole drives into SQLite.

What it records: names, folder structure (parent/child tree), sizes, modified +
created timestamps, attributes, extensions — everything directory enumeration
provides. It NEVER opens a file: no content reads, no hashing, no per-file
handles (os.scandir stat data comes from the directory listing itself).

Write access: none to the scanned drives. The only thing written is the catalog
database, and the tool refuses to place it on a drive being scanned unless you
pass --allow-db-on-scanned.

Usage:
  qc.py                      GUI popup to pick drives, then scan
  qc.py C: E:                scan these drives (no popup)
  qc.py --list               show detected drives and exit
  qc.py E: --db PATH.sqlite  put the catalog somewhere specific

Windows-only. Python 3.11+ standard library only (on 3.12+ drive discovery uses
os.listdrives; older versions fall back to a Win32 call).
"""
from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from ctypes import wintypes
from datetime import datetime, timezone

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS scan(
  scan_id      INTEGER PRIMARY KEY,
  drive        TEXT NOT NULL,
  label        TEXT,
  fs_type     TEXT,
  serial_hex   TEXT,               -- volume serial: format-time, travels with the drive
  volume_guid  TEXT,               -- Volume GUID path: per-machine stable across letter changes
  hw_product   TEXT,               -- physical device model (zero-access IOCTL)
  hw_serial    TEXT,               -- physical device serial: survives reformat
  disk_total   INTEGER,
  disk_free    INTEGER,
  started_utc  TEXT NOT NULL,
  finished_utc TEXT,
  dir_count    INTEGER,
  file_count   INTEGER,
  byte_total   INTEGER,
  error_count  INTEGER,
  status       TEXT NOT NULL DEFAULT 'running'   -- running | done | aborted | failed
);
CREATE TABLE IF NOT EXISTS entry(
  entry_id    INTEGER PRIMARY KEY,
  scan_id     INTEGER NOT NULL REFERENCES scan(scan_id),
  parent_id   INTEGER REFERENCES entry(entry_id),   -- NULL only for the drive root row
  name        TEXT NOT NULL,
  is_dir      INTEGER NOT NULL,
  size_bytes  INTEGER,            -- NULL for directories
  mtime_ns    INTEGER,            -- Unix epoch nanoseconds
  birth_ns    INTEGER,            -- creation time, Unix epoch nanoseconds
  attr        INTEGER,            -- FILE_ATTRIBUTE_* bits
  reparse_tag INTEGER,            -- set for junctions/symlinks; subtree NOT descended
  ext         TEXT,               -- lowercase, files only
  depth       INTEGER NOT NULL    -- 0 = the drive root row
);
CREATE INDEX IF NOT EXISTS ix_entry_scan_parent ON entry(scan_id, parent_id);
CREATE INDEX IF NOT EXISTS ix_entry_scan_ext    ON entry(scan_id, ext) WHERE is_dir = 0;
CREATE INDEX IF NOT EXISTS ix_entry_scan_size   ON entry(scan_id, size_bytes) WHERE is_dir = 0;
CREATE TABLE IF NOT EXISTS scan_error(
  scan_id  INTEGER NOT NULL REFERENCES scan(scan_id),
  path     TEXT NOT NULL,
  error    TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS v_paths AS
WITH RECURSIVE p(entry_id, scan_id, path, is_dir, size_bytes, depth) AS (
  SELECT entry_id, scan_id, name, is_dir, size_bytes, depth
    FROM entry WHERE parent_id IS NULL
  UNION ALL
  SELECT e.entry_id, e.scan_id, p.path || '\\' || e.name, e.is_dir, e.size_bytes, e.depth
    FROM entry e JOIN p ON e.parent_id = p.entry_id
)
SELECT * FROM p;
"""

# --- read-only volume queries (no file handles involved) ---
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
]
_k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_k32.GetDriveTypeW.restype = wintypes.UINT
_k32.GetLogicalDrives.argtypes = []
_k32.GetLogicalDrives.restype = wintypes.DWORD
_k32.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                 wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                 wintypes.LPVOID]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_INVALID_HANDLE = wintypes.HANDLE(-1).value
_IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400

# OneDrive/cloud placeholder reparse tags: 0x9000001A plus per-provider variants that
# differ only in one nibble — mask it out to match the whole family.
def is_cloud_tag(tag: int | None) -> bool:
    return tag is not None and (tag & 0xFFFF0FFF) == 0x9000001A


def volume_guid_of(root: str) -> str | None:
    """\\\\?\\Volume{...}\\ for a root like 'E:\\'. Unelevated; stable per machine
    across drive-letter changes (but assigned per Windows install)."""
    buf = ctypes.create_unicode_buffer(64)
    if _k32.GetVolumeNameForVolumeMountPointW(root, buf, len(buf)):
        return buf.value or None
    return None


def hardware_identity_of(drive: str) -> tuple[str | None, str | None]:
    """(product, serial) of the physical device behind a drive, via
    IOCTL_STORAGE_QUERY_PROPERTY on a ZERO-ACCESS volume handle — the zero access
    request is what makes this work without elevation. Survives reformatting; USB
    bridges sometimes report the enclosure or nothing."""
    h = _k32.CreateFileW("\\\\.\\" + drive, 0, 0x3, None, 3, 0, None)  # access 0, share RW, OPEN_EXISTING
    if h == _INVALID_HANDLE:
        return None, None
    try:
        query = (ctypes.c_ubyte * 12)()  # STORAGE_PROPERTY_QUERY: StorageDeviceProperty, PropertyStandardQuery
        out = (ctypes.c_ubyte * 1024)()
        returned = wintypes.DWORD()
        ok = _k32.DeviceIoControl(h, _IOCTL_STORAGE_QUERY_PROPERTY, query, len(query),
                                  out, len(out), ctypes.byref(returned), None)
        if not ok or returned.value < 36:
            return None, None
        buf = bytes(out[:returned.value])

        def read_str(off_pos: int) -> str | None:
            off = int.from_bytes(buf[off_pos:off_pos + 4], "little")
            if not 0 < off < len(buf):
                return None
            end = buf.find(b"\0", off)
            s = buf[off:end if end != -1 else len(buf)].decode("ascii", "replace").strip()
            return s or None

        vendor = read_str(12)
        product = read_str(16)
        serial = read_str(24)
        full_product = " ".join(p for p in (vendor, product) if p) or None
        return full_product, serial
    finally:
        _k32.CloseHandle(h)
DRIVE_TYPE_NAMES = {0: "unknown", 1: "invalid", 2: "removable", 3: "fixed",
                    4: "remote", 5: "cdrom", 6: "ramdisk"}


def volume_info(root: str):
    label = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    if _k32.GetVolumeInformationW(root, label, len(label), ctypes.byref(serial),
                                  None, None, fs, len(fs)):
        return label.value or None, fs.value or None, f"{serial.value:08x}"
    return None, None, None


def human(n) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TiB"


def _list_drive_roots() -> list[str]:
    """os.listdrives() needs Python 3.12+; older Pythons fall back to the
    GetLogicalDrives bitmask (bit 0 = A:, bit 1 = B:, …)."""
    try:
        return os.listdrives()
    except AttributeError:
        mask = _k32.GetLogicalDrives()
        return [f"{chr(65 + i)}:\\" for i in range(26) if mask & (1 << i)]


def detect_drives() -> list[dict]:
    out = []
    for root in _list_drive_roots():
        drive = root.rstrip("\\")
        dtype = DRIVE_TYPE_NAMES.get(_k32.GetDriveTypeW(root), "unknown")
        label = fs = serial = None
        total = free = None
        if dtype not in ("invalid", "unknown"):
            label, fs, serial = volume_info(root)
            try:
                u = shutil.disk_usage(root)
                total, free = u.total, u.free
            except OSError:
                pass
        out.append({"drive": drive, "type": dtype, "label": label, "fs": fs,
                    "serial": serial, "total": total, "free": free})
    return out


DEFAULT_SORT = "size:desc,type:asc,name:asc"
SORT_KEYS = ("size", "type", "name")


def parse_sort(spec: str) -> list[tuple[str, bool]]:
    """'size:desc,type:asc' -> [('size', True), ('type', False)]; True = descending.
    Unknown keys/directions fail loudly; duplicate keys keep the first; empty -> default."""
    out: list[tuple[str, bool]] = []
    seen = set()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        key, _, direction = part.partition(":")
        direction = direction or "asc"
        if key not in SORT_KEYS or direction not in ("asc", "desc"):
            raise ValueError(f"bad sort component {part!r} (keys: size|type|name, dirs: asc|desc)")
        if key in seen:
            continue
        seen.add(key)
        out.append((key, direction == "desc"))
    return out or parse_sort(DEFAULT_SORT)


def pick_drives_gui(drives: list[dict], explicit_db: str | None, auto_dir: str,
                    default_sort: str = DEFAULT_SORT) -> tuple[list[str], str, bool, str]:
    """Returns (picked_drives, db_path, allow_db_on_scanned, sort_spec_string).
    Empty drive list = cancelled.

    When no --db was given, the catalog field auto-suggests
    census_<letters>_drive_<YYYYMMDDHHMM>.sqlite and keeps updating as drives are
    ticked — until the user edits the field, which pins their custom value.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except ImportError:
        print("tkinter unavailable — pass drive letters on the command line (see --help)",
              file=sys.stderr)
        sys.exit(2)
    root = tk.Tk()
    root.title("quick census — select drives (read-only scan)")
    root.attributes("-topmost", True)
    tk.Label(root, text="Metadata-only census. Nothing on the selected drives is written or opened.",
             anchor="w", padx=12, pady=8).pack(fill="x")
    vars_by_drive: dict[str, tk.BooleanVar] = {}

    db_var = tk.StringVar()
    last_auto = [""]  # sentinel: field still tracks our suggestions while it equals this
    auto_dir_ref = [auto_dir]  # cloud opt-in may retarget suggestions to a private dir

    def tracking() -> bool:
        return explicit_db is None and db_var.get().strip() in ("", last_auto[0])

    def refresh_suggestion(*_):
        if not tracking():
            return
        sel = [k for k, v in vars_by_drive.items() if v.get()]
        suggestion = os.path.join(auto_dir_ref[0], suggest_db_name(sel))
        last_auto[0] = suggestion
        db_var.set(suggestion)

    for d in drives:
        text = (f"{d['drive']}  {d['label'] or '(no label)'}   {d['fs'] or '?'}   "
                f"{human(d['total'])} total, {human(d['free'])} free   [{d['type']}]")
        v = tk.BooleanVar(value=False)
        state = "normal" if d["type"] in ("fixed", "removable", "ramdisk") else "disabled"
        tk.Checkbutton(root, text=text, variable=v, anchor="w", padx=16, state=state,
                       command=refresh_suggestion).pack(fill="x")
        vars_by_drive[d["drive"]] = v

    # catalog destination: editable path (custom name welcome) + file dialog
    dest = tk.Frame(root)
    dest.pack(fill="x", padx=12, pady=(10, 0))
    tk.Label(dest, text="Catalog file:").pack(side="left")
    db_var.set(explicit_db if explicit_db is not None else os.path.join(auto_dir, suggest_db_name([])))
    if explicit_db is None:
        last_auto[0] = db_var.get()
    tk.Entry(dest, textvariable=db_var, width=64).pack(side="left", fill="x", expand=True, padx=6)

    def browse():
        cur = db_var.get().strip() or os.path.join(auto_dir_ref[0], suggest_db_name([]))
        chosen = filedialog.asksaveasfilename(
            parent=root, title="Save catalog as",
            initialdir=os.path.dirname(os.path.abspath(cur)) or ".",
            initialfile=os.path.basename(cur) or "census.sqlite",
            defaultextension=".sqlite",
            filetypes=[("SQLite catalog", "*.sqlite"), ("All files", "*.*")],
            confirmoverwrite=False)  # appending to an existing catalog is normal
        if chosen:
            db_var.set(chosen)  # a browsed choice counts as the user's own value

    tk.Button(dest, text="Browse…", command=browse).pack(side="left")

    # summary-table sort: three ordered slots, each a key + direction (— disables a slot)
    from tkinter import ttk
    sortf = tk.Frame(root)
    sortf.pack(fill="x", padx=12, pady=(8, 0))
    tk.Label(sortf, text="Summary sort:").pack(side="left")
    seeded = parse_sort(default_sort)
    slot_vars: list[tuple[tk.StringVar, tk.StringVar]] = []
    for i in range(3):
        key = seeded[i][0] if i < len(seeded) else "—"
        direction = ("desc" if seeded[i][1] else "asc") if i < len(seeded) else "asc"
        tk.Label(sortf, text=f"  {i + 1}.").pack(side="left")
        kv = tk.StringVar(value=key)
        dv = tk.StringVar(value=direction)
        ttk.Combobox(sortf, textvariable=kv, values=["size", "type", "name", "—"],
                     width=5, state="readonly").pack(side="left", padx=(2, 0))
        ttk.Combobox(sortf, textvariable=dv, values=["desc", "asc"],
                     width=5, state="readonly").pack(side="left", padx=(2, 0))
        slot_vars.append((kv, dv))

    allow_var = tk.BooleanVar(value=False)
    tk.Checkbutton(root, text="Allow the catalog to live on a scanned drive "
                              "(the catalog file itself is excluded from the census)",
                   variable=allow_var, anchor="w", padx=16).pack(fill="x", pady=(2, 0))

    cloud_var = tk.BooleanVar(value=False)
    cloud_hint = tk.Label(root, text="", anchor="w", padx=32, fg="#9a6700")

    def cloud_toggled():
        if cloud_var.get():
            cloud_hint.config(
                text="tip: catalogs listing cloud folders are sensitive too — a private location like "
                     "%LOCALAPPDATA%\\quickcensus keeps the catalog itself out of sync roots.")
            new_dir = os.path.join(os.environ.get("LOCALAPPDATA", auto_dir), "quickcensus")
        else:
            cloud_hint.config(text="")
            new_dir = auto_dir
        if tracking():  # only steer the suggestion while the user hasn't taken over
            auto_dir_ref[0] = new_dir
            sel = [k for k, v in vars_by_drive.items() if v.get()]
            suggestion = os.path.join(new_dir, suggest_db_name(sel))
            last_auto[0] = suggestion
            db_var.set(suggestion)
        else:
            auto_dir_ref[0] = new_dir

    tk.Checkbutton(root, text="List cloud placeholder subtrees (OneDrive online-only folders) — "
                              "metadata-only, never downloads file content",
                   variable=cloud_var, anchor="w", padx=16,
                   command=cloud_toggled).pack(fill="x", pady=(2, 0))
    cloud_hint.pack(fill="x")

    picked: list[str] = []
    result = {"db": "", "allow": False, "sort": default_sort, "cloud": False}

    def go():
        sel = [k for k, v in vars_by_drive.items() if v.get()]
        if not sel:
            messagebox.showwarning("quick census", "No drives selected.", parent=root)
            return
        dbp = os.path.abspath(db_var.get().strip() or os.path.join(auto_dir_ref[0], suggest_db_name(sel)))
        if os.path.splitdrive(dbp)[0].upper() in {d.upper() for d in sel} and not allow_var.get():
            messagebox.showwarning(
                "quick census",
                f"The catalog file\n\n{dbp}\n\nis on a drive you are about to scan.\n"
                "Pick another location, or tick the allow checkbox to accept that one "
                "file being written there.", parent=root)
            return
        picked.extend(sel)
        result["db"] = dbp
        result["allow"] = allow_var.get()
        result["cloud"] = cloud_var.get()
        result["sort"] = ",".join(f"{kv.get()}:{dv.get()}" for kv, dv in slot_vars
                                  if kv.get() != "—") or DEFAULT_SORT
        root.destroy()

    row = tk.Frame(root)
    row.pack(pady=10)
    tk.Button(row, text="Scan", width=12, command=go).pack(side="left", padx=6)
    tk.Button(row, text="Cancel", width=12, command=root.destroy).pack(side="left", padx=6)
    root.mainloop()
    return picked, result["db"], result["allow"], result["sort"], result["cloud"]


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def suggest_db_name(drives: list[str]) -> str:
    """census_E_drive_202608061920.sqlite — letters of the selected drives + local time."""
    ts = datetime.now().strftime("%Y%m%d%H%M")
    letters = "_".join(d[0].upper() for d in drives)
    return f"census_{letters + '_' if letters else ''}drive_{ts}.sqlite"


def open_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    # migrate catalogs created before the identity columns existed
    cols = {r[1] for r in con.execute("PRAGMA table_info(scan)")}
    for col in ("volume_guid", "hw_product", "hw_serial"):
        if col not in cols:
            con.execute(f"ALTER TABLE scan ADD COLUMN {col} TEXT")
    con.execute("PRAGMA synchronous = NORMAL")
    return con


def scan_drive(con: sqlite3.Connection, drive: str, skip_paths_cf: set[str],
               include_cloud: bool = False) -> int:
    root = drive + "\\"
    label, fs, serial = volume_info(root)
    guid = volume_guid_of(root)
    hw_product, hw_serial = hardware_identity_of(drive)
    try:
        usage = shutil.disk_usage(root)
        disk_total, disk_free = usage.total, usage.free
    except OSError:
        disk_total = disk_free = None
    cur = con.execute(
        "INSERT INTO scan(drive, label, fs_type, serial_hex, volume_guid, hw_product, hw_serial, "
        "disk_total, disk_free, started_utc) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (drive, label, fs, serial, guid, hw_product, hw_serial, disk_total, disk_free, iso_now()))
    scan_id = cur.lastrowid
    con.commit()

    next_id = (con.execute("SELECT COALESCE(MAX(entry_id), 0) FROM entry").fetchone()[0]) + 1
    batch: list[tuple] = []
    err_batch: list[tuple] = []
    n_dirs = n_files = n_bytes = n_errs = 0
    t0 = time.monotonic()
    last_progress = 0.0

    def flush():
        nonlocal batch, err_batch
        if batch:
            con.executemany(
                "INSERT INTO entry(entry_id, scan_id, parent_id, name, is_dir, size_bytes, "
                "mtime_ns, birth_ns, attr, reparse_tag, ext, depth) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            batch = []
        if err_batch:
            con.executemany("INSERT INTO scan_error(scan_id, path, error) VALUES(?,?,?)", err_batch)
            err_batch = []
        con.commit()

    def progress(current: str):
        nonlocal last_progress
        now = time.monotonic()
        if now - last_progress < 0.25:
            return
        last_progress = now
        shown = current if len(current) <= 58 else "…" + current[-57:]
        print(f"\r{drive} {n_files:,} files, {n_dirs:,} dirs, {human(n_bytes)}"
              f" | {shown:<58}", end="", flush=True)

    root_id = next_id
    next_id += 1
    batch.append((root_id, scan_id, None, drive, 1, None, None, None,
                  FILE_ATTRIBUTE_DIRECTORY, None, None, 0))
    ext_root = "\\\\?\\" + root
    stack: list[tuple[str, int, int]] = [(ext_root, root_id, 1)]
    status = "done"
    try:
        while stack:
            dpath, parent_id, depth = stack.pop()
            try:
                it = os.scandir(dpath)
            except OSError as e:
                err_batch.append((scan_id, dpath[4:], f"{type(e).__name__}: {e.strerror or e}"))
                n_errs += 1
                continue
            with it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError as e:
                        err_batch.append((scan_id, entry.path[4:],
                                          f"{type(e).__name__}: {e.strerror or e}"))
                        n_errs += 1
                        continue
                    attr = st.st_file_attributes
                    is_reparse = bool(attr & FILE_ATTRIBUTE_REPARSE_POINT)
                    is_dir = bool(attr & FILE_ATTRIBUTE_DIRECTORY)
                    tag = st.st_reparse_tag if is_reparse else None
                    birth = getattr(st, "st_birthtime_ns", None) or st.st_ctime_ns
                    eid = next_id
                    next_id += 1
                    if is_dir:
                        batch.append((eid, scan_id, parent_id, entry.name, 1, None,
                                      st.st_mtime_ns, birth, attr, tag, None, depth))
                        n_dirs += 1
                        # junctions/symlinks are never descended (cycle guard). Cloud
                        # placeholder dirs (OneDrive online-only) are descended only on
                        # explicit opt-in: enumeration is metadata-only and hydrates no
                        # content, but it may cause the sync client to materialize child
                        # placeholder stubs (a metadata network event).
                        if not is_reparse or (include_cloud and is_cloud_tag(tag)):
                            stack.append((entry.path, eid, depth + 1))
                    else:
                        if entry.path[4:].casefold() in skip_paths_cf:
                            next_id -= 1
                            continue  # the live catalog db itself
                        ext = os.path.splitext(entry.name)[1][1:].lower() or None
                        batch.append((eid, scan_id, parent_id, entry.name, 0, st.st_size,
                                      st.st_mtime_ns, birth, attr, tag, ext, depth))
                        n_files += 1
                        n_bytes += st.st_size
                    if len(batch) >= 5000:
                        flush()
            progress(dpath[4:])
    except KeyboardInterrupt:
        status = "aborted"
    flush()
    con.execute(
        "UPDATE scan SET finished_utc=?, dir_count=?, file_count=?, byte_total=?, "
        "error_count=?, status=? WHERE scan_id=?",
        (iso_now(), n_dirs, n_files, n_bytes, n_errs, status, scan_id))
    con.commit()
    elapsed = time.monotonic() - t0
    print(f"\r{drive} scan {scan_id}: {n_files:,} files, {n_dirs:,} dirs, {human(n_bytes)}, "
          f"{n_errs} errors, {elapsed:.1f}s ({status}){' ' * 40}")
    top = con.execute(
        "SELECT COALESCE(ext,'(none)') e, COUNT(*) n, SUM(size_bytes) b FROM entry "
        "WHERE scan_id=? AND is_dir=0 GROUP BY ext ORDER BY b DESC LIMIT 10", (scan_id,)).fetchall()
    for e, n, b in top:
        print(f"    {e:<12} {n:>8,} files  {human(b):>10}")
    if status == "aborted":
        raise KeyboardInterrupt
    return scan_id


def _fmt_date(ns: int | None) -> str:
    if not ns:
        return "?"
    return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d")


def summarize_scan(con: sqlite3.Connection, scan_id: int) -> list[dict]:
    """Roll up the scan's tree: per first-level entry, recursive size / file types /
    subfolder counts by level. One in-memory pass; no recursion."""
    rows = con.execute(
        "SELECT entry_id, parent_id, name, is_dir, size_bytes, mtime_ns, ext, depth, reparse_tag "
        "FROM entry WHERE scan_id=?", (scan_id,)).fetchall()
    children: dict[int, list] = defaultdict(list)
    root_id = None
    for r in rows:
        if r["parent_id"] is None:
            root_id = r["entry_id"]
        else:
            children[r["parent_id"]].append(r)

    firsts = []
    for r in sorted(children.get(root_id, []), key=lambda x: x["name"].casefold()):
        item = {
            "name": r["name"], "mtime_ns": r["mtime_ns"], "reparse": r["reparse_tag"],
            "is_dir": bool(r["is_dir"]), "ext": r["ext"],
            "size": r["size_bytes"] or 0, "files": 0 if r["is_dir"] else 1,
            "types": Counter(), "levels": Counter(),
        }
        if r["is_dir"] and not r["reparse_tag"]:
            stack = [r["entry_id"]]
            while stack:
                for c in children.get(stack.pop(), ()):
                    if c["is_dir"]:
                        item["levels"][c["depth"]] += 1  # depth 2 == "lv2" under a first-level folder
                        if not c["reparse_tag"]:
                            stack.append(c["entry_id"])
                    else:
                        item["files"] += 1
                        item["size"] += c["size_bytes"] or 0
                        item["types"][c["ext"] or "(none)"] += 1
        firsts.append(item)

    return firsts


def write_summary(con: sqlite3.Connection, scan_ids: list[int], txt_path: str, top_n: int,
                  sort_spec: list[tuple[str, bool]]):
    out = []
    w = out.append
    w("QUICK CENSUS SUMMARY")
    w(f"generated {datetime.now().strftime('%Y-%m-%d %H:%M')} by qc.py — metadata only, no file was opened")
    for scan_id in scan_ids:
        s = con.execute("SELECT * FROM scan WHERE scan_id=?", (scan_id,)).fetchone()
        elapsed = ""
        if s["finished_utc"]:
            dt = (datetime.fromisoformat(s["finished_utc"])
                  - datetime.fromisoformat(s["started_utc"])).total_seconds()
            elapsed = f", {dt:.1f}s"
        w("")
        w("=" * 100)
        w(f"SCAN {scan_id}  —  {s['drive']}  {s['label'] or '(no label)'}  [{s['fs_type'] or '?'}]"
          f"  serial {s['serial_hex'] or '?'}")
        w("=" * 100)
        w("")
        w("1. OVERALL")
        w(f"   {s['file_count']:,} files, {s['dir_count']:,} folders, {human(s['byte_total'])}"
          f" cataloged, {s['error_count']} unreadable path(s){elapsed}, status {s['status']}")
        w(f"   disk: {human(s['disk_total'])} total, {human(s['disk_free'])} free"
          f"   scanned {s['started_utc']}")
        hw = " ".join(p for p in (s["hw_product"], s["hw_serial"] and f"SN {s['hw_serial']}") if p)
        w(f"   identity: volume serial {s['serial_hex'] or '—'} (travels with the drive)"
          f" · volume GUID {s['volume_guid'] or '—'} (this machine)"
          f" · hardware {hw or '—'} (survives reformat)")
        w("")
        w(f"   {'type':<10} {'files':>9}  {'bytes':>10}")
        for r in con.execute(
                "SELECT COALESCE(ext,'(none)') e, COUNT(*) n, SUM(size_bytes) b FROM entry "
                "WHERE scan_id=? AND is_dir=0 GROUP BY ext ORDER BY b DESC LIMIT 10", (scan_id,)):
            w(f"   {r['e']:<10} {r['n']:>9,}  {human(r['b']):>10}")
        errs = con.execute("SELECT path, error FROM scan_error WHERE scan_id=? LIMIT 5", (scan_id,)).fetchall()
        if errs:
            w("")
            w("   unreadable:")
            for r in errs:
                w(f"     {r['path']}  ({r['error']})")

        firsts = summarize_scan(con, scan_id)
        total = len(firsts)
        # the cap always keeps the LARGEST roots (its whole purpose); the sort orders the display
        firsts.sort(key=lambda x: -x["size"])
        shown = firsts[:top_n]
        omitted = total - len(shown)

        rows = []
        for it in shown:
            if it["reparse"]:
                typ = "junction" if it["reparse"] == 0xA0000003 else "symlink"
            elif it["is_dir"]:
                typ = "folder"
            else:
                typ = it["ext"] or "file"
            if it["is_dir"] and not it["reparse"]:
                top_types = "  ".join(f"{e}×{n}" for e, n in it["types"].most_common(4))
                more = len(it["types"]) - 4
                if more > 0:
                    top_types += f"  +{more} more"
                top_types = top_types or "(empty)"
                levels = " ".join(f"lv{d}:{n}" for d, n in sorted(it["levels"].items())[:5]) or "-"
            else:
                top_types = "-"
                levels = "-"
            rows.append({"name": it["name"], "typ": typ, "date": _fmt_date(it["mtime_ns"]),
                         "size": it["size"], "types": top_types, "levels": levels})
        # multi-key sort: chained stable sorts, least-significant key first
        for key, descending in reversed(sort_spec):
            if key == "size":
                rows.sort(key=lambda r: r["size"], reverse=descending)
            elif key == "type":
                rows.sort(key=lambda r: r["typ"].casefold(), reverse=descending)
            else:
                rows.sort(key=lambda r: r["name"].casefold(), reverse=descending)

        sort_note = ", ".join(f"{k} {'desc' if d else 'asc'}" for k, d in sort_spec)
        w("")
        w(f"2. ROOT-LEVEL STRUCTURE ({total} entries at the root"
          + (f"; showing the {top_n} largest by size, {omitted} omitted" if omitted else "")
          + f"; sorted by {sort_note})")
        w("")
        header = (f"   {'name':<36} {'type':<9} {'date':<10} {'size':>10}  "
                  f"{'major file types':<52} subfolders by level")
        w(header)
        w("   " + "-" * (len(header) - 3))
        for r in rows:
            name = r["name"] if len(r["name"]) <= 36 else r["name"][:33] + "..."
            w(f"   {name:<36} {r['typ']:<9} {r['date']:<10} {human(r['size']):>10}  "
              f"{r['types']:<52} {r['levels']}")
    w("")
    with open(txt_path, "w", encoding="utf-8-sig") as f:  # BOM: Windows consoles decode it right
        f.write("\n".join(out))


MIN_PYTHON = (3, 11)  # deliberately chosen floor (current Anaconda base); tested there and on 3.14


def require_supported_python(tool: str) -> bool:
    if sys.version_info < MIN_PYTHON:
        have = ".".join(map(str, sys.version_info[:3]))
        print(f"{tool} needs Python {'.'.join(map(str, MIN_PYTHON))}+ — you are running {have}.\n"
              f"Install a newer Python (or conda env) and rerun.", file=sys.stderr)
        return False
    return True


def main(argv=None) -> int:
    if os.name != "nt":
        print("qc.py is Windows-only", file=sys.stderr)
        return 2
    if not require_supported_python("qc.py"):
        return 2
    p = argparse.ArgumentParser(description="quick read-only metadata census into SQLite")
    p.add_argument("drives", nargs="*", help="drive letters like C: E: (omit for a GUI picker)")
    p.add_argument("--db", default=None,
                   help="catalog database path (default: census_<letters>_drive_<YYYYMMDDHHMM>.sqlite beside qc.py)")
    p.add_argument("--list", action="store_true", help="list detected drives and exit")
    p.add_argument("--allow-db-on-scanned", action="store_true",
                   help="permit the catalog db to live on a drive being scanned")
    p.add_argument("--top", type=int, default=100,
                   help="root-level entries shown in the summary txt, largest first (default 100)")
    p.add_argument("--sort", default=DEFAULT_SORT,
                   help="summary-table sort, e.g. size:desc,type:asc,name:asc (keys: size|type|name)")
    p.add_argument("--include-cloud", action="store_true",
                   help="descend into cloud placeholder dirs (OneDrive online-only): metadata-only, "
                        "never downloads content; may make the sync client materialize child stubs")
    args = p.parse_args(argv)
    try:
        sort_spec = parse_sort(args.sort)
    except ValueError as e:
        p.error(str(e))
    script_dir = os.path.dirname(os.path.abspath(__file__))

    infos = detect_drives()
    if args.list:
        for d in infos:
            print(f"{d['drive']}  {d['label'] or '(no label)':<18} {d['fs'] or '?':<6} "
                  f"{human(d['total']):>10} total  {human(d['free']):>10} free  [{d['type']}]")
        return 0

    if args.drives:
        drives = []
        valid = {d["drive"].upper() for d in infos}
        for raw in args.drives:
            d = raw.rstrip(":\\").upper() + ":"
            if d not in valid:
                print(f"drive {d} not present (detected: {', '.join(sorted(valid))})", file=sys.stderr)
                return 2
            drives.append(d)
    else:
        drives, gui_db, gui_allow, gui_sort, gui_cloud = pick_drives_gui(
            infos, args.db, script_dir, args.sort)
        if not drives:
            print("nothing selected")
            return 0
        args.db = gui_db
        args.allow_db_on_scanned = args.allow_db_on_scanned or gui_allow
        args.include_cloud = args.include_cloud or gui_cloud
        sort_spec = parse_sort(gui_sort)

    if args.include_cloud and args.db is None:
        print("tip: --include-cloud catalogs are sensitive too — consider "
              "--db %LOCALAPPDATA%\\quickcensus\\... to keep them out of sync roots", file=sys.stderr)

    if args.db is None:
        args.db = os.path.join(script_dir, suggest_db_name(drives))
    db_path = os.path.abspath(args.db)
    db_drive = os.path.splitdrive(db_path)[0].upper()
    if db_drive in drives and not args.allow_db_on_scanned:
        print(f"refusing: catalog db {db_path} sits on {db_drive}, which is being scanned.\n"
              f"Pick another location with --db, or pass --allow-db-on-scanned to accept\n"
              f"that one file (and its -wal/-shm sidecars) being written there.", file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = open_db(db_path)
    skip = {(db_path + s).casefold() for s in ("", "-wal", "-shm")}
    txt_path = os.path.splitext(db_path)[0] + ".txt"
    scan_ids: list[int] = []
    try:
        for d in drives:
            scan_ids.append(scan_drive(con, d, skip, include_cloud=args.include_cloud))
    except KeyboardInterrupt:
        print("\naborted by user — completed drives are intact, current scan marked 'aborted'",
              file=sys.stderr)
        return 130
    else:
        write_summary(con, scan_ids, txt_path, args.top, sort_spec)
    finally:
        con.close()
    print(f"catalog: {db_path}")
    print(f"summary: {txt_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
