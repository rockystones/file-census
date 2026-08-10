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
from datetime import datetime, timezone

from qc import human, open_db, require_supported_python, suggest_db_name, write_summary, parse_sort, DEFAULT_SORT

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
# Microsoft Graph Command Line Tools (first-party public client; documented, secretless).
DEFAULT_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dae7d5"
SCOPE = "https://graph.microsoft.com/Files.Read"
SELECT = "id,name,size,parentReference,file,folder,fileSystemInfo,root"


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


def fetch_json(url: str, token: str, retries: int = 6) -> dict:
    """GET with bearer auth, honoring Retry-After on throttle/transient errors."""
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
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


def crawl(token: str, fetch=fetch_json) -> tuple[dict, list[dict], str | None]:
    """(drive info, all items, deltaLink). fetch is injectable for tests."""
    drive = fetch(f"{GRAPH}/me/drive?$select=id,driveType,name,owner,quota", token)
    items: list[dict] = []
    url = f"{GRAPH}/me/drive/root/delta?$select={SELECT}&$top=999"
    delta_link = None
    while url:
        page = fetch(url, token)
        items.extend(page.get("value", []))
        print(f"\r  {len(items):,} items fetched", end="", flush=True)
        url = page.get("@odata.nextLink")
        delta_link = page.get("@odata.deltaLink", delta_link)
    print()
    return drive, items, delta_link


def build_catalog(con, drive: dict, items: list[dict], delta_link: str | None) -> int:
    owner = (((drive.get("owner") or {}).get("user")) or {}).get("displayName") or "me"
    quota = drive.get("quota") or {}
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = con.execute(
        "INSERT INTO scan(drive, label, fs_type, serial_hex, platform, disk_total, disk_free, "
        "started_utc) VALUES(?,?,?,?,?,?,?,?)",
        (f"onedrive:{owner}", drive.get("name"), drive.get("driveType"),
         drive.get("id"), "cloud", quota.get("total"), quota.get("remaining"), started))
    scan_id = cur.lastrowid

    root_gid = None
    by_gid: dict[str, dict] = {}
    for it in items:
        by_gid[it["id"]] = it
        if "root" in it:
            root_gid = it["id"]
    if root_gid is None:
        sys.exit("delta feed had no root item — nothing catalogued")

    next_id = (con.execute("SELECT COALESCE(MAX(entry_id), 0) FROM entry").fetchone()[0]) + 1
    eid_of: dict[str, int] = {}
    kids: dict[str, list[str]] = {}
    for it in items:
        pid = (it.get("parentReference") or {}).get("id")
        if pid and it["id"] != root_gid:
            kids.setdefault(pid, []).append(it["id"])
    orphans = [g for g, it in by_gid.items()
               if g != root_gid and (it.get("parentReference") or {}).get("id") not in by_gid]
    if orphans:
        print(f"  note: {len(orphans)} items with no reachable parent attached under root",
              file=sys.stderr)
        kids.setdefault(root_gid, []).extend(orphans)

    batch = []
    n_files = n_dirs = n_bytes = 0
    root_eid = next_id
    next_id += 1
    eid_of[root_gid] = root_eid
    batch.append((root_eid, scan_id, None, f"onedrive:{owner}", 1, None, None, None,
                  0, None, None, 0))
    stack = [(root_gid, 0)]
    while stack:
        gid, depth = stack.pop()
        for cg in kids.get(gid, ()):
            it = by_gid[cg]
            fsi = it.get("fileSystemInfo") or {}
            mtime = iso_to_ns(fsi.get("lastModifiedDateTime"))
            birth = iso_to_ns(fsi.get("createdDateTime"))
            eid = next_id
            next_id += 1
            eid_of[cg] = eid
            if "folder" in it:
                batch.append((eid, scan_id, eid_of[gid], it.get("name") or "?", 1, None,
                              mtime, birth, 0, None, None, depth + 1))
                n_dirs += 1
                stack.append((cg, depth + 1))
            else:
                name = it.get("name") or "?"
                ext = os.path.splitext(name)[1][1:].lower() or None
                h = ((it.get("file") or {}).get("hashes") or {}).get("quickXorHash")
                size = it.get("size") or 0
                batch.append((eid, scan_id, eid_of[gid], name, 0, size,
                              mtime, birth, 0, None, ext, depth + 1, h))
                n_files += 1
                n_bytes += size
    dirs_rows = [b for b in batch if len(b) == 12]
    file_rows = [b for b in batch if len(b) == 13]
    con.executemany(
        "INSERT INTO entry(entry_id, scan_id, parent_id, name, is_dir, size_bytes, mtime_ns, "
        "birth_ns, attr, reparse_tag, ext, depth) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", dirs_rows)
    con.executemany(
        "INSERT INTO entry(entry_id, scan_id, parent_id, name, is_dir, size_bytes, mtime_ns, "
        "birth_ns, attr, reparse_tag, ext, depth, hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        file_rows)
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
    p.add_argument("--top", type=int, default=100)
    p.add_argument("--sort", default=DEFAULT_SORT)
    args = p.parse_args(argv)
    sort_spec = parse_sort(args.sort)

    token = device_code_signin(args.tenant, args.client_id)
    print("  signed in (token held in memory only); enumerating…")
    drive, items, delta_link = crawl(token)

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
