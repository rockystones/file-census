#!/usr/bin/env python3
"""qcview.py — File-Explorer-style browser for quick census catalogs (read-only).

Opens a census_*.sqlite produced by qc.py and shows the cataloged tree with
type / date / recursive size / item-count columns. Sibling files or folders
whose names differ only in digit runs (IMG_0001.jpg … IMG_0500.jpg — one shared
pattern) collapse into a single "≡ IMG_#.jpg ×500" row; click to expand.

Usage:  python qcview.py [catalog.sqlite]     (no arg: file-open dialog)
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

from qc import human  # same folder; shared byte formatter

DIGITS = re.compile(r"\d+")
GROUP_MIN_DEFAULT = 5


def family_key(name: str) -> str:
    """Names that differ only in digit runs share a key: 'IMG_0001.jpg' -> 'img_#.jpg'."""
    return DIGITS.sub("#", name.casefold())


def group_label(names: list[str]) -> str:
    """Pattern label for a family: digit runs that vary across members become '#',
    digit runs identical in every member are kept ('cv_eis_16ch(1).nox' … '(11).nox'
    -> 'cv_eis_16ch(#).nox')."""
    split = [re.split(r"(\d+)", n) for n in names]
    rep = split[0]
    if any(len(s) != len(rep) for s in split):
        return DIGITS.sub("#", names[0])  # structure mismatch: coarse fallback
    out = []
    for idx, seg in enumerate(rep):
        if idx % 2 == 1 and len({s[idx] for s in split}) > 1:
            out.append("#")
        else:
            out.append(seg)
    return "".join(out)


def natural_key(name: str):
    """'file10' sorts after 'file2': split into text/number runs."""
    return tuple(int(p) if p.isdigit() else p.casefold() for p in re.split(r"(\d+)", name))


def fmt_dt(ns) -> str:
    if not ns:
        return ""
    return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d %H:%M")


class Model:
    """Whole-scan tree in memory: entry info, children index, recursive aggregates."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.con = sqlite3.connect(db_path)
        self.con.row_factory = sqlite3.Row
        self.scans = [dict(r) for r in self.con.execute(
            "SELECT * FROM scan ORDER BY scan_id")]
        if not self.scans:
            raise ValueError("no scans in this catalog")

    def load_scan(self, scan_id: int):
        self.info: dict[int, sqlite3.Row] = {}
        self.children: dict[int, list[int]] = defaultdict(list)
        self.agg: dict[int, list[int]] = {}  # folder id -> [recursive bytes, recursive files]
        self.root_id = None
        rows = self.con.execute(
            "SELECT entry_id, parent_id, name, is_dir, size_bytes, mtime_ns, ext, reparse_tag, depth "
            "FROM entry WHERE scan_id=? ORDER BY depth DESC", (scan_id,)).fetchall()
        for r in rows:
            self.info[r["entry_id"]] = r
            if r["parent_id"] is None:
                self.root_id = r["entry_id"]
            else:
                self.children[r["parent_id"]].append(r["entry_id"])
            if r["is_dir"]:
                self.agg.setdefault(r["entry_id"], [0, 0])
        # rows arrive deepest-first, so every child's aggregate exists before its parent needs it
        for r in rows:
            pid = r["parent_id"]
            if pid is None:
                continue
            tgt = self.agg.setdefault(pid, [0, 0])
            if r["is_dir"]:
                sub = self.agg.get(r["entry_id"], [0, 0])
                tgt[0] += sub[0]
                tgt[1] += sub[1]
            else:
                tgt[0] += r["size_bytes"] or 0
                tgt[1] += 1

    def listing(self, folder_id: int, group_min: int | None):
        """Ordered node specs for one folder: ('dir'|'file', id) and
        ('group', kind, label, member_ids) — groups are same-pattern families."""
        kids = self.children.get(folder_id, [])
        dirs = sorted((i for i in kids if self.info[i]["is_dir"]),
                      key=lambda i: natural_key(self.info[i]["name"]))
        files = sorted((i for i in kids if not self.info[i]["is_dir"]),
                       key=lambda i: natural_key(self.info[i]["name"]))
        out = []
        for kind, ids in (("dir", dirs), ("file", files)):
            if group_min is None:
                out.extend((kind, i) for i in ids)
                continue
            buckets: dict[str, list[int]] = defaultdict(list)
            for i in ids:
                buckets[family_key(self.info[i]["name"])].append(i)
            grouped: set[int] = set()
            specs = []
            for key, members in buckets.items():
                if "#" in key and len(members) >= group_min:
                    grouped.update(members)
                    label = group_label([self.info[i]["name"] for i in members])
                    specs.append(("group", kind, label, members))
            for i in ids:
                if i not in grouped:
                    specs.append((kind, i))
            # keep natural order: groups sort by their label among the singles
            def spec_key(s):
                return natural_key(s[2] if s[0] == "group" else self.info[s[1]]["name"])
            specs.sort(key=spec_key)
            out.extend(specs)
        return out

    def path_of(self, entry_id: int) -> str:
        parts = []
        cur = entry_id
        while cur is not None:
            r = self.info[cur]
            parts.append(r["name"])
            cur = r["parent_id"]
        return "\\".join(reversed(parts))


