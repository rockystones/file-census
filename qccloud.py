#!/usr/bin/env python3
"""qccloud.py — metadata census of a OneDrive via Microsoft Graph, into the same
catalog schema qc.py uses (browsable in qcview, diffable scan-vs-scan).

Safety model:
- Delegated, read-only: OAuth device-code flow, scope Files.Read only. You sign in on
  Microsoft's page; the script never sees the password and cannot write to the drive.
- Tokens live in memory for the run and are never persisted, printed, or logged.
- Metadata only: the $select excludes downloadUrl — no request can reference content.
  The local filesystem is never touched, so hydration cannot occur by construction.
- Hashes (quickXorHash) are computed by the service and stored per file — content-grade
  dupe detection with zero bytes read.
- Scales: each page is compacted to ~300-byte records as it arrives and catalog rows
  are flushed in batches, so even a ~700k-item drive crawls in a few hundred MB of
  memory (holding raw Graph items for the whole crawl used to exhaust RAM).

Usage:
  python qccloud.py                       # sign in, census /me/drive, auto-named catalog
  python qccloud.py --db my.sqlite
  python qccloud.py --tenant consumers    # personal accounts only ('organizations', or a
                                          # tenant id, for work/school; default 'common')
  python qccloud.py --client-id <guid>    # your own Entra app registration
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from datetime import datetime, timezone

from qc import human, open_db, require_supported_python, suggest_db_name, write_summary, parse_sort, DEFAULT_SORT

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
# Microsoft Graph Command Line Tools (first-party public client; documented, secretless).
DEFAULT_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dae7d5"
SCOPE = "https://graph.microsoft.com/Files.Read"
# 'package' matters: OneNote notebooks are package items (containers with children,
# but no folder facet) — without it their subtrees would be silently dropped.
SELECT = "id,name,size,parentReference,file,folder,fileSystemInfo,root,package"


MSA_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"  # Microsoft's personal-account directory
REGISTER_APP = ("See 'Registering your own app' in README.md — 5 minutes, no cost. In short:\n"
                "  Entra portal > App registrations > New registration\n"
                "    - account types: include personal Microsoft accounts if you use a\n"
                "      personal OneDrive\n"
                "    - Authentication > Allow public client flows: YES  (device code needs it)\n"
                "    - API permissions > Microsoft Graph > Delegated > Files.Read\n"
                "  then rerun with --client-id <Application (client) ID>")


def _signin_guidance(msg: str, tenant: str, client_id: str) -> str:
    """Translate an AADSTS error into the next thing to try."""
    m = msg or ""
    if "AADSTS50059" in m:
        return ("What it means: device-code sign-in must be created against ONE concrete\n"
                "tenant, and it happens before you sign in — so there is no username for\n"
                "Entra to infer one from. Both 'common' and 'organizations' are ambiguous\n"
                "multi-tenant endpoints and will always fail here.\n"
                "Use:  --tenant consumers      personal @outlook/@hotmail/@live account\n"
                "                              (this one IS concrete: the personal-account tenant)\n"
                "      --tenant yourschool.edu work or school — the exact domain, or its\n"
                "                              tenant GUID; nothing vaguer will resolve")
    if "AADSTS700016" in m:
        who = ("personal Microsoft accounts" if MSA_TENANT in m else f"tenant {tenant!r}")
        extra = ("\nThe default client (Microsoft Graph Command Line Tools) is not available to\n"
                 "personal Microsoft accounts, so a personal OneDrive always needs your own app."
                 if MSA_TENANT in m else
                 "\nGood news: the tenant itself resolved — only the client is missing. Your\n"
                 "organisation has not installed this Microsoft first-party app. Register your\n"
                 "own app WHILE SIGNED IN WITH THAT WORK ACCOUNT, so it lives inside the tenant\n"
                 "and needs no cross-tenant approval (Files.Read is normally user-consentable).")
        return (f"What it means: client {client_id} does not exist for {who}.{extra}\n\n"
                + REGISTER_APP)
    if "AADSTS7000218" in m or "client_assertion" in m or "client_secret" in m:
        return ("What it means: your app registration is not marked as a public client, so Entra\n"
                "expects a client secret that device-code flow does not use.\n"
                "Fix: Entra portal > your app > Authentication > Advanced settings >\n"
                "     'Allow public client flows' = Yes, then Save and rerun.")
    if "AADSTS50020" in m:
        return ("What it means: that account does not exist in the tenant you named.\n"
                f"You used --tenant {tenant!r}; sign in with a matching account, or switch to\n"
                "--tenant consumers (personal) / organizations (work).")
    if "AADSTS65001" in m or "consent" in m.lower():
        return ("What it means: the permission has not been consented to.\n"
                "If this is a work tenant you may need an administrator to grant Files.Read\n"
                "for the app (Entra portal > your app > API permissions > Grant admin consent).")
    if "AADSTS900023" in m or "invalid_tenant" in m.lower():
        return (f"What it means: {tenant!r} is not a recognized tenant.\n"
                "Use 'consumers', 'organizations', a verified domain, or the tenant GUID.")
    return REGISTER_APP


def _fail_signin(stage: str, payload: dict, tenant: str, client_id: str):
    msg = payload.get("error_description") or json.dumps(payload)
    print(f"\n{stage} failed.\n\n{msg.strip()}\n\n"
          f"{_signin_guidance(msg, tenant, client_id)}\n", file=sys.stderr)
    sys.exit(2)


def _post_form(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


class TokenProvider:
    """Holds the current bearer token in memory; renew() fetches a fresh one when a
    request comes back 401 (expired). The token itself is never logged or persisted
    by this tool."""

    def __init__(self, initial: str, renew_fn=None, source: str = ""):
        self.token = (initial or "").strip()
        self._renew_fn = renew_fn
        self.source = source

    def renew(self) -> bool:
        if not self._renew_fn:
            return False
        fresh = self._renew_fn()
        if fresh and fresh.strip() and fresh.strip() != self.token:
            self.token = fresh.strip()
            return True
        return False


def describe_token(token: str) -> str:
    """Local peek at the JWT payload (no verification, nothing sent anywhere):
    who it is for, which scopes, and when it expires. Never prints the token."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        import base64
        claims = json.loads(base64.urlsafe_b64decode(payload))
        who = claims.get("upn") or claims.get("preferred_username") or claims.get("app_displayname") or "?"
        scopes = claims.get("scp", "?")
        exp = claims.get("exp")
        left = ""
        if exp:
            mins = max(0, int(exp - time.time())) // 60
            # A multi-hour expiry means a CAE token (xms_cc): revocable mid-life, so
            # exp is an upper bound — Graph may 401 long before it (sign-out, policy,
            # IP change). Proven on a real crawl: rejected at minute ~302 of 1394.
            cap = (" — upper bound: revocable token, Graph may 401 sooner; "
                   "the tool then prompts for a fresh one and resumes"
                   if claims.get("xms_cc") or mins > 180 else "")
            left = f", expires in ~{mins} min{cap}"
        return f"token for {who} (scopes: {scopes}{left})"
    except Exception:
        return "token accepted (not a decodable JWT — proceeding anyway)"


