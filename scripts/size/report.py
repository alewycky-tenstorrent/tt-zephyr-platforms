#!/usr/bin/env python3
# Copyright (c) 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Diff two size collections and render a Markdown report.

The report is warn-only by design: it always says what moved, and it raises a
GitHub Actions warning annotation when an image no longer fits, but it does not
fail the job unless --fail-on-overflow is passed. Today an overflow surfaces
only at `tt_boot_fs.py mkfs` as

    Range 22e000:29d800 overlaps with existing range 2740608:2744704

which names neither the image nor a size; the point of the warning is to say
"dmc exceeds its bootfs budget by 2,048 B" before that happens.

Usage:
    scripts/size/report.py --head head.json [--base base.json] -o report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Metrics reported as trend lines. `stored` and `resident` are the authoritative
# pair: stored is what occupies flash, resident is what occupies RAM and so
# includes bss and noinit. `placed` is the signed/raw file the boot filesystem
# actually writes, and is the one the bootfs budget applies to.
TREND_METRICS = ("stored", "resident", "placed")

METRIC_HELP = {
    "stored": "flash bytes (= zephyr.bin)",
    "resident": "RAM bytes, incl. bss+noinit",
    "placed": "bytes written to the partition",
    "signed": "signed image bytes",
}


def load(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data


def key_of(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("board")), str(row.get("build")), str(row.get("image")))


def index(doc: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {key_of(r): r for r in doc.get("measurements", [])}


def fmt(n: int | None) -> str:
    return "—" if n is None else f"{n:,}"


def fmt_delta(d: int) -> str:
    return f"{d:+,}"


def budget_for(row: dict[str, Any], metric: str) -> tuple[str, int] | None:
    """Tightest declared ceiling that applies to this metric."""
    applicable = [b for b in row.get("budgets", []) if b["metric"] == metric]
    if not applicable:
        return None
    best = min(applicable, key=lambda b: int(b["limit"]))
    return best["name"], int(best["limit"])


def overflows(rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str, int, int]]:
    """(row, budget_name, limit, measured) for every exceeded ceiling."""
    out = []
    for row in rows:
        measured = row.get("measured", {})
        for b in row.get("budgets", []):
            value = measured.get(b["metric"])
            if value is not None and value > int(b["limit"]):
                out.append((row, b["name"], int(b["limit"]), value))
    return out


def near_budget(
    rows: list[dict[str, Any]], pct: float
) -> list[tuple[dict[str, Any], str, int, int]]:
    out = []
    for row in rows:
        measured = row.get("measured", {})
        for b in row.get("budgets", []):
            value = measured.get(b["metric"])
            limit = int(b["limit"])
            if value is None or limit <= 0 or value > limit:
                continue
            if 100.0 * value / limit >= pct:
                out.append((row, b["name"], limit, value))
    return out


