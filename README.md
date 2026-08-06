# quickcensus — read-only whole-drive metadata census

One file, standard library only, Windows, Python 3.12+. Walks entire drives and
records what directory enumeration alone provides — **no file is ever opened**:

- names + full folder structure (parent/child tree)
- sizes, modified + created timestamps
- attribute bits, reparse tags (junctions/symlinks recorded, never followed)
- extension, depth

Intended for non-sensitive drives where filenames are not a privacy concern.

## Guarantees

- **Zero writes to scanned drives.** The only artifact is the SQLite catalog,
  and qc.py refuses to place it on a drive being scanned unless you pass
  `--allow-db-on-scanned` (then the db file itself is excluded from the census).
- **No file handles.** Metadata comes from the directory listing (`os.scandir`);
  contents are never read, nothing is hashed, locked files cannot block it.
- Junction/symlink subtrees are not descended (cycle-safe); access-denied
  directories are recorded in `scan_error` and the walk continues.
- Ctrl-C aborts cleanly: finished drives keep status `done`, the interrupted
  scan is marked `aborted`.

## Usage

```
python qc.py               # popup: tick drives, set the catalog path/name, Scan
python qc.py C: E:         # no popup
python qc.py --list        # show detected drives
python qc.py E: --db D:\catalogs\home.sqlite
```

The default catalog name encodes the selection and the moment:
`census_E_drive_202608061920.sqlite` (letters of the chosen drives + local
`YYYYMMDDHHMM`), created beside qc.py. In the popup the **Catalog file** field
live-updates this suggestion as you tick drives — until you type or Browse… to
your own path, which pins your choice. Choosing a location on a drive you are
about to scan warns immediately — tick the allow checkbox to accept it (the
catalog file itself is then excluded from the census). Pointing at an existing
catalog appends a new scan to it rather than overwriting.

Each run also writes a **summary preview** next to the catalog
(`census_E_drive_202608061920.txt`): overall stats + top file types, unreadable
paths, and a root-level structure table — one row per first-level folder/file
with recursive size, date, its major file types with counts, and subfolder
counts per level (`lv2:13 lv3:58 …`) so drive depth/width is visible at a
glance. Roots beyond the `--top` largest (default 100) are omitted with a note.

The table's ordering is configurable: in the popup, three "Summary sort" slots
(key `size`/`type`/`name` + `asc`/`desc` each; set a slot to `—` to skip it)
define a multi-key sort, priority left to right; on the command line the same
spec is `--sort size:desc,type:asc,name:asc` (that string is the default). The
`--top` cap always keeps the *largest* roots regardless of sort — the sort only
orders what is shown, and the section header records which sort was used.

Each run adds a new `scan` row per drive — history accumulates in one catalog,
so two scans of the same drive can be compared later.

## Catalog schema (SQLite)

- `scan` — one row per drive per run: label, fs, serial, disk totals, timings,
  final counts, status.
- `entry` — one row per file/dir: `parent_id` tree, `name`, `is_dir`,
  `size_bytes`, `mtime_ns`/`birth_ns` (Unix nanoseconds), `attr`, `reparse_tag`,
  `ext`, `depth`. Paths are not stored flat — reconstruct via the `v_paths` view.
- `scan_error` — paths that could not be listed/stat'ed, with the error.
- `v_paths` — recursive view yielding `entry_id, scan_id, path, is_dir,
  size_bytes, depth` with full `X:\...` paths. Always filter it by `scan_id`.

## Example queries

Top space by extension:
```sql
SELECT ext, COUNT(*) files, SUM(size_bytes) bytes
FROM entry WHERE scan_id = 1 AND is_dir = 0
GROUP BY ext ORDER BY bytes DESC LIMIT 20;
```

Largest 20 files with full paths:
```sql
SELECT path, size_bytes FROM v_paths
WHERE scan_id = 1 AND is_dir = 0
ORDER BY size_bytes DESC LIMIT 20;
```

Immediate children of one folder, sized (folder rollup):
```sql
WITH RECURSIVE sub(entry_id, top) AS (
  SELECT entry_id, entry_id FROM entry
   WHERE scan_id = 1 AND parent_id = (SELECT entry_id FROM v_paths
                                      WHERE scan_id = 1 AND path = 'C:\Users')
  UNION ALL
  SELECT e.entry_id, s.top FROM entry e JOIN sub s ON e.parent_id = s.entry_id
)
SELECT (SELECT name FROM entry WHERE entry_id = s.top) folder,
       SUM(CASE WHEN e.is_dir = 0 THEN e.size_bytes ELSE 0 END) bytes,
       SUM(e.is_dir = 0) files
FROM sub s JOIN entry e ON e.entry_id = s.entry_id
GROUP BY s.top ORDER BY bytes DESC;
```

New/changed/vanished between two scans of the same drive (by path):
```sql
SELECT COALESCE(a.path, b.path) path,
       CASE WHEN a.path IS NULL THEN 'new'
            WHEN b.path IS NULL THEN 'vanished'
            ELSE 'changed' END what
FROM (SELECT path, size_bytes, entry_id FROM v_paths WHERE scan_id = 1 AND is_dir = 0) a
FULL OUTER JOIN
     (SELECT path, size_bytes, entry_id FROM v_paths WHERE scan_id = 2 AND is_dir = 0) b
  USING (path)
WHERE a.path IS NULL OR b.path IS NULL
   OR a.size_bytes <> b.size_bytes;
```

## Relationship to the butler demo

This is the filebutler census Pass A distilled to its no-handles core: same DFS,
same reparse guard, but multi-drive, GUI-pickable, and with a parent-id tree
instead of flat paths (compact at millions of rows). No hashing, no USN, no ops.