def device_code_signin(tenant: str, client_id: str) -> str:
    """Returns an access token. Interactive: prints the code, waits for the user."""
    dc = _post_form(f"{LOGIN}/{tenant}/oauth2/v2.0/devicecode",
                    {"client_id": client_id, "scope": SCOPE})
    if "user_code" not in dc:
        _fail_signin("sign-in setup", dc, tenant, client_id)
    print(f"\n  To sign in: open {dc['verification_uri']} and enter code {dc['user_code']}\n"
          f"  (read-only Files.Read scope; no credential is stored — the token lives in\n"
          f"   memory for this run only, and no refresh token is requested)\n")
    interval = int(dc.get("interval", 5))
    while True:
        time.sleep(interval)
        tok = _post_form(f"{LOGIN}/{tenant}/oauth2/v2.0/token",
                         {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                          "client_id": client_id, "device_code": dc["device_code"]})
        if "access_token" in tok:
            return tok["access_token"]
        err = tok.get("error", "")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        _fail_signin("sign-in", tok, tenant, client_id)


def fetch_json(url: str, provider: TokenProvider, retries: int = 6) -> dict:
    """GET with bearer auth: honors Retry-After on throttling, and on 401 (token
    expired or revoked — classic tokens live ~1 h; long-lived CAE tokens can be
    rejected mid-life at any policy event) asks the provider for a fresh one and
    retries the same URL, so a long delta crawl resumes exactly where it was."""
    if not url.startswith(GRAPH.rsplit("/", 1)[0] + "/"):
        sys.exit(f"refusing to send the bearer token to a non-Graph URL: {url[:80]}")
    renewed = False
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {provider.token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and not renewed:
                print("\n  token expired or rejected (401)", file=sys.stderr)
                if provider.renew():
                    renewed = True
                    continue
                sys.exit("  no way to renew this token — rerun with a fresh one "
                         f"({provider.source or 'see --help'})")
            if e.code in (429, 503, 504) and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After", "5") or "5")
                print(f"\n  throttled ({e.code}) — waiting {wait}s", file=sys.stderr)
                time.sleep(min(wait, 120))
                continue
            sys.exit(f"Graph request failed ({e.code}): {e.read()[:300]}")
    sys.exit("Graph request failed after retries")


def iso_to_ns(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1e9)
    except ValueError:
        return None


# One raw Graph driveItem is ~2 KB of nested Python dicts; a 700k-item drive held
# raw is gigabytes and killed a real crawl with MemoryError. Each page is therefore
# compacted to these ~300-byte records the moment it arrives, and the raw page is
# dropped — the whole crawl then stays a few hundred MB regardless of drive size.
Item = namedtuple("Item", "gid pid name is_dir size mtime birth hash is_root")


def compact_item(it: dict) -> Item:
    fsi = it.get("fileSystemInfo") or {}
    pid = (it.get("parentReference") or {}).get("id")
    is_dir = "folder" in it or "package" in it  # packages: OneNote notebooks, containers
    return Item(
        sys.intern(it["id"]),                   # intern: child ids == parent ids, share them
        sys.intern(pid) if pid else None,
        it.get("name") or "?",
        is_dir,
        None if is_dir else (it.get("size") or 0),
        iso_to_ns(fsi.get("lastModifiedDateTime")),
        iso_to_ns(fsi.get("createdDateTime")),
        None if is_dir else ((it.get("file") or {}).get("hashes") or {}).get("quickXorHash"),
        "root" in it,
    )


def crawl(provider, fetch=fetch_json) -> tuple[dict, dict[str, Item], str | None]:
    """(drive info, compacted items by id, deltaLink). fetch is injectable for tests.
    Keyed by id because a delta enumeration may return the same item more than once —
    per the Graph contract the LAST occurrence is the current state and wins."""
    drive = fetch(f"{GRAPH}/me/drive?$select=id,driveType,name,owner,quota", provider)
    items: dict[str, Item] = {}
    url = f"{GRAPH}/me/drive/root/delta?$select={SELECT}&$top=999"
    delta_link = None
    while url:
        page = fetch(url, provider)
        for raw in page.get("value", ()):
            rec = compact_item(raw)
            items[rec.gid] = rec
        print(f"\r  {len(items):,} items fetched", end="", flush=True)
        url = page.get("@odata.nextLink")
        delta_link = page.get("@odata.deltaLink", delta_link)
    print()
    return drive, items, delta_link


def build_catalog(con, drive: dict, items: dict[str, Item], delta_link: str | None) -> int:
    owner = (((drive.get("owner") or {}).get("user")) or {}).get("displayName") or "me"
    quota = drive.get("quota") or {}
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = con.execute(
        "INSERT INTO scan(drive, label, fs_type, serial_hex, platform, disk_total, disk_free, "
        "started_utc) VALUES(?,?,?,?,?,?,?,?)",
        (f"onedrive:{owner}", drive.get("name"), drive.get("driveType"),
         drive.get("id"), "cloud", quota.get("total"), quota.get("remaining"), started))
    scan_id = cur.lastrowid

    root_gid = next((it.gid for it in items.values() if it.is_root), None)
    if root_gid is None:
        sys.exit("delta feed had no root item — nothing catalogued")

    # tree by ids: children grouped under their parent; anything whose parent is
    # missing OR is not a container (a file can't be walked into) attaches under
    # root instead of being silently lost
    kids: dict[str, list[Item]] = {}
    orphans: list[Item] = []
    for it in items.values():
        if it.gid == root_gid:
            continue
        parent = items.get(it.pid) if it.pid else None
        if parent is not None and parent.is_dir:
            kids.setdefault(it.pid, []).append(it)
        else:
            orphans.append(it)
    if orphans:
        print(f"  note: {len(orphans)} items with no reachable parent attached under root",
              file=sys.stderr)
        kids.setdefault(root_gid, []).extend(orphans)

    next_id = (con.execute("SELECT COALESCE(MAX(entry_id), 0) FROM entry").fetchone()[0]) + 1
    total = len(items)
    batch: list[tuple] = []

    def flush():
        nonlocal batch
        if batch:
            con.executemany(
                "INSERT INTO entry(entry_id, scan_id, parent_id, name, is_dir, size_bytes, "
                "mtime_ns, birth_ns, attr, reparse_tag, ext, depth, hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            batch = []
            con.commit()
            print(f"\r  cataloguing… {n_files + n_dirs:,}/{total:,}", end="", flush=True)

    eid_of: dict[str, int] = {}  # containers only — files are never parents
    n_files = n_dirs = n_bytes = 0
    root_eid = next_id
    next_id += 1
    eid_of[root_gid] = root_eid
    batch.append((root_eid, scan_id, None, f"onedrive:{owner}", 1, None, None, None,
                  0, None, None, 0, None))
    stack = [(root_gid, 0)]
    while stack:
        gid, depth = stack.pop()
        peid = eid_of[gid]
        for it in kids.get(gid, ()):
            eid = next_id
            next_id += 1
            if it.is_dir:
                batch.append((eid, scan_id, peid, it.name, 1, None,
                              it.mtime, it.birth, 0, None, None, depth + 1, None))
                n_dirs += 1
                eid_of[it.gid] = eid
                stack.append((it.gid, depth + 1))
            else:
                ext = os.path.splitext(it.name)[1][1:].lower() or None
                batch.append((eid, scan_id, peid, it.name, 0, it.size,
                              it.mtime, it.birth, 0, None, ext, depth + 1, it.hash))
                n_files += 1
                n_bytes += it.size
            if len(batch) >= 5000:
                flush()
    flush()
    print(f"\r{' ' * 40}\r", end="")
    con.execute(
        "UPDATE scan SET finished_utc=?, dir_count=?, file_count=?, byte_total=?, "
        "error_count=0, status='done', scope=NULL WHERE scan_id=?",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),
         n_dirs, n_files, n_bytes, scan_id))
    if delta_link:
        con.execute("CREATE TABLE IF NOT EXISTS cloud_state("
                    "drive_id TEXT PRIMARY KEY, delta_link TEXT, updated_utc TEXT)")
        con.execute("INSERT INTO cloud_state(drive_id, delta_link, updated_utc) VALUES(?,?,?) "
                    "ON CONFLICT(drive_id) DO UPDATE SET delta_link=excluded.delta_link, "
                    "updated_utc=excluded.updated_utc",
                    (drive.get("id"), delta_link, started))
    con.commit()
    hashed = con.execute("SELECT COUNT(*) FROM entry WHERE scan_id=? AND hash IS NOT NULL",
                         (scan_id,)).fetchone()[0]
    print(f"  scan {scan_id}: {n_files:,} files ({hashed:,} with service hashes), "
          f"{n_dirs:,} folders, {human(n_bytes)}")
    return scan_id