def render(
    head: dict[str, Any],
    base: dict[str, Any] | None,
    *,
    min_bytes: int,
    min_pct: float,
    near_pct: float,
    base_note: str | None,
) -> tuple[str, list[str]]:
    head_rows = head.get("measurements", [])
    head_idx = index(head)
    base_idx = index(base) if base else {}

    lines: list[str] = ["## Firmware size"]
    warnings: list[str] = []

    over = overflows(head_rows)
    near = [n for n in near_budget(head_rows, near_pct) if n not in over]

    # ---- headline ----
    hc = head.get("commit", {})
    if base:
        bc = base.get("commit", {})
        lines.append(
            f"`{str(bc.get('sha', ''))[:12]}` → `{str(hc.get('sha', ''))[:12]}` "
            f"· {len(head_rows)} images"
        )
    else:
        lines.append(f"`{str(hc.get('sha', ''))[:12]}` · {len(head_rows)} images · no baseline")
    if base_note:
        lines.append(f"\n> {base_note}")

    # ---- overflow ----
    if over:
        lines.append("\n### ⚠️ Over budget\n")
        lines.append("| Board | Image | Budget | Limit | Actual | Over by |")
        lines.append("|---|---|---|---|---|---|")
        for row, name, limit, value in sorted(over, key=lambda t: -(t[3] - t[2])):
            lines.append(
                f"| {row['board']} | `{row['image']}` | {name} | {fmt(limit)} "
                f"| {fmt(value)} | **{fmt_delta(value - limit)}** |"
            )
            warnings.append(
                f"{row['board']}/{row['image']} exceeds its {name} budget by "
                f"{value - limit:,} B ({value:,} > {limit:,})"
            )

    # ---- changes ----
    changed_rows: list[str] = []
    added: list[str] = []
    removed: list[str] = []

    for key in sorted(head_idx.keys() | base_idx.keys()):
        board, _build, image = key
        h = head_idx.get(key)
        b = base_idx.get(key)
        if h is None:
            removed.append(f"{board}/{image}")
            continue
        if b is None and base:
            added.append(f"{board}/{image}")
            continue
        if b is None:
            continue

        for metric in TREND_METRICS:
            hv = h["measured"].get(metric)
            bv = b["measured"].get(metric)
            if hv is None or bv is None:
                continue
            delta = hv - bv
            if delta == 0:
                continue
            pct = (100.0 * delta / bv) if bv else 0.0
            if abs(delta) < min_bytes and abs(pct) < min_pct:
                continue

            bud = budget_for(h, metric)
            if bud:
                bname, blimit = bud
                headroom = blimit - hv
                budget_cell = f"{bname} {fmt(blimit)}"
                headroom_cell = f"{fmt(headroom)} ({100.0 * headroom / blimit:.1f}% free)"
            else:
                budget_cell = "—"
                headroom_cell = "—"

            changed_rows.append(
                f"| {board} | `{image}` | {metric} | {fmt(bv)} | {fmt(hv)} "
                f"| **{fmt_delta(delta)}** | {pct:+.2f}% | {budget_cell} | {headroom_cell} |"
            )

    if base:
        if changed_rows:
            lines.append("\n### Changes\n")
            lines.append("| Board | Image | Metric | Base | Head | Δ | Δ% | Budget | Headroom |")
            lines.append("|---|---|---|---|---|---|---|---|---|")
            lines.extend(changed_rows)
        else:
            lines.append(
                f"\nNo image moved by ≥ {min_bytes:,} B or ≥ {min_pct:g}% "
                f"on stored, resident or placed."
            )
        if added:
            lines.append(f"\nNew images: {', '.join(f'`{a}`' for a in sorted(added))}")
        if removed:
            gone = ", ".join(f"`{r}`" for r in sorted(removed))
            lines.append(f"\nImages no longer built: {gone}")

    # ---- near budget ----
    if near:
        lines.append(f"\n### Within {near_pct:g}% of budget\n")
        lines.append("| Board | Image | Budget | Limit | Actual | Used | Headroom |")
        lines.append("|---|---|---|---|---|---|---|")
        for row, name, limit, value in sorted(near, key=lambda t: -(t[3] / t[2])):
            lines.append(
                f"| {row['board']} | `{row['image']}` | {name} | {fmt(limit)} | {fmt(value)} "
                f"| {100.0 * value / limit:.1f}% | {fmt(limit - value)} |"
            )

    # ---- full table, collapsed ----
    lines.append("\n<details>\n<summary>All images</summary>\n")
    lines.append("| Board | Image | Pool | stored | resident | Binding budget | Headroom |")
    lines.append("|---|---|---|---|---|---|---|")
    for key in sorted(head_idx.keys()):
        board, _build, image = key
        row = head_idx[key]
        m = row["measured"]
        binding = row.get("binding")
        bud = next((b for b in row.get("budgets", []) if b["name"] == binding), None)
        if bud:
            value = m.get(bud["metric"], 0)
            head_room = f"{fmt(int(bud['limit']) - value)} vs {binding}"
        else:
            head_room = "—"
        pool = "single" if row.get("single_pool") else "split"
        lines.append(
            f"| {board} | `{image}` | {pool} | {fmt(m.get('stored'))} "
            f"| {fmt(m.get('resident'))} | {binding or '—'} | {head_room} |"
        )
    lines.append("\n" + " · ".join(f"**{k}**: {v}" for k, v in METRIC_HELP.items()))
    lines.append("\n</details>")

    return "\n".join(lines) + "\n", warnings


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--head", required=True, type=Path)
    ap.add_argument("--base", type=Path, default=None)
    ap.add_argument("--base-note", default=None,
                    help="Shown above the table, e.g. when the baseline is not the merge-base.")
    ap.add_argument("--min-bytes", type=int, default=64,
                    help="Report a change at or above this size.")
    ap.add_argument("--min-pct", type=float, default=0.1)
    ap.add_argument("--near-pct", type=float, default=90.0,
                    help="Always list images using at least this share of a budget.")
    ap.add_argument("--annotate", action="store_true",
                    help="Emit ::warning:: lines for overflows, as compliance.yml does.")
    ap.add_argument("--fail-on-overflow", action="store_true",
                    help="Exit non-zero when an image is over budget. Off by default.")
    ap.add_argument("-o", "--output", type=Path, default=None)
    args = ap.parse_args()

    head = load(args.head)
    base = load(args.base) if args.base and args.base.is_file() else None

    text, warnings = render(
        head,
        base,
        min_bytes=args.min_bytes,
        min_pct=args.min_pct,
        near_pct=args.near_pct,
        base_note=args.base_note,
    )

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)

    if args.annotate:
        for w in warnings:
            print(f"::warning title=Firmware size::{w}", file=sys.stderr)

    if warnings and args.fail_on_overflow:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
