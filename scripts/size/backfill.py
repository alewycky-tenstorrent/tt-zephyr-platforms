#!/usr/bin/env python3
# Copyright (c) 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Rebuild historical commits and record their firmware sizes.

Intended for the 43 non-rc `v*` release tags, which give a clean long-term
trend for roughly 470 sysbuilds. Every commit is not worth it: history is 2211
linear commits over 17 months.

Two things to know before running this:

  * It mutates a west workspace. Each ref needs its own `west update`, so refs
    are processed serially and the workspace must not be one you are working
    in. Point --workspace at a dedicated checkout. --path-cache is strongly
    recommended or manifest churn dominates the runtime.

  * Numbers produced here will not match CI byte for byte: different SDK
    version, different host, and images are unsigned unless a key is supplied.
    They are recorded as `source.kind = local-backfill` so the trend page can
    render them as a separate series. Do not read a toolchain step as a code
    regression.

Refs that fail to build are recorded as gaps and the run continues; old tags
are not guaranteed to build with the current SDK.

Usage:
    scripts/size/backfill.py --workspace ~/bh-zephyr-backfill --list
    scripts/size/backfill.py --workspace ~/bh-zephyr-backfill --path-cache ~/west-cache
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from collect import commit_info, measure_build
from store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
FALLBACK_BOARDS = ["p100a", "p150a", "p300a"]


def run(args: list[str], cwd: Path, *, check: bool = True, quiet: bool = False) -> int:
    if not quiet:
        print(f"    $ {' '.join(args)}", flush=True)
    res = subprocess.run(args, cwd=str(cwd), check=False,
                         capture_output=quiet, text=True)
    if check and res.returncode != 0:
        if quiet and res.stderr:
            print(res.stderr[-4000:], file=sys.stderr)
        raise RuntimeError(f"command failed ({res.returncode}): {' '.join(args)}")
    return res.returncode


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def release_tags(repo: Path) -> list[str]:
    tags = git_out(repo, "tag", "-l", "v*", "--sort=creatordate").split()
    return [t for t in tags if "rc" not in t]


def boards_for(manifest_repo: Path) -> list[str]:
    path = manifest_repo / ".github" / "boards.json"
    if path.is_file():
        try:
            data: list[str] = json.loads(path.read_text(encoding="utf-8"))
            return data
        except json.JSONDecodeError:
            pass
    return FALLBACK_BOARDS


def board_target(manifest_repo: Path, board: str) -> str:
    script = manifest_repo / "scripts" / "rev2board.sh"
    if script.is_file():
        try:
            return subprocess.run([str(script), board, "smc"], check=True,
                                  capture_output=True, text=True).stdout.strip()
        except subprocess.CalledProcessError:
            pass
    return f"tt_blackhole@{board}/tt_blackhole/smc"


def build_one(workspace: Path, manifest_repo: Path, board: str, jobs: int) -> Path | None:
    build_dir = workspace / f"build-size-{board}"
    shutil.rmtree(build_dir, ignore_errors=True)
    target = board_target(manifest_repo, board)
    try:
        run(
            [
                "west", "build", "-d", str(build_dir), "-p", "always",
                "--sysbuild", "-b", target,
                str(manifest_repo / "app" / "smc"),
                "--", f"-DCMAKE_BUILD_PARALLEL_LEVEL={jobs}",
            ],
            cwd=workspace,
            quiet=True,
        )
    except RuntimeError as exc:
        print(f"    ! {board}: {exc}", file=sys.stderr)
        return None
    return build_dir


