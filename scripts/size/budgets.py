# Copyright (c) 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Resolve the size ceilings that apply to a built image.

An image can be bounded by up to five independent limits, and in this tree they
do not agree with each other. For the DMC the linker says 448 KiB, imgtool says
452 KiB (it sizes from slot 1, which is the external `dmfw` partition), and the
boot filesystem says 444 KiB (the real gap to `dmfwtail`). The tightest of those
is enforced last and reports itself as a raw address-range collision, so this
module records every applicable ceiling and names the one that actually binds.

  linker_ram  CONFIG_SRAM_SIZE                        bounds `resident`
  linker_rom  CONFIG_FLASH_LOAD_SIZE - ROM_START      bounds `stored`
  imgtool     reg size of slot1_partition             bounds `signed`
  bootfs      gap to the next tt-boot-fs partition    bounds `placed`
  ramload     CONFIG_BOOT_IMAGE_EXECUTABLE_RAM_SIZE   bounds `resident`

`placed` is the size of the file the boot filesystem actually writes, which is
the signed binary for application images and the raw binary for bootloaders.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_RE = re.compile(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$")
BINARY_PATH_RE = re.compile(
    r"^\$BUILD_DIR/(?P<image>[^/$]+)/zephyr/(?P<file>zephyr(\.signed)?\.bin)$"
)

TT_BOOT_FS_COMPATIBLE = "tenstorrent,tt-boot-fs"


@dataclass(frozen=True)
class Budget:
    name: str
    limit: int
    metric: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "limit": self.limit, "metric": self.metric}


@dataclass(frozen=True)
class Placement:
    """Where the boot filesystem puts an image, and how much room it really has."""

    partition: str
    offset: int
    declared: int
    gap: int
    filename: str


def parse_config(path: Path) -> dict[str, str]:
    """Parse a Zephyr .config into a plain dict. Absent options are simply missing."""
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = CONFIG_RE.match(line.strip())
            if m:
                out[m.group(1)] = m.group(2).strip('"')
    return out


def config_int(cfg: dict[str, str], key: str, default: int = 0) -> int:
    raw = cfg.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw, 0)
    except ValueError:
        return default


def _load_dt(dts_path: Path, zephyr_base: Path) -> Any:
    """Parse a generated zephyr.dts using Zephyr's dtlib (stdlib-only, no yaml)."""
    import sys

    src = zephyr_base / "scripts" / "dts" / "python-devicetree" / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from devicetree import dtlib

    return dtlib.DT(str(dts_path))


def find_slot1_size(dts_path: Path, zephyr_base: Path) -> int | None:
    """Size of slot1_partition, which is what imgtool passes as --slot-size."""
    dt = _load_dt(dts_path, zephyr_base)
    for node in dt.node_iter():
        if "slot1_partition" in node.labels and "reg" in node.props:
            nums = node.props["reg"].to_nums()
            if len(nums) >= 2:
                return nums[1]
    return None


def read_placements(dts_path: Path, zephyr_base: Path) -> dict[str, Placement]:
    """
    Map image name -> where tt-boot-fs places it.

    The declared `reg` size of a partition is *not* a capacity bound: mainimg,
    safeimg and dmfwimg each declare 4 KiB more than they can hold, because an
    MCUboot slot includes its trailer and the trailer lives in a separate
    partition that starts 4 KiB earlier. scripts/tt_boot_fs.py never reads the
    size either; it only uses reg[0] and lets an oversized image collide with
    the next partition. So the honest ceiling is the gap to the next start.
    """
    dt = _load_dt(dts_path, zephyr_base)

    parts_node = None
    for node in dt.node_iter():
        if "compatible" not in node.props:
            continue
        if TT_BOOT_FS_COMPATIBLE in node.props["compatible"].to_strings():
            parts_node = node
            break
    if parts_node is None:
        return {}

    device_size = (
        parts_node.props["flash-device-size"].to_num()
        if "flash-device-size" in parts_node.props
        else 0
    )

    entries: list[tuple[int, int, str, str | None]] = []
    for child in parts_node.nodes.values():
        if "reg" not in child.props:
            continue
        nums = child.props["reg"].to_nums()
        if len(nums) < 2:
            continue
        label = child.props["label"].to_string() if "label" in child.props else child.name
        bpath = child.props["binary-path"].to_string() if "binary-path" in child.props else None
        entries.append((nums[0], nums[1], label, bpath))

    entries.sort()
    starts = [e[0] for e in entries]

    placements: dict[str, Placement] = {}
    for i, (offset, declared, label, bpath) in enumerate(entries):
        if bpath is None:
            continue
        m = BINARY_PATH_RE.match(bpath)
        if m is None:
            continue
        gap = (starts[i + 1] - offset) if i + 1 < len(starts) else max(device_size - offset, 0)
        placement = Placement(
            partition=label,
            offset=offset,
            declared=declared,
            gap=gap,
            filename=m.group("file"),
        )
        image = m.group("image")
        # An image can be written to more than one partition (mcuboot goes to
        # both cmfw and failover). The tightest gap is the one that binds.
        existing = placements.get(image)
        if existing is None or placement.gap < existing.gap:
            placements[image] = placement
    return placements


def is_ram_loaded(cfg: dict[str, str]) -> bool:
    return (
        cfg.get("CONFIG_MCUBOOT_BOOTLOADER_MODE_RAM_LOAD") == "y"
        or cfg.get("CONFIG_MCUBOOT_BOOTLOADER_MODE_RAM_LOAD_WITH_REVERT") == "y"
    )


def resolve_budgets(
    cfg: dict[str, str],
    *,
    slot1_size: int | None,
    placement: Placement | None,
    ramload_size: int | None,
    signed: bool,
) -> list[Budget]:
    budgets: list[Budget] = []

    sram = config_int(cfg, "CONFIG_SRAM_SIZE") * 1024
    if sram:
        budgets.append(Budget("linker_ram", sram, "resident"))

    load_size = config_int(cfg, "CONFIG_FLASH_LOAD_SIZE")
    if load_size:
        rom_start = config_int(cfg, "CONFIG_ROM_START_OFFSET")
        budgets.append(Budget("linker_rom", load_size - rom_start, "stored"))

    if signed and slot1_size:
        budgets.append(Budget("imgtool", slot1_size, "signed"))

    if placement is not None:
        budgets.append(Budget("bootfs", placement.gap, "placed"))

    if ramload_size and is_ram_loaded(cfg):
        budgets.append(Budget("ramload", ramload_size, "resident"))

    return budgets


def binding_budget(budgets: list[Budget], measured: dict[str, int]) -> Budget | None:
    """The ceiling with the least headroom against its own metric."""
    scored = [(b.limit - measured.get(b.metric, 0), b) for b in budgets if b.metric in measured]
    if not scored:
        return None
    return min(scored, key=lambda t: t[0])[1]