def run_viewer(db_path: str | None):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("quick census viewer")
    root.geometry("1000x640")

    state = {"model": None, "scan_id": None, "groups": {}, "populated": set()}

    bar = tk.Frame(root)
    bar.pack(fill="x", padx=6, pady=4)
    tk.Button(bar, text="Open…", command=lambda: pick_file()).pack(side="left")
    scan_var = tk.StringVar()
    scan_box = ttk.Combobox(bar, textvariable=scan_var, width=56, state="readonly")
    scan_box.pack(side="left", padx=8)
    group_on = tk.BooleanVar(value=True)
    tk.Checkbutton(bar, text="Collapse similar names ≥", variable=group_on,
                   command=lambda: reload_tree()).pack(side="left", padx=(12, 0))
    min_var = tk.IntVar(value=GROUP_MIN_DEFAULT)
    tk.Spinbox(bar, from_=2, to=999, width=4, textvariable=min_var,
               command=lambda: reload_tree()).pack(side="left")

    tree = ttk.Treeview(root, columns=("type", "date", "size", "items"), selectmode="browse")
    tree.heading("#0", text="Name", anchor="w")
    tree.heading("type", text="Type", anchor="w")
    tree.heading("date", text="Modified", anchor="w")
    tree.heading("size", text="Size", anchor="e")
    tree.heading("items", text="Items", anchor="e")
    tree.column("#0", width=460)
    tree.column("type", width=80, stretch=False)
    tree.column("date", width=120, stretch=False)
    tree.column("size", width=90, anchor="e", stretch=False)
    tree.column("items", width=70, anchor="e", stretch=False)
    ys = ttk.Scrollbar(root, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=ys.set)
    ys.pack(side="right", fill="y")
    tree.pack(fill="both", expand=True, padx=6)

    status = tk.Label(root, text="Open a census_*.sqlite catalog", anchor="w")
    status.pack(fill="x", padx=6, pady=3)

    def group_min():
        return max(2, min_var.get()) if group_on.get() else None

    def node_row(entry_id: int):
        r = state["model"].info[entry_id]
        if r["is_dir"]:
            icon = "🔗 " if r["reparse_tag"] else "📁 "
            typ = "junction" if r["reparse_tag"] == 0xA0000003 else (
                "symlink" if r["reparse_tag"] else "folder")
            agg = state["model"].agg.get(entry_id, [0, 0])
            size, items = ("" if r["reparse_tag"] else human(agg[0]),
                           "" if r["reparse_tag"] else f"{agg[1]:,}")
            has_kids = bool(state["model"].children.get(entry_id)) and not r["reparse_tag"]
        else:
            icon = ""
            typ = r["ext"] or "file"
            size, items = human(r["size_bytes"] or 0), ""
            has_kids = False
        return icon + r["name"], (typ, fmt_dt(r["mtime_ns"]), size, items), has_kids

    def insert_specs(parent_iid: str, specs):
        m = state["model"]
        for spec in specs:
            if spec[0] == "group":
                _, kind, label, members = spec
                gid = f"g{len(state['groups'])}"
                state["groups"][gid] = members
                total = sum((m.agg.get(i, [0, 0])[0] if kind == "dir" else (m.info[i]["size_bytes"] or 0))
                            for i in members)
                latest = max((m.info[i]["mtime_ns"] or 0) for i in members)
                icon = "≡ 📁 " if kind == "dir" else "≡ "
                iid = tree.insert(parent_iid, "end", gid, text=f"{icon}{label}  ×{len(members)}",
                                  values=(f"{kind} group", fmt_dt(latest), human(total), f"{len(members):,}"))
                tree.insert(iid, "end", gid + "|d", text="…")
            else:
                kind, entry_id = spec
                text, values, has_kids = node_row(entry_id)
                iid = tree.insert(parent_iid, "end", f"e{entry_id}", text=text, values=values)
                if has_kids:
                    tree.insert(iid, "end", f"e{entry_id}|d", text="…")

    def populate(iid: str):
        if iid in state["populated"]:
            return
        state["populated"].add(iid)
        for c in tree.get_children(iid):
            if c.endswith("|d"):
                tree.delete(c)
        if iid.startswith("g"):
            members = state["groups"][iid]
            kind = "dir" if tree.set(iid, "type").startswith("dir") else "file"
            insert_specs(iid, [(kind, i) for i in members])
        else:
            insert_specs(iid, state["model"].listing(int(iid[1:]), group_min()))

    def reload_tree():
        if state["model"] is None or state["scan_id"] is None:
            return
        tree.delete(*tree.get_children(""))
        state["groups"].clear()
        state["populated"] = set()
        insert_specs("", state["model"].listing(state["model"].root_id, group_min()))

    def show_scan(idx: int):
        m = state["model"]
        s = m.scans[idx]
        root.config(cursor="watch")
        root.update_idletasks()
        m.load_scan(s["scan_id"])
        root.config(cursor="")
        state["scan_id"] = s["scan_id"]
        reload_tree()
        status.config(text=f"{s['drive']} {s['label'] or ''} — {s['file_count']:,} files, "
                           f"{s['dir_count']:,} folders, {human(s['byte_total'])}, "
                           f"scanned {s['started_utc']}")

    def open_db(path: str):
        try:
            state["model"] = Model(path)
        except Exception as e:
            messagebox.showerror("quick census viewer", f"Cannot open {path}:\n{e}")
            return
        m = state["model"]
        scan_box["values"] = [
            f"scan {s['scan_id']}: {s['drive']} {s['label'] or ''} — "
            f"{(s['file_count'] or 0):,} files, {human(s['byte_total'] or 0)} ({(s['started_utc'] or '')[:10]})"
            for s in m.scans]
        scan_box.current(len(m.scans) - 1)
        root.title(f"quick census viewer — {os.path.basename(path)}")
        show_scan(len(m.scans) - 1)

    def pick_file():
        p = filedialog.askopenfilename(
            parent=root, title="Open census catalog",
            initialdir=os.path.dirname(os.path.abspath(__file__)),
            filetypes=[("SQLite catalog", "*.sqlite"), ("All files", "*.*")])
        if p:
            open_db(p)

    scan_box.bind("<<ComboboxSelected>>", lambda e: show_scan(scan_box.current()))
    tree.bind("<<TreeviewOpen>>", lambda e: populate(tree.focus()))

    def on_select(_e):
        iid = tree.focus()
        if not iid or state["model"] is None:
            return
        if iid.startswith("e"):
            m = state["model"]
            entry_id = int(iid.split("|")[0][1:])
            r = m.info[entry_id]
            extra = "" if r["is_dir"] else f"  ({(r['size_bytes'] or 0):,} bytes)"
            status.config(text=m.path_of(entry_id) + extra)
        elif iid.startswith("g"):
            n = len(state["groups"].get(iid.split("|")[0], []))
            status.config(text=f"pattern group: {n} entries with the same name shape "
                               f"(digits collapsed to #) — expand to list them")
    tree.bind("<<TreeviewSelect>>", on_select)

    if db_path:
        open_db(db_path)
    root.mainloop()


def main() -> int:
    if os.name != "nt":
        print("qcview.py is Windows-only (matches qc.py catalogs)", file=sys.stderr)
        return 2
    db = sys.argv[1] if len(sys.argv) > 1 else None
    if db and not os.path.exists(db):
        print(f"not found: {db}", file=sys.stderr)
        return 2
    run_viewer(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
