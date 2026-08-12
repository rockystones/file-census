# quickcensus — read-only whole-drive metadata census

One file, standard library only, **Windows + Linux**, Python 3.11+ (Anaconda
base works). On Windows, drives are letters and identity comes from Win32
volume/device queries; on Linux, "drives" are block-device mount points from
`/proc/mounts` and identity comes from `lsblk` (filesystem UUID → volume
serial, PARTUUID → volume GUID, device model+serial → hardware). Linux scans
never follow symlinks and never cross into other filesystems (foreign mount
points are recorded like junctions); case sensitivity is respected per
platform. Walks entire drives and
records what directory enumeration alone provides — **by default, no file is
ever opened**:

- names + full folder structure (parent/child tree)
- sizes, modified + created timestamps
- attribute bits, reparse tags (junctions/symlinks recorded, never followed)
- extension, depth
- optionally (`--hash`, off by default) each file's **quickXorHash** — the
  content hash OneDrive itself reports — computed by reading the file
  read-only; see *Content hashing* below

Intended for non-sensitive drives where filenames are not a privacy concern.

## Safety & privacy model

**Safety of the scanned drives is structural, not promised.** In its default
mode the tool contains no code path that opens a file: metadata comes from
directory listings (`os.scandir`), so nothing can be modified, locked,
executed, or hydrated by a scan, and locked files cannot block it. Opting into
`--hash` adds exactly one new operation — `open(path, "rb")` and sequential
reads — and applies it only to files whose attributes prove it side-effect
free (see *Content hashing*). The only artifact written anywhere is
the catalog + summary pair, and qc.py refuses to place them on a drive being
scanned unless you pass `--allow-db-on-scanned` (the catalog file is then
excluded from its own census). There are zero network calls — standard library
only, no sockets — and nothing runs elevated. The one non-file handle the tool
opens is a *zero-access* volume-device handle for the hardware-identity query;
zero access means it can neither read nor write anything.

**Cloud placeholders (OneDrive Files On-Demand).** The trigger boundary is:
enumerating and stat-ing placeholders is safe; opening a file and reading its
bytes hydrates (downloads) it. qc never reads bytes, so it can never download
content. By default it does not even *enter* online-only folders (they appear
as single cloud-tagged rows) — entering them is a deliberate opt-in
(`--include-cloud`, or the GUI checkbox, default off) because enumerating an
online-only folder can make the sync client materialize child placeholder
stubs: a metadata network event, still zero content download. When you opt in,
the GUI suggests a private catalog location (see below).

**Privacy of the catalog itself.** A catalog lists every filename on a drive —
treat it as sensitive. The tool never transmits it (no telemetry, no uploads;
this repo also gitignores `census_*` files so catalogs cannot ride along on a
push). Beyond that, protection is exactly Windows file permissions on wherever
the catalog sits — with two honest caveats: other *administrators* of the same
machine can read any file regardless of ACLs, and **if the catalog lands inside
a cloud-synced folder it will sync to the cloud like anything else**. That is
the easiest accidental leak; keep catalogs out of sync roots (for example
`%LOCALAPPDATA%\quickcensus`, which the GUI suggests when the cloud checkbox
is on).

**Drive identity captured per scan** (all unelevated, recorded in the `scan`
row and the summary txt):
- *volume serial* — stamped at format time, stored on the volume, travels with
  the drive across machines and letter changes; 32-bit, so treat as a strong
  hint, not proof;
- *volume GUID* — unique and stable on this machine across letter changes, but
  assigned per Windows install (does not travel);
- *hardware product + serial* — the physical device's own identity via a
  zero-access `IOCTL_STORAGE_QUERY_PROPERTY`; survives reformatting; USB
  bridges occasionally report the enclosure or nothing.

Also: junction/symlink subtrees are never descended (cycle-safe); access-denied
directories are recorded in `scan_error` and the walk continues; Ctrl-C aborts
cleanly (finished drives keep status `done`, the interrupted scan is marked
`aborted`).

## Usage

```
python qc.py               # popup: tick drives, set the catalog path/name, Scan
python qc.py C: E:         # no popup (Linux: python qc.py / /mnt/data)
python qc.py --list        # show detected drives / mounts
python qc.py E: --db D:\catalogs\home.sqlite
python qc.py C: --only C:\Data --only C:\Projects   # scoped census (see below)
```

**Common error (Linux):** `tkinter is not installed, so the GUI can't open.`
Many Linux Pythons ship without tkinter — and the GUI is entirely optional:
every feature has a CLI twin, validated on a real Linux box:

```
python qc.py --list                # mounts with label/fs/size — works without tkinter
python qc.py /mnt                  # full census of a mount — works without tkinter
python qc.py /mnt --only /mnt/data/projects
```

To get the GUI: `sudo apt install python3-tk` (Debian/Ubuntu),
`sudo dnf install python3-tkinter` (Fedora), or `conda install tk` (Anaconda).

**Scoped census.** By default a census covers the whole drive. To limit it,
tick "Limit census to selected folders" in the popup and use **Choose
folders…** — a lazy folder tree of the ticked drives where clicking a folder
ticks/unticks it (☑ includes its whole subtree; nested selections are
deduplicated; drive roots can't be ticked because unlimited is the default).
The CLI twin is repeatable `--only <folder>`. A scoped catalog still
reconstructs full paths (ancestor folders get rows without their siblings
being enumerated), the scan records its scope in the `scan.scope` column, and
the summary txt states the scope prominently — so a limited census can never
be mistaken for full drive coverage.

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

## Content hashing: `--hash`

```
python qc.py E: --hash          # census + quickXorHash of every file's content
```

Off by default (and a separate checkbox in the GUI). When on, the census opens
each regular file **read-only** and computes its **quickXorHash** — the same
content hash Microsoft's servers compute for every OneDrive file and report
through the Graph API (`file.hashes.quickXorHash`). The hash lands in the
`entry.hash` column that `qccloud.py` fills for cloud catalogs, which is the
point: catalogs become joinable **by content**, across drives and against the
cloud —

```sql
-- which files on this backup drive already live in OneDrive, byte-identical?
SELECT l.path, c.path AS onedrive_path
FROM   local.v_paths l JOIN cloud.v_paths c
       ON l.hash = c.hash AND l.is_dir = 0 AND c.is_dir = 0;
```

Same-size files with different content, renamed copies, and moved trees all
resolve correctly — name/size dedup can only *suggest*, hash dedup *proves*
(up to hash collision, which quickXorHash being non-cryptographic makes
possible but which does not occur by accident in practice; treat matches on
this hash as "same file" for dedup triage, and re-verify byte-for-byte before
anything destructive).

**What is never hashed** (recorded as skipped, hash stays NULL): cloud
placeholders and offline files (`FILE_ATTRIBUTE_OFFLINE`, `RECALL_ON_OPEN`,
`RECALL_ON_DATA_ACCESS` — opening those would make the sync client download
content, the one thing this tool must never cause), reparse-tagged files
(links), and on Linux anything that is not a regular file. Unreadable files
are logged to `scan_error` with a `hash:` prefix and the walk continues.
Zero-byte files get their constant hash (`AAAAAAAAAAAAAAAAAAAAAAAAAAA=`)
without being opened.

**Cost:** every byte of the drive is read once, so a hashing census runs at
disk speed instead of enumeration speed — hours for terabytes, not seconds.
The implementation (stdlib-only, big-int XOR folding) sustains several hundred
MiB/s, so a spinning disk or USB enclosure is the bottleneck, not Python. The
progress line shows bytes hashed live, including inside multi-GiB files.
Correctness is pinned by published Microsoft/rclone test vectors and a
cross-check against an independent transcription of Microsoft's reference
implementation.

## Mapped network drives and UNC shares

Organisation shares work like any other volume — scan them by drive letter or,
better, by UNC path:

```
python qc.py Z: --only "Z:\Projects"
python qc.py \\fileserver\dept --only \\fileserver\dept\Projects
```

No credentials are handled: qc runs as you and inherits whatever Explorer
already authenticated. `--list` shows each mapped letter with the share behind
it, and the picker lists network drives too (they are selectable, just flagged
as slower).

**Prefer the UNC form.** A drive letter is per-machine and per-user — the same
share is `Z:` here and `S:` on a colleague's machine — so the catalog records
the UNC target in `scan.unc_path` and the summary prints it as the identity
line. UNC also works where a mapped letter does not: an elevated prompt or a
scheduled task runs in a different logon session and cannot see your mappings.

**Expect it to be slow, and scope it.** A network census is latency-bound, not
bandwidth-bound: every directory listing is a server round-trip, so a tree that
takes a minute locally can take hours over SMB. `--only` is the difference. If
the share is hosted on a machine you can log into, running qc.py *there*
against the local path is dramatically faster and captures real volume
identity.

**Check coverage afterwards.** If the network drops mid-scan, individual
folders fail into `scan_error` while the scan still finishes as `done` — so a
partial network census can look complete. Read the error count in the summary
before trusting the totals.

## When qc.py can't read a tree: qcexport.ps1 + qcimport.py

Sometimes the tree is readable by *you*, in a shell, on that machine — but not
by a qc.py run (permission scoping, no Python installed, an OneDrive root only
the logged-on user can enumerate). Two small pieces cover that, and no cloud
sign-in is involved:

```
rem  1. on that machine, from cmd.exe — metadata only, no file is opened
powershell -ExecutionPolicy Bypass -File qcexport.ps1 -Path "C:\Users\me\OneDrive"

rem  2. anywhere, on the CSV it produced
python qcimport.py qcexport_OneDrive_202608102230.csv --label "Work laptop"
```

`qcexport.ps1` walks with an explicit stack (no infinite recursion), keeps a
visited-path guard and a `-MaxDepth` cap, logs unreadable folders to a sidecar
`.errors.txt`, and writes UTF-8 CSV so non-ASCII names survive. It never opens
a file, so it cannot hydrate a cloud placeholder.

**Cloud folders vs links.** A OneDrive placeholder folder carries the same
`ReparsePoint` attribute as a junction, so a naive "skip reparse points" rule
lists *only the root level* of a OneDrive tree. The script tells them apart by
`LinkType` — real junctions/symlinks report one, cloud folders do not — and by
default **descends into cloud folders** (that is usually why you are running
it) while recording junctions/symlinks without following them. `-SkipCloud`
and `-FollowLinks` override each behaviour. The run summary states how many of
each it saw, so silent skipping is visible.

It works on network shares too, by mapped letter or UNC:

```
powershell -ExecutionPolicy Bypass -File qcexport.ps1 -Path "\\fileserver\dept\Projects"
powershell -ExecutionPolicy Bypass -File qcexport.ps1 -Path "Z:\Projects"
```

On a share it announces the network target, retries a folder listing that fails
transiently (`-Retries`, default 2 — an unretried blip would otherwise become a
silent gap), and writes a `.meta.json` sidecar recording which volume or share
the listing came from: the UNC target, volume label, filesystem, and serial.
A listing taken through `Z:` therefore still identifies its share, so it lines
up with scans taken from another machine where the same share is `S:`.

`qcimport.py` turns that CSV into an ordinary catalog — recorded as a **scoped
scan** whose scope is the folder you listed, so a partial listing can never be
mistaken for whole-drive coverage. It picks up the sidecar automatically;
`--serial`, `--label` and `--unc` override it if you know better. The result opens in qcview and diffs
scan-vs-scan like any other catalog. Verified end-to-end: an exported+imported
tree matches a direct `qc.py --only` scan of the same folder exactly — same
files, same sizes, same timestamps to the nanosecond.

## OneDrive census: qccloud.py

```
python qccloud.py                     # device-code sign-in, census /me/drive
python qccloud.py --token-file %TEMP%\token.txt   # borrowed Graph Explorer token
```

**[docs-qccloud.html](docs-qccloud.html)** is the implementation walkthrough —
how the token flows, how the delta crawl builds the catalog, and the audited
safety model with its honest limits.

Built for large drives: each Graph page is compacted to small records as it
arrives and catalog rows are flushed in batches, so a ~700k-item drive crawls
in roughly 350 MB of memory (regression-tested at that scale). Delta feeds that
mention an item more than once keep the last occurrence, per the Graph
contract; OneNote notebooks (`package` items) are catalogued as containers with
their contents; items whose parent is missing from the feed attach under the
drive root with a printed note.

### Choosing the tenant

`--tenant` decides which sign-in authority is used, and getting it wrong is the
most common first failure:

| account | flag |
|---|---|
| personal (@outlook / @hotmail / @live) | `--tenant consumers` |
| work or school | the **exact domain** (`--tenant pitt.edu`) or its tenant GUID |

Device-code sign-in must be minted against **one concrete tenant**, and it
happens *before* you authenticate, so Entra has no username to infer one from.
That makes both `common` (the default) and `organizations` fail with
**AADSTS50059** — they are ambiguous multi-tenant endpoints. `consumers` works
because it resolves to the one fixed personal-account directory; for work or
school, name the domain. The tool translates this and the other common AADSTS
codes into the next thing to try.

### Registering your own app

Required for **personal OneDrive** (Microsoft's default first-party client does
not exist in the personal-account directory — error **AADSTS700016**), and for
work tenants that have not consented to that client. Five minutes, no cost:

1. Open the [Entra portal](https://entra.microsoft.com) → **App registrations**
   → **New registration**. **Sign in with the account whose OneDrive you want
   to census** — registering inside that tenant avoids any cross-tenant consent
   question. (A personal Microsoft account gets a free default directory.)
2. **Name** it anything (e.g. `quickcensus`). **Supported account types**: pick
   *"Accounts in any organizational directory … and personal Microsoft
   accounts"* if you will census a personal OneDrive; work-only is fine
   otherwise. Leave **Redirect URI** empty — device-code flow does not use one.
   Click **Register**.
3. Copy the **Application (client) ID** from the overview page.
4. **Authentication** → *Advanced settings* → **Allow public client flows =
   Yes** → Save. This step is easy to miss and its absence fails later with
   **AADSTS7000218** ("client_assertion or client_secret required").
5. **API permissions** → *Add a permission* → **Microsoft Graph** → *Delegated
   permissions* → tick **Files.Read** → Add. On a work tenant an administrator
   may need to click **Grant admin consent**.
6. Run it:

```
python qccloud.py --tenant consumers --client-id <application-client-id>
```

Your own registration is also the better long-term choice for the work account:
consent, audit trail, and revocation all sit under an app you control.

### Blocked from registering? Borrow Graph Explorer's token

Some tenants (universities especially) block user app registrations but still
allow Microsoft's own **Graph Explorer** (https://aka.ms/ge). If signing in
there and running `GET /me/drive/root/children` works, you have everything you
need — Graph Explorer will show you its bearer token, and this tool can run on
it:

1. In Graph Explorer, sign in and run any `/me/drive` request once (consent
   `Files.Read` if prompted).
2. Open the **Access token** tab, copy the token, save it to a file *outside
   any repo or synced folder* (e.g. `%TEMP%\token.txt`).
3. Run:

```
python qccloud.py --token-file %TEMP%\token.txt
```

`--paste-token` does the same interactively with nothing written to disk.
The tool prints who the token is for, its scopes, and its remaining lifetime
(decoded locally), then crawls. Tokens live ~1 hour; if one expires mid-crawl
you are prompted to paste a fresh one and **the crawl resumes exactly where it
was** (the delta feed's next-link is retried, not restarted). Delete the token
file afterwards — it is a real credential for its remaining minutes, scoped to
whatever Graph Explorer was consented. The same route works for personal
accounts, so it also covers the case where you'd rather not register an app at
all.

Catalogs a OneDrive **from the cloud side** via Microsoft Graph — the local
filesystem is never touched, so hydration cannot occur, and online-only files
are covered by definition. Safety model: delegated device-code sign-in with
read-only `Files.Read` scope (you authenticate on Microsoft's page; the script
never sees the password and holds the token in memory only — nothing is
persisted); every request is metadata-only (`$select` excludes `downloadUrl`);
throttling honors `Retry-After`. Each file also carries the service-computed
`quickXorHash` into the catalog's `hash` column — content-grade dupe detection
with zero bytes read (local scans leave the column NULL). The Graph
`deltaLink` is stored per drive for future incremental runs. Default client is
Microsoft's own Graph Command Line Tools public app; organizational tenants
that block it need your own Entra registration (public client, delegated
Files.Read) via `--client-id`. Cloud catalogs open in qcview like any other
scan ('/', case-insensitive). Note the complement: Graph sees the *cloud*
state — local-only unsynced files need a normal qc.py scan.

## Browsing a catalog: qcview.py

```
python qcview.py                     # file-open dialog
python qcview.py census_E_drive_202608061920.sqlite
```

A read-only, File-Explorer-style browser over any qc.py catalog: expandable
tree with Type / Modified / Size / Items columns (folder sizes and item counts
are recursive), a scan picker when the catalog holds several scans, and the
full path of the selection in the status bar. For dense listings it collapses
sibling files or folders whose names differ only in digit runs into one
pattern row — `≡ cv_eis_16ch(#).nox ×11` — expandable on click; digit runs
identical across the whole family stay literal, only the varying ones become
`#`. The "Collapse similar names ≥ N" toggle and threshold control it
(default ≥ 5).

When a catalog holds two or more scans, the **Diff…** button compares any pair
A→B (defaults to the two most recent): a change-only tree shows added (green),
removed (red), and size/mtime-changed (orange) files, with every ancestor
folder badged with its rollup (`+12 −3 ~5`, net bytes). Comparison is keyed on
paths relative to the drive root, so re-lettered drives still compare;
directory-mtime drift is deliberately ignored as noise. Status checkboxes
filter the view; small diffs open fully expanded.

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
