#!/usr/bin/env python3
# Copyright (c) 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Read and write the orphan branch that holds firmware size history.

Layout on the branch (default `firmware-size-data`):

    data/<yyyy>/<sha>.json   one commit, every board, every image
    index.json               ordered [{sha, date, tag, source, path}]
    schema.json              description of the measurement record
    index.html               static trend page

Writes go through git plumbing (hash-object / read-tree / write-tree /
commit-tree) rather than a worktree, so nothing is ever checked out and the
branch can be created from nothing. Data written here is permanent, which is
the point: CI artifacts expire after 90 days, so a measurement only survives if
it is committed to this branch before then.

Usage:
    scripts/size/store.py put --data sizes.json
    scripts/size/store.py get --sha <sha> -o base.json
    scripts/size/store.py nearest --from <sha>
    scripts/size/store.py list
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_BRANCH = "firmware-size-data"
REPO_ROOT = Path(__file__).resolve().parents[2]

SCHEMA_DOC = {
    "schema": 1,
    "description": "Firmware size measurements, one file per commit.",
    "measurement": {
        "build": "label for the build invocation, e.g. smc-sysbuild",
        "board": "board revision, e.g. p150a",
        "image": "sysbuild image name, e.g. smc / recovery / mcuboot / dmc",
        "single_pool": "true when every allocated section lives in SRAM (no XIP)",
        "sections": "text/rodata/data/bss/noinit/tls, informational only",
        "measured": {
            "stored": "sum of PT_LOAD p_filesz; equals zephyr.bin; occupies flash",
            "resident": "allocated sections inside the SRAM region; includes bss and noinit",
            "bin": "size of zephyr.bin",
            "signed": "size of zephyr.signed.bin, when the image is signed",
            "placed": "size of the file tt-boot-fs writes into the partition",
        },
        "budgets": "every applicable ceiling, each naming the metric it bounds",
        "binding": "the ceiling with the fewest bytes of headroom",
    },
}


def run(args: list[str], *, env: dict[str, str] | None = None, check: bool = True) -> str:
    res = subprocess.run(args, capture_output=True, text=True, env=env, check=False)
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


class Store:
    def __init__(self, repo: Path, branch: str = DEFAULT_BRANCH) -> None:
        self.repo = repo
        self.branch = branch

    def _git(self, *args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
        return run(["git", "-C", str(self.repo), *args], env=env, check=check)

    def head(self) -> str | None:
        out = self._git("rev-parse", "--verify", "--quiet", self.branch, check=False).strip()
        return out or None

    def read_file(self, path: str) -> str | None:
        if self.head() is None:
            return None
        res = subprocess.run(
            ["git", "-C", str(self.repo), "cat-file", "-p", f"{self.branch}:{path}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return res.stdout if res.returncode == 0 else None

    def read_index(self) -> list[dict[str, Any]]:
        raw = self.read_file("index.json")
        if not raw:
            return []
        try:
            data: list[dict[str, Any]] = json.loads(raw)
            return data
        except json.JSONDecodeError:
            return []

    @staticmethod
    def data_path(sha: str, date: str) -> str:
        year = date[:4] if len(date) >= 4 and date[:4].isdigit() else "unknown"
        return f"data/{year}/{sha}.json"

    def get(self, sha: str) -> dict[str, Any] | None:
        for entry in self.read_index():
            if entry["sha"].startswith(sha):
                raw = self.read_file(entry["path"])
                if raw:
                    doc: dict[str, Any] = json.loads(raw)
                    return doc
        return None

    def commit_files(self, files: dict[str, str], message: str) -> str:
        """Create a commit on the branch containing `files` (path -> content)."""
        parent = self.head()
        env = dict(os.environ)
        env.setdefault("GIT_AUTHOR_NAME", "tt-size-bot")
        env.setdefault("GIT_AUTHOR_EMAIL", "tt-size-bot@users.noreply.github.com")
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])

        with tempfile.TemporaryDirectory() as tmp:
            env["GIT_INDEX_FILE"] = str(Path(tmp) / "index")
            if parent:
                self._git("read-tree", parent, env=env)

            for path, content in files.items():
                blob_file = Path(tmp) / "blob"
                blob_file.write_text(content, encoding="utf-8")
                blob = self._git("hash-object", "-w", str(blob_file), env=env).strip()
                self._git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)

            tree = self._git("write-tree", env=env).strip()

        args = ["commit-tree", tree, "-m", message]
        if parent:
            args += ["-p", parent]
        commit = self._git(*args, env=env).strip()

        ref = f"refs/heads/{self.branch}"
        if parent:
            self._git("update-ref", ref, commit, parent)
        else:
            self._git("update-ref", ref, commit)
        return commit

    def put(self, doc: dict[str, Any], *, message: str | None = None) -> str:
        commit = doc["commit"]
        sha, date = commit["sha"], commit.get("date", "")
        path = self.data_path(sha, date)

        # Merge with anything already stored for this commit, so per-board CI
        # jobs can land independently without clobbering each other.
        existing_raw = self.read_file(path)
        if existing_raw:
            existing = json.loads(existing_raw)
            merged: dict[tuple[str, str, str], dict[str, Any]] = {
                (str(r.get("board")), str(r.get("build")), str(r.get("image"))): r
                for r in existing.get("measurements", [])
            }
            for row in doc.get("measurements", []):
                merged[(str(row.get("board")), str(row.get("build")), str(row.get("image")))] = row
            doc = dict(doc)
            doc["measurements"] = [merged[k] for k in sorted(merged)]

        files = {path: json.dumps(doc, indent=2) + "\n"}

        entries = [e for e in self.read_index() if e["sha"] != sha]
        entries.append(
            {
                "sha": sha,
                "date": date,
                "tag": commit.get("tag"),
                "source": doc.get("source", {}).get("kind"),
                "images": len(doc.get("measurements", [])),
                "path": path,
            }
        )
        entries.sort(key=lambda e: (e.get("date") or "", e["sha"]))
        files["index.json"] = json.dumps(entries, indent=2) + "\n"

        if self.read_file("schema.json") is None:
            files["schema.json"] = json.dumps(SCHEMA_DOC, indent=2) + "\n"
        if self.read_file("index.html") is None:
            files["index.html"] = TREND_PAGE

        subject = commit.get("subject", "")[:60]
        msg = message or f"size: {sha[:12]} {subject}"
        return self.commit_files(files, msg)

    def nearest(self, from_sha: str, source_repo: Path) -> dict[str, Any] | None:
        """Walk first-parent history back from `from_sha` to the newest stored commit."""
        known = {e["sha"]: e for e in self.read_index()}
        if not known:
            return None
        out = run(
            ["git", "-C", str(source_repo), "rev-list", "--first-parent",
             "--max-count=500", from_sha],
            check=False,
        )
        for line in out.splitlines():
            entry = known.get(line.strip())
            if entry:
                return entry
        return None


