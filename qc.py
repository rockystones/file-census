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

Windows-only. Python 3.12+ (os.listdrives). Standard library only.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import sqlite3
import sys
import time
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
  fs_type      TEXT,
  serial_hex   TEXT,
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


def detect_drives() -> list[dict]:
    out = []
    for root in os.listdrives():
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


def pick_drives_gui(drives: list[dict], default_db: str) -> tuple[list[str], str, bool]:
    """Returns (picked_drives, db_path, allow_db_on_scanned). Empty list = cancelled."""
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
    for d in drives:
        text = (f"{d['drive']}  {d['label'] or '(no label)'}   {d['fs'] or '?'}   "
                f"{human(d['total'])} total, {human(d['free'])} free   [{d['type']}]")
        v = tk.BooleanVar(value=False)
        state = "normal" if d["type"] in ("fixed", "removable", "ramdisk") else "disabled"
        tk.Checkbutton(root, text=text, variable=v, anchor="w", padx=16, state=state).pack(fill="x")
        vars_by_drive[d["drive"]] = v

    # catalog destination: editable path (custom name welcome) + file dialog
    dest = tk.Frame(root)
    dest.pack(fill="x", padx=12, pady=(10, 0))
    tk.Label(dest, text="Catalog file:").pack(side="left")
    db_var = tk.StringVar(value=default_db)
    tk.Entry(dest, textvariable=db_var, width=64).pack(side="left", fill="x", expand=True, padx=6)

    def browse():
        cur = db_var.get().strip() or default_db
        chosen = filedialog.asksaveasfilename(
            parent=root, title="Save catalog as",
            initialdir=os.path.dirname(os.path.abspath(cur)) or ".",
            initialfile=os.path.basename(cur) or "census.sqlite",
            defaultextension=".sqlite",
            filetypes=[("SQLite catalog", "*.sqlite"), ("All files", "*.*")],
            confirmoverwrite=False)  # appending to an existing catalog is normal
        if chosen:
            db_var.set(chosen)

    tk.Button(dest, text="Browse…", command=browse).pack(side="left")
    allow_var = tk.BooleanVar(value=False)
    tk.Checkbutton(root, text="Allow the catalog to live on a scanned drive "
                              "(the catalog file itself is excluded from the census)",
                   variable=allow_var, anchor="w", padx=16).pack(fill="x", pady=(2, 0))

    picked: list[str] = []
    result = {"db": default_db, "allow": False}

    def go():
        sel = [k for k, v in vars_by_drive.items() if v.get()]
        if not sel:
            messagebox.showwarning("quick census", "No drives selected.", parent=root)
            return
        dbp = os.path.abspath(db_var.get().strip() or default_db)
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
        root.destroy()

    row = tk.Frame(root)
    row.pack(pady=10)
    tk.Button(row, text="Scan", width=12, command=go).pack(side="left", padx=6)
    tk.Button(row, text="Cancel", width=12, command=root.destroy).pack(side="left", padx=6)
    root.mainloop()
    return picked, result["db"], result["allow"]


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("PRAGMA synchronous = NORMAL")
    return con


def scan_drive(con: sqlite3.Connection, drive: str, skip_paths_cf: set[str]) -> int:
    root = drive + "\\"
    label, fs, serial = volume_info(root)
    try:
        usage = shutil.disk_usage(root)
        disk_total, disk_free = usage.total, usage.free
    except OSError:
        disk_total = disk_free = None
    cur = con.execute(
        "INSERT INTO scan(drive, label, fs_type, serial_hex, disk_total, disk_free, started_utc) "
        "VALUES(?,?,?,?,?,?,?)",
        (drive, label, fs, serial, disk_total, disk_free, iso_now()))
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
                        if not is_reparse:  # junction/symlink dirs recorded, never descended
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


def main(argv=None) -> int:
    if os.name != "nt":
        print("qc.py is Windows-only", file=sys.stderr)
        return 2
    p = argparse.ArgumentParser(description="quick read-only metadata census into SQLite")
    p.add_argument("drives", nargs="*", help="drive letters like C: E: (omit for a GUI picker)")
    p.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                "census.sqlite"),
                   help="catalog database path (default: census.sqlite beside qc.py)")
    p.add_argument("--list", action="store_true", help="list detected drives and exit")
    p.add_argument("--allow-db-on-scanned", action="store_true",
                   help="permit the catalog db to live on a drive being scanned")
    args = p.parse_args(argv)

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
        drives, gui_db, gui_allow = pick_drives_gui(infos, args.db)
        if not drives:
            print("nothing selected")
            return 0
        args.db = gui_db
        args.allow_db_on_scanned = args.allow_db_on_scanned or gui_allow

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
    try:
        for d in drives:
            scan_drive(con, d, skip)
    except KeyboardInterrupt:
        print("\naborted by user — completed drives are intact, current scan marked 'aborted'",
              file=sys.stderr)
        return 130
    finally:
        con.close()
    print(f"catalog: {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