def process_ref(
    ref: str,
    workspace: Path,
    manifest_repo: Path,
    store: Store,
    zephyr_base: Path,
    boards: list[str],
    jobs: int,
    path_cache: Path | None,
) -> bool:
    print(f"==> {ref}", flush=True)
    started = time.monotonic()

    run(["git", "-C", str(manifest_repo), "checkout", "--detach", "--force", ref], cwd=workspace)
    update = ["west", "update", "--narrow"]
    if path_cache is not None:
        update += ["--path-cache", str(path_cache)]
    run(update, cwd=workspace, quiet=True)

    ref_boards = boards or boards_for(manifest_repo)
    measurements: list[dict[str, Any]] = []
    built = 0
    for board in ref_boards:
        build_dir = build_one(workspace, manifest_repo, board, jobs)
        if build_dir is None:
            continue
        built += 1
        measurements.extend(
            measure_build(build_dir, "smc-sysbuild", board, zephyr_base)
        )
        shutil.rmtree(build_dir, ignore_errors=True)

    if not measurements:
        print(f"    ! {ref}: nothing built, recording a gap", file=sys.stderr)
        return False

    doc = {
        "schema": 1,
        "commit": commit_info(manifest_repo, "HEAD"),
        "source": {"kind": "local-backfill", "ref": ref, "boards_built": built,
                   "boards_requested": len(ref_boards)},
        "measurements": measurements,
    }
    store.put(doc, message=f"size: backfill {ref} ({built}/{len(ref_boards)} boards)")
    print(f"    ok {ref}: {len(measurements)} measurements from {built}/{len(ref_boards)} boards "
          f"in {time.monotonic() - started:.0f}s", flush=True)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--workspace", type=Path, required=True,
                    help="West topdir of a workspace dedicated to backfilling.")
    ap.add_argument("--manifest-repo", type=Path, default=None,
                    help="Defaults to <workspace>/tt-system-firmware.")
    ap.add_argument("--store-repo", type=Path, default=REPO_ROOT,
                    help="Repo holding the data branch. Defaults to this checkout.")
    ap.add_argument("--branch", default="firmware-size-data")
    ap.add_argument("--ref", action="append", default=None,
                    help="Explicit ref to build. Repeatable. Defaults to all non-rc v* tags.")
    ap.add_argument("--board", action="append", default=None,
                    help="Board revision. Repeatable. Defaults to the ref's boards.json.")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--path-cache", type=Path, default=None,
                    help="Passed to `west update`. Without it manifest churn dominates.")
    ap.add_argument("--zephyr-base", type=Path, default=None)
    ap.add_argument("--redo", action="store_true", help="Rebuild refs already in the index.")
    ap.add_argument("--list", action="store_true", dest="list_only",
                    help="Print the refs that would be built, then exit.")
    args = ap.parse_args()

    workspace = args.workspace.expanduser().resolve()
    manifest_repo = (args.manifest_repo or workspace / "tt-system-firmware").resolve()
    zephyr_base = (args.zephyr_base or workspace / "zephyr").resolve()

    if not args.list_only:
        for path, what in ((workspace, "workspace"), (manifest_repo, "manifest repo")):
            if not path.is_dir():
                print(f"error: {what} {path} does not exist", file=sys.stderr)
                return 2

    source_repo = manifest_repo if manifest_repo.is_dir() else REPO_ROOT
    refs = args.ref or release_tags(source_repo)

    store = Store(args.store_repo, args.branch)
    if not args.redo:
        done = {e.get("source_ref") or e["sha"] for e in store.read_index()}
        tag_done = {e.get("tag") for e in store.read_index() if e.get("tag")}
        refs = [r for r in refs if r not in tag_done and r not in done]

    if args.list_only:
        for ref in refs:
            print(ref)
        print(f"\n{len(refs)} refs x {len(args.board or boards_for(source_repo))} boards",
              file=sys.stderr)
        return 0

    failures: list[str] = []
    for ref in refs:
        try:
            if not process_ref(ref, workspace, manifest_repo, store, zephyr_base,
                               args.board or [], args.jobs, args.path_cache):
                failures.append(ref)
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            print(f"    ! {ref}: {exc}", file=sys.stderr)
            failures.append(ref)

    print(f"\ndone: {len(refs) - len(failures)}/{len(refs)} refs recorded")
    if failures:
        print(f"gaps: {', '.join(failures)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