TREND_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Firmware size history</title>
<style>
  body { font: 14px system-ui, sans-serif; margin: 2rem; max-width: 70rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border-bottom: 1px solid #ddd; padding: .35rem .6rem; text-align: right; }
  th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
  .bar { height: .6rem; background: #e5e7eb; border-radius: 3px; overflow: hidden; }
  .bar > i { display: block; height: 100%; background: #2563eb; }
  .over > i { background: #dc2626; }
  select { font: inherit; margin-right: 1rem; }
</style>
<h1>Firmware size history</h1>
<p>
  <select id="commit"></select>
  <span id="meta"></span>
</p>
<table><thead><tr>
  <th>Board</th><th>Image</th><th>stored</th><th>resident</th>
  <th>Binding</th><th>Used</th><th></th>
</tr></thead><tbody id="rows"></tbody></table>
<script>
const fmt = n => n == null ? '\\u2014' : n.toLocaleString();
async function j(p) { const r = await fetch(p); if (!r.ok) throw new Error(p); return r.json(); }
(async () => {
  const idx = await j('index.json');
  const sel = document.getElementById('commit');
  idx.slice().reverse().forEach(e => {
    const o = document.createElement('option');
    o.value = e.path;
    o.textContent = `${(e.date||'').slice(0,10)}  ${e.sha.slice(0,12)}`
                  + (e.tag ? `  (${e.tag})` : '');
    sel.append(o);
  });
  async function show() {
    const doc = await j(sel.value);
    document.getElementById('meta').textContent =
      `${doc.commit.subject || ''} \\u00b7 ${doc.source?.kind || ''}`;
    const tb = document.getElementById('rows');
    tb.textContent = '';
    for (const m of doc.measurements) {
      const b = (m.budgets || []).find(x => x.name === m.binding);
      const used = b ? (m.measured[b.metric] || 0) / b.limit : 0;
      const tr = document.createElement('tr');
      for (const cell of [m.board, m.image, fmt(m.measured.stored), fmt(m.measured.resident),
                          m.binding || '\\u2014', b ? (100*used).toFixed(1) + '%' : '\\u2014']) {
        const td = document.createElement('td'); td.textContent = cell; tr.append(td);
      }
      const td = document.createElement('td');
      td.innerHTML = `<span class="bar${used>1?' over':''}"><i style="width:${
        Math.min(100, used*100).toFixed(1)}%"></i></span>`;
      tr.append(td); tb.append(tr);
    }
  }
  sel.addEventListener('change', show);
  if (idx.length) await show();
})();
</script>
"""


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo", type=Path, default=REPO_ROOT)
    ap.add_argument("--branch", default=DEFAULT_BRANCH)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_put = sub.add_parser("put", help="Append a collection to the data branch.")
    p_put.add_argument("--data", required=True, type=Path)
    p_put.add_argument("--message", default=None)

    p_get = sub.add_parser("get", help="Print the stored collection for a commit.")
    p_get.add_argument("--sha", required=True)
    p_get.add_argument("-o", "--output", type=Path, default=None)

    p_near = sub.add_parser("nearest", help="Newest stored ancestor of a commit.")
    p_near.add_argument("--from", dest="from_sha", required=True)
    p_near.add_argument("--source-repo", type=Path, default=None)
    p_near.add_argument("-o", "--output", type=Path, default=None,
                        help="Write that commit's collection here as well.")

    sub.add_parser("list", help="Print the index.")

    args = ap.parse_args()
    store = Store(args.repo, args.branch)

    if args.cmd == "put":
        doc = json.loads(args.data.read_text(encoding="utf-8"))
        commit = store.put(doc, message=args.message)
        print(f"{args.branch} <- {commit[:12]} ({len(doc.get('measurements', []))} measurements)")
        return 0

    if args.cmd == "get":
        doc = store.get(args.sha)
        if doc is None:
            print(f"no data stored for {args.sha}", file=sys.stderr)
            return 1
        text = json.dumps(doc, indent=2) + "\n"
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0

    if args.cmd == "nearest":
        entry = store.nearest(args.from_sha, args.source_repo or args.repo)
        if entry is None:
            print("no stored ancestor found", file=sys.stderr)
            return 1
        print(entry["sha"])
        if args.output:
            doc = store.get(entry["sha"])
            if doc is None:
                return 1
            args.output.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        return 0

    for entry in store.read_index():
        tag = f" ({entry['tag']})" if entry.get("tag") else ""
        print(f"{entry['sha'][:12]}  {entry.get('date', ''):25}  {entry.get('images', 0):3} images"
              f"  {entry.get('source', '')}{tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