def main(argv=None) -> int:
    if not require_supported_python("qccloud.py"):
        return 2
    p = argparse.ArgumentParser(description="read-only OneDrive metadata census via Microsoft Graph")
    p.add_argument("--db", default=None, help="catalog path (default: onedrive-named beside script)")
    p.add_argument("--tenant", default="common",
                   help="'common' (default), 'consumers', 'organizations', or a tenant id")
    p.add_argument("--client-id", default=DEFAULT_CLIENT_ID,
                   help="Entra public-client app id (default: Microsoft Graph Command Line Tools)")
    p.add_argument("--token-file", default=None, metavar="PATH",
                   help="use a bearer token from this file instead of signing in — e.g. copied "
                        "from Graph Explorer's 'Access token' tab (the escape hatch when your "
                        "tenant blocks app registrations). On expiry you'll be prompted to "
                        "update the file and the crawl resumes where it was.")
    p.add_argument("--paste-token", action="store_true",
                   help="like --token-file but pasted interactively; nothing touches disk")
    p.add_argument("--top", type=int, default=100)
    p.add_argument("--sort", default=DEFAULT_SORT)
    args = p.parse_args(argv)
    sort_spec = parse_sort(args.sort)

    if args.token_file:
        path = os.path.abspath(args.token_file)

        def read_file_token(first=[True]):
            if not first[0]:
                input(f"\n  paste a fresh token into {path}\n"
                      f"  (Graph Explorer > sign in > 'Access token' tab > copy), save, "
                      f"then press Enter… ")
            first[0] = False
            try:
                with open(path, encoding="utf-8-sig") as f:
                    return f.read().strip()
            except OSError as e:
                sys.exit(f"cannot read token file: {e}")

        provider = TokenProvider(read_file_token(), renew_fn=read_file_token,
                                 source=f"update {path}")
    elif args.paste_token:
        import getpass

        def ask_token():
            # getpass, not input(): the token must not echo into the console scrollback
            return getpass.getpass("\n  paste the access token (hidden; stays in memory): ").strip()
        provider = TokenProvider(ask_token(), renew_fn=ask_token, source="paste a fresh token")
    else:
        provider = TokenProvider(
            device_code_signin(args.tenant, args.client_id),
            renew_fn=lambda: device_code_signin(args.tenant, args.client_id),
            source="device-code sign-in")
    if not provider.token:
        sys.exit("empty token")
    print(f"  {describe_token(provider.token)}")
    print("  (held in memory only; every request is read-only metadata); enumerating…")
    drive, items, delta_link = crawl(provider)

    db_path = os.path.abspath(args.db or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        suggest_db_name([]).replace("census_drive_", "census_onedrive_")))
    con = open_db(db_path)
    try:
        scan_id = build_catalog(con, drive, items, delta_link)
        txt = os.path.splitext(db_path)[0] + ".txt"
        write_summary(con, [scan_id], txt, args.top, sort_spec)
        print(f"catalog: {db_path}\nsummary: {txt}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
