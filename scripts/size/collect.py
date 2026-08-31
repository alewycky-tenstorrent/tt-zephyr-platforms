#!/usr/bin/env python3
# Copyright (c) 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Collect firmware size measurements from one or more Zephyr build directories.

Images are discovered by looking for */zephyr/zephyr.elf (a sysbuild tree) or
zephyr/zephyr.elf (a plain build). Nothing enumerates image names, so adding a
board to .github/boards.json or an image to app/smc/sysbuild.cmake shows up here
with no change to this script.

Everything read here is already produced by an ordinary build and already
uploaded by .github/workflows/build-fw.yml, so collection never needs the build
to be modified or repeated.

Usage:
    scripts/size/collect.py --build-dir build-p150a --board p150a -o sizes.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from budgets import (
    Placement,
    binding_budget,
    config_int,
    find_slot1_size,
    parse_config,
    read_placements,
    resolve_budgets,
)
from elfsize import measure_elf

SCHEMA_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parents[2]


def default_zephyr_base() -> Path:
    env = os.environ.get("ZEPHYR_BASE")
    return Path(env) if env else REPO_ROOT.parent / "zephyr"


def discover_images(build_dir: Path) -> list[tuple[str, Path]]:
    """Return [(image_name, <dir containing zephyr.elf>)] for a build tree."""
    plain = build_dir / "zephyr" / "zephyr.elf"
    if plain.is_file():
        return [("main", build_dir / "zephyr")]

    found: list[tuple[str, Path]] = []
    for child in sorted(build_dir.iterdir()):
        if not child.is_dir():
            continue
        elf = child / "zephyr" / "zephyr.elf"
        if elf.is_file():
            found.append((child.name, child / "zephyr"))
    return found


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_info(repo: Path, ref: str) -> dict[str, Any]:
    sha, date, subject = git(repo, "log", "-1", "--format=%H%n%cI%n%s", ref).split("\n", 2)
    tags = git(repo, "tag", "--points-at", sha).split()
    release = next((t for t in tags if t.startswith("v") and "rc" not in t), None)
    return {"sha": sha, "date": date, "subject": subject, "tag": release}


def measure_build(
    build_dir: Path,
    build_name: str,
    board: str | None,
    zephyr_base: Path,
) -> list[dict[str, Any]]:
    images = discover_images(build_dir)
    if not images:
        print(f"warning: no images found under {build_dir}", file=sys.stderr)
        return []

    configs = {name: parse_config(d / ".config") for name, d in images if (d / ".config").is_file()}

    # The boot filesystem table is duplicated into every image's devicetree that
    # includes tt_blackhole_fixed_partitions.dtsi; the first one found wins.
    placements: dict[str, Placement] = {}
    ramload_size: int | None = None
    for name, image_dir in images:
        dts = image_dir / "zephyr.dts"
        if not placements and dts.is_file():
            placements = read_placements(dts, zephyr_base)
        image_cfg = configs.get(name, {})
        if "CONFIG_BOOT_IMAGE_EXECUTABLE_RAM_SIZE" in image_cfg:
            ramload_size = config_int(image_cfg, "CONFIG_BOOT_IMAGE_EXECUTABLE_RAM_SIZE")

    if board is None:
        for name, _ in images:
            rev = configs.get(name, {}).get("CONFIG_BOARD_REVISION")
            if rev:
                board = rev
                break

    rows: list[dict[str, Any]] = []
    for name, image_dir in images:
        cfg = configs.get(name)
        if cfg is None:
            print(f"warning: {name} has no .config, skipping", file=sys.stderr)
            continue

        sram_base = config_int(cfg, "CONFIG_SRAM_BASE_ADDRESS")
        sram_size = config_int(cfg, "CONFIG_SRAM_SIZE") * 1024
        size = measure_elf(image_dir / "zephyr.elf", sram_base, sram_size)

        signed_bin = image_dir / "zephyr.signed.bin"
        raw_bin = image_dir / "zephyr.bin"
        measured: dict[str, int] = {"stored": size.stored, "resident": size.resident}
        if raw_bin.is_file():
            measured["bin"] = raw_bin.stat().st_size
        if signed_bin.is_file():
            measured["signed"] = signed_bin.stat().st_size

        placement = placements.get(name)
        if placement is not None:
            placed = image_dir / placement.filename
            if placed.is_file():
                measured["placed"] = placed.stat().st_size

        dts = image_dir / "zephyr.dts"
        slot1 = find_slot1_size(dts, zephyr_base) if dts.is_file() else None

        budgets = resolve_budgets(
            cfg,
            slot1_size=slot1,
            placement=placement,
            ramload_size=ramload_size,
            signed=signed_bin.is_file(),
        )
        binding = binding_budget(budgets, measured)

        rows.append(
            {
                "build": build_name,
                "board": board,
                "image": name,
                "board_target": cfg.get("CONFIG_BOARD_TARGET"),
                "soc": cfg.get("CONFIG_SOC"),
                "single_pool": size.single_pool,
                "sections": size.sections.__dict__,
                "measured": measured,
                "budgets": [b.as_dict() for b in budgets],
                "binding": binding.name if binding else None,
                "partition": placement.partition if placement else None,
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--build-dir", action="append", required=True, type=Path,
                    help="Build directory to measure. Repeatable.")
    ap.add_argument("--build-name", action="append", default=None,
                    help="Label for the matching --build-dir, or one label for all of them. "
                         "Defaults to the directory basename.")
    ap.add_argument("--board", default=None, help="Board revision. Inferred if omitted.")
    ap.add_argument("--repo", type=Path, default=REPO_ROOT,
                    help="Repo to read commit metadata from.")
    ap.add_argument("--ref", default="HEAD", help="Commit to attribute the measurements to.")
    ap.add_argument("--source-kind", default="local",
                    choices=["ci-artifact", "local-backfill", "local"],
                    help="Provenance. Locally built numbers do not match CI byte for byte.")
    ap.add_argument("--run-id", default=None, help="CI run id, recorded in the provenance block.")
    ap.add_argument("--zephyr-base", type=Path, default=None)
    ap.add_argument("-o", "--output", type=Path, default=None, help="Defaults to stdout.")
    args = ap.parse_args()

    zephyr_base = args.zephyr_base or default_zephyr_base()
    names = args.build_name or []

    measurements: list[dict[str, Any]] = []
    for i, build_dir in enumerate(args.build_dir):
        if not build_dir.is_dir():
            print(f"warning: {build_dir} is not a directory, skipping", file=sys.stderr)
            continue
        if len(names) == 1:
            name = names[0]  # one label applies to every directory
        elif i < len(names):
            name = names[i]
        else:
            name = build_dir.name
        measurements.extend(measure_build(build_dir, name, args.board, zephyr_base))

    # (board, build, image) is the identity used by the store and the report, so
    # a repeat means one measurement would silently replace another -- usually a
    # board that could not be inferred, or the same tree passed twice.
    seen: set[tuple[str, str, str]] = set()
    for row in measurements:
        key = (str(row["board"]), str(row["build"]), str(row["image"]))
        if key in seen:
            print(f"warning: duplicate measurement for {key[0]}/{key[2]}; "
                  f"pass --board to disambiguate", file=sys.stderr)
        seen.add(key)

    doc = {
        "schema": SCHEMA_VERSION,
        "commit": commit_info(args.repo, args.ref),
        "source": {
            "kind": args.source_kind,
            "run_id": args.run_id,
            "collected": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        "measurements": measurements,
    }

    text = json.dumps(doc, indent=2, sort_keys=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote {len(measurements)} measurements to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
