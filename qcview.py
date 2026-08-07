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

    def scan_paths(self, scan_id: int) -> dict[str, tuple[str, bool, int, int]]:
        """rel-path(casefold) -> (display rel path, is_dir, size, mtime) for one scan.
        Paths are relative to the drive root so scans of re-lettered drives still compare."""
        rows = self.con.execute(
            "SELECT entry_id, parent_id, name, is_dir, size_bytes, mtime_ns "
            "FROM entry WHERE scan_id=? ORDER BY depth ASC", (scan_id,)).fetchall()
        rel: dict[int, str] = {}
        out: dict[str, tuple[str, bool, int, int]] = {}
        for r in rows:
            if r["parent_id"] is None:
                rel[r["entry_id"]] = ""
                continue
            p = rel[r["parent_id"]]
            mine = f"{p}\\{r['name']}" if p else r["name"]
            if r["is_dir"]:
                rel[r["entry_id"]] = mine
            out[mine.casefold()] = (mine, bool(r["is_dir"]), r["size_bytes"] or 0, r["mtime_ns"] or 0)
        return out

    def diff_scans(self, scan_a: int, scan_b: int) -> dict:
        """Path-keyed A->B comparison. Statuses: added (in B only), removed (in A only),
        changed (file in both, size or mtime differs). Directory mtime drift is ignored
        (it changes whenever children change — pure noise at this level)."""
        a = self.scan_paths(scan_a)
        b = self.scan_paths(scan_b)
        changes = []
        for key, (disp, is_dir, size_b, mtime_b) in b.items():
            if key not in a:
                changes.append({"rel": disp, "is_dir": is_dir, "status": "added",
                                "size_a": None, "size_b": size_b, "mtime_b": mtime_b})
            elif not is_dir:
                _, _, size_a, mtime_a = a[key]
                if size_a != size_b or mtime_a != mtime_b:
                    changes.append({"rel": disp, "is_dir": False, "status": "changed",
                                    "size_a": size_a, "size_b": size_b, "mtime_b": mtime_b})
        for key, (disp, is_dir, size_a, mtime_a) in a.items():
            if key not in b:
                changes.append({"rel": disp, "is_dir": is_dir, "status": "removed",
                                "size_a": size_a, "size_b": None, "mtime_b": mtime_a})
        n_add = sum(1 for c in changes if c["status"] == "added" and not c["is_dir"])
        n_rem = sum(1 for c in changes if c["status"] == "removed" and not c["is_dir"])
        n_chg = sum(1 for c in changes if c["status"] == "changed")
        net = (sum(c["size_b"] for c in changes if c["status"] == "added" and not c["is_dir"])
               - sum(c["size_a"] for c in changes if c["status"] == "removed" and not c["is_dir"])
               + sum(c["size_b"] - c["size_a"] for c in changes if c["status"] == "changed"))
        return {"changes": changes,
                "summary": {"added": n_add, "removed": n_rem, "changed": n_chg, "net": net}}


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
    diff_btn = tk.Button(bar, text="Diff…", state="disabled", command=lambda: open_diff())
    diff_btn.pack(side="left", padx=(14, 0))

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

    def scan_label(s) -> str:
        return (f"scan {s['scan_id']}: {s['drive']} {s['label'] or ''} — "
                f"{(s['file_count'] or 0):,} files, {human(s['byte_total'] or 0)} "
                f"({(s['started_utc'] or '')[:10]})")

    def open_db(path: str):
        try:
            state["model"] = Model(path)
        except Exception as e:
            messagebox.showerror("quick census viewer", f"Cannot open {path}:\n{e}")
            return
        m = state["model"]
        scan_box["values"] = [scan_label(s) for s in m.scans]
        scan_box.current(len(m.scans) - 1)
        diff_btn.config(state="normal" if len(m.scans) >= 2 else "disabled")
        root.title(f"quick census viewer — {os.path.basename(path)}")
        show_scan(len(m.scans) - 1)

    def signed(n: int) -> str:
        return ("+" if n >= 0 else "−") + human(abs(n))

    def open_diff():
        m = state["model"]
        dlg = tk.Toplevel(root)
        dlg.title("Compare scans")
        dlg.transient(root)
        labels = [scan_label(s) for s in m.scans]
        tk.Label(dlg, text="A (before):").grid(row=0, column=0, sticky="e", padx=8, pady=6)
        tk.Label(dlg, text="B (after):").grid(row=1, column=0, sticky="e", padx=8)
        va, vb = tk.StringVar(), tk.StringVar()
        ca = ttk.Combobox(dlg, textvariable=va, values=labels, width=60, state="readonly")
        cb = ttk.Combobox(dlg, textvariable=vb, values=labels, width=60, state="readonly")
        ca.grid(row=0, column=1, padx=8, pady=6)
        cb.grid(row=1, column=1, padx=8)
        ca.current(len(m.scans) - 2)
        cb.current(len(m.scans) - 1)
        warn = tk.Label(dlg, text="", fg="#9a6700")
        warn.grid(row=2, column=0, columnspan=2)

        def note(*_):
            sa, sb = m.scans[ca.current()], m.scans[cb.current()]
            warn.config(text="different drives — comparing relative structure only"
                        if sa["drive"] != sb["drive"] else "")
        ca.bind("<<ComboboxSelected>>", note)
        cb.bind("<<ComboboxSelected>>", note)
        note()

        def go():
            ia, ib = ca.current(), cb.current()
            dlg.destroy()
            if ia == ib:
                messagebox.showinfo("Compare scans", "Pick two different scans.")
                return
            show_diff(ia, ib)
        tk.Button(dlg, text="Compare", width=12, command=go).grid(row=3, column=1, sticky="e",
                                                                  padx=8, pady=8)

    def show_diff(idx_a: int, idx_b: int):
        m = state["model"]
        sa, sb = m.scans[idx_a], m.scans[idx_b]
        root.config(cursor="watch")
        root.update_idletasks()
        result = m.diff_scans(sa["scan_id"], sb["scan_id"])
        root.config(cursor="")

        win = tk.Toplevel(root)
        win.title(f"diff: scan {sa['scan_id']} → scan {sb['scan_id']}")
        win.geometry("980x600")
        s = result["summary"]
        tk.Label(win, anchor="w",
                 text=f"{scan_label(sa)}   →   {scan_label(sb)}").pack(fill="x", padx=8, pady=(6, 0))
        tk.Label(win, anchor="w", font=("Segoe UI", 10, "bold"),
                 text=f"+{s['added']} added   −{s['removed']} removed   ~{s['changed']} changed"
                      f"   net {signed(s['net'])}").pack(fill="x", padx=8)

        fbar = tk.Frame(win)
        fbar.pack(fill="x", padx=8)
        shows = {k: tk.BooleanVar(value=True) for k in ("added", "removed", "changed")}
        for k, v in shows.items():
            tk.Checkbutton(fbar, text=f"show {k}", variable=v,
                           command=lambda: rebuild()).pack(side="left")

        dtree = ttk.Treeview(win, columns=("status", "type", "size", "modified"), selectmode="browse")
        dtree.heading("#0", text="Name", anchor="w")
        for col, txt, wdt, anch in (("status", "Status", 110, "w"), ("type", "Type", 80, "w"),
                                    ("size", "Size", 170, "e"), ("modified", "Modified", 120, "w")):
            dtree.heading(col, text=txt, anchor=anch)
            dtree.column(col, width=wdt, anchor=anch, stretch=False)
        dtree.column("#0", width=430)
        dtree.tag_configure("added", foreground="#1a7f37")
        dtree.tag_configure("removed", foreground="#cf222e")
        dtree.tag_configure("changed", foreground="#9a6700")
        dys = ttk.Scrollbar(win, orient="vertical", command=dtree.yview)
        dtree.configure(yscrollcommand=dys.set)
        dys.pack(side="right", fill="y")
        dtree.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        def rebuild():
            dtree.delete(*dtree.get_children(""))
            active = [c for c in result["changes"] if shows[c["status"]].get()]
            active.sort(key=lambda c: natural_key(c["rel"]))
            nodes: dict[str, str] = {}      # rel casefold -> iid
            containers: dict[str, list] = {}  # pure ancestor rows -> [a, r, c, net]

            def ensure_chain(rel_display: str) -> str:
                """Create/find container rows for every ancestor of rel; returns parent iid."""
                parts = rel_display.split("\\")
                parent = ""
                sofar = ""
                for seg in parts[:-1]:
                    sofar = f"{sofar}\\{seg}" if sofar else seg
                    key = sofar.casefold()
                    if key not in nodes:
                        iid = dtree.insert(parent, "end", f"d|{key}", text="📁 " + seg,
                                           values=("", "folder", "", ""))
                        nodes[key] = iid
                        containers[key] = [0, 0, 0, 0]
                    parent = nodes[key]
                return parent

            for c in active:
                parent = ensure_chain(c["rel"])
                name = c["rel"].rsplit("\\", 1)[-1]
                # roll file-level churn into every ancestor container
                if not c["is_dir"] or c["status"] != "changed":
                    sofar = ""
                    for seg in c["rel"].split("\\")[:-1]:
                        sofar = f"{sofar}\\{seg}" if sofar else seg
                        agg = containers.get(sofar.casefold())
                        if agg is not None:
                            if not c["is_dir"]:
                                if c["status"] == "added":
                                    agg[0] += 1
                                    agg[3] += c["size_b"]
                                elif c["status"] == "removed":
                                    agg[1] += 1
                                    agg[3] -= c["size_a"]
                                else:
                                    agg[2] += 1
                                    agg[3] += c["size_b"] - c["size_a"]
                if c["status"] == "changed":
                    size_txt = f"{human(c['size_a'])} → {human(c['size_b'])}"
                elif c["status"] == "added":
                    size_txt = "" if c["is_dir"] else human(c["size_b"])
                else:
                    size_txt = "" if c["is_dir"] else human(c["size_a"])
                icon = "📁 " if c["is_dir"] else ""
                typ = "folder" if c["is_dir"] else (os.path.splitext(name)[1][1:].lower() or "file")
                key = c["rel"].casefold()
                iid = dtree.insert(parent, "end", f"c|{key}", text=icon + name,
                                   values=(c["status"], typ, size_txt, fmt_dt(c["mtime_b"])),
                                   tags=(c["status"],))
                nodes[key] = iid  # children of an added/removed dir nest under its colored row

            for key, (a, r, ch, net) in containers.items():
                badge = " ".join(x for x in (f"+{a}" if a else "", f"−{r}" if r else "",
                                             f"~{ch}" if ch else "") if x)
                dtree.set(nodes[key], "status", badge)
                if net:
                    dtree.set(nodes[key], "size", signed(net))

            def open_all(iid, depth):
                if depth <= 0:
                    return
                dtree.item(iid, open=True)
                for k in dtree.get_children(iid):
                    open_all(k, depth - 1)
            depth = 99 if len(active) <= 400 else 2
            for k in dtree.get_children(""):
                open_all(k, depth)

        rebuild()

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
