#!/usr/bin/env python3
"""qcimport.py — turn a folder listing produced elsewhere into a normal census catalog.

Input: the CSV written by qcexport.ps1 (FullName, IsDir, Length, LastWriteTimeUtc,
CreationTimeUtc, Attributes). Use it when qc.py itself cannot read a tree — a
permission-scoped OneDrive root, a machine without Python, someone else's export.

The result is recorded as a SCOPED scan (scope = the listed roots), so a partial
listing can never be mistaken for whole-drive coverage. It opens in qcview and
diffs against other scans like any other catalog.

Usage:
  python qcimport.py listing.csv
  python qcimport.py listing.csv --db catalogs/laptop.sqlite --label "Work laptop"
  python qcimport.py listing.csv --serial a0accd9b     # if you know the volume serial
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import os
import sys
from datetime import datetime, timezone

from qc import (DEFAULT_SORT, human, open_db, parse_sort, require_supported_python,
                write_summary)

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
REQUIRED = {"FullName", "IsDir", "Length", "LastWriteTimeUtc", "CreationTimeUtc", "Attributes"}


def iso_to_ns(s: str | None) -> int | None:
    """.NET 'o' timestamps -> integer nanoseconds, without losing precision.

    The 'o' format carries 7 fractional digits (Windows' 100 ns FILETIME ticks), which
    datetime cannot hold and float seconds cannot represent — so seconds and fraction
    are converted separately, as integers. This makes imported mtimes byte-identical
    to the ones qc.py reads from os.stat, so imports and scans compare cleanly.
    """
    if not s:
        return None
    t = s.strip().rstrip("Z")
    frac_ns = 0
    if "." in t:
        t, frac = t.split(".", 1)
        frac_ns = int(frac[:9].ljust(9, "0"))
    try:
        dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return calendar.timegm(dt.utctimetuple()) * 1_000_000_000 + frac_ns


def read_rows(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rdr = csv.DictReader(f)
        missing = REQUIRED - set(rdr.fieldnames or ())
        if missing:
            sys.exit(f"{path}: missing column(s) {sorted(missing)} — was this written by "
                     "qcexport.ps1?")
        return list(rdr)


def read_meta(csv_path: str) -> dict:
    """Sidecar written by qcexport.ps1: which volume/share the listing came from."""
    p = os.path.splitext(csv_path)[0] + ".meta.json"
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"note: ignoring unreadable {os.path.basename(p)} ({e})", file=sys.stderr)
        return {}


def build(con, rows: list[dict], label: str | None, serial: str | None,
          source: str, unc: str | None = None, meta: dict | None = None) -> int:
    entries: dict[str, dict] = {}
    for r in rows:
        p = (r["FullName"] or "").rstrip("\\/")
        if not p:
            continue
        attr = int(r["Attributes"] or 0)
        is_dir = str(r["IsDir"]).strip().lower() in ("1", "true", "yes")
        entries[p] = {
            "is_dir": is_dir,
            "size": None if is_dir or r["Length"] in ("", None) else int(r["Length"]),
            "mtime": iso_to_ns(r["LastWriteTimeUtc"]),
            "birth": iso_to_ns(r["CreationTimeUtc"]),
            "attr": attr,
            "tag": FILE_ATTRIBUTE_REPARSE_POINT if attr & FILE_ATTRIBUTE_REPARSE_POINT else None,
        }
    if not entries:
        sys.exit("no rows to import")

    # splitdrive yields 'C:' for local paths and '\\\\server\\share' for UNC ones
    drives = {os.path.splitdrive(p)[0] for p in entries}
    drive = sorted(drives, key=str.casefold)[0] or "?:"
    if len(drives) > 1:
        print(f"note: listing spans {sorted(drives)}; cataloguing under {drive}", file=sys.stderr)
    if not drive.startswith("\\\\"):
        drive = drive.upper()

    # identity: CLI flags win, then the sidecar, then the path shape itself
    roots_meta = (meta or {}).get("roots") or []
    if not unc:
        unc = next((r.get("unc") for r in roots_meta if r.get("unc")), None)
    if not unc and drive.startswith("\\\\"):
        unc = drive
    if not label:
        label = next((r.get("volumeLabel") for r in roots_meta if r.get("volumeLabel")), None)
    if not serial:
        serial = next((r.get("serial") for r in roots_meta if r.get("serial")), None)

    # scope = the topmost listed folders (those whose parent was not itself listed)
    lower = {p.casefold() for p in entries}
    roots = sorted(p for p in entries
                   if entries[p]["is_dir"] and os.path.dirname(p).casefold() not in lower)
    if not roots:
        roots = sorted({os.path.dirname(p) for p in entries})[:1]

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fs_type = next((r.get("fileSystem") for r in roots_meta if r.get("fileSystem")), None)
    cur = con.execute(
        "INSERT INTO scan(drive, label, fs_type, serial_hex, platform, scope, unc_path, "
        "started_utc) VALUES(?,?,?,?,?,?,?,?)",
        (drive, label, fs_type, serial, "win", json.dumps(roots), unc, now))
    scan_id = cur.lastrowid

    next_id = (con.execute("SELECT COALESCE(MAX(entry_id), 0) FROM entry").fetchone()[0]) + 1
    eid: dict[str, int] = {}
    batch: list[tuple] = []
    root_eid = next_id
    next_id += 1
    eid[drive.casefold()] = root_eid
    batch.append((root_eid, scan_id, None, drive, 1, None, None, None,
                  FILE_ATTRIBUTE_DIRECTORY, None, None, 0))

    drive_cf = drive.casefold()

    def ensure(path: str) -> int:
        """Return the entry id for a directory path, creating ancestor rows as needed."""
        nonlocal next_id
        path = path.rstrip("\\/") or drive          # 'C:\' and 'C:' are the same node
        key = path.casefold()
        if key == drive_cf:
            return root_eid                          # never create a second drive row
        if key in eid:
            return eid[key]
        parent = os.path.dirname(path)
        pid = ensure(parent) if parent and parent.casefold() != key else root_eid
        info = entries.get(path)
        depth = path.count("\\")
        e = next_id
        next_id += 1
        eid[key] = e
        batch.append((e, scan_id, pid, os.path.basename(path) or path, 1, None,
                      (info or {}).get("mtime"), (info or {}).get("birth"),
                      (info or {}).get("attr", FILE_ATTRIBUTE_DIRECTORY),
                      (info or {}).get("tag"), None, depth))
        return e

    n_files = n_dirs = 0
    n_bytes = 0
    for p in sorted(entries, key=lambda s: (s.count("\\"), s.casefold())):
        info = entries[p]
        if info["is_dir"]:
            ensure(p)
            n_dirs += 1
        else:
            parent = ensure(os.path.dirname(p))
            name = os.path.basename(p)
            ext = os.path.splitext(name)[1][1:].lower() or None
            depth = p.count("\\")
            e = next_id
            next_id += 1
            batch.append((e, scan_id, parent, name, 0, info["size"], info["mtime"],
                          info["birth"], info["attr"], info["tag"], ext, depth))
            n_files += 1
            n_bytes += info["size"] or 0

    con.executemany(
        "INSERT INTO entry(entry_id, scan_id, parent_id, name, is_dir, size_bytes, mtime_ns, "
        "birth_ns, attr, reparse_tag, ext, depth) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", batch)

    err_path = os.path.splitext(source)[0] + ".errors.txt"
    n_err = 0
    if os.path.exists(err_path):
        with open(err_path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                path, _, msg = line.partition("\t")
                con.execute("INSERT INTO scan_error(scan_id, path, error) VALUES(?,?,?)",
                            (scan_id, path, msg or "unreadable"))
                n_err += 1

    con.execute(
        "UPDATE scan SET finished_utc=?, dir_count=?, file_count=?, byte_total=?, "
        "error_count=?, status='done' WHERE scan_id=?",
        (now, n_dirs, n_files, n_bytes, n_err, scan_id))
    con.commit()
    print(f"  scan {scan_id}: {n_files:,} files, {n_dirs:,} folders, {human(n_bytes)}, "
          f"{n_err} unreadable path(s)")
    if unc:
        print(f"    network share: {unc} (recorded as this volume's identity)")
    for r in roots:
        print(f"    scope: {r}")
    return scan_id


def main(argv=None) -> int:
    if not require_supported_python("qcimport.py"):
        return 2
    p = argparse.ArgumentParser(description="import a folder listing (qcexport.ps1 CSV) "
                                            "as a scoped census scan")
    p.add_argument("csv", help="CSV produced by qcexport.ps1")
    p.add_argument("--db", default=None, help="catalog path (default: alongside the CSV)")
    p.add_argument("--label", default=None, help="volume label to record")
    p.add_argument("--serial", default=None, help="volume serial, if known (ties this listing "
                                                  "to scans of the same drive)")
    p.add_argument("--unc", default=None, help=r"UNC share this listing came from, e.g. "
                                               r"\\server\share (usually detected automatically "
                                               "from the .meta.json sidecar or the paths)")
    p.add_argument("--top", type=int, default=100)
    p.add_argument("--sort", default=DEFAULT_SORT)
    args = p.parse_args(argv)
    sort_spec = parse_sort(args.sort)

    rows = read_rows(args.csv)
    meta = read_meta(args.csv)
    db_path = os.path.abspath(args.db or (os.path.splitext(args.csv)[0] + ".sqlite"))
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = open_db(db_path)
    try:
        scan_id = build(con, rows, args.label, args.serial, os.path.abspath(args.csv),
                        unc=args.unc, meta=meta)
        txt = os.path.splitext(db_path)[0] + ".txt"
        write_summary(con, [scan_id], txt, args.top, sort_spec)
        print(f"catalog: {db_path}\nsummary: {txt}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
