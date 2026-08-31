# Copyright (c) 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Measure the footprint of a Zephyr image from its ELF file.

Two numbers matter, and they are computed by different rules:

  stored   Sum of PT_LOAD p_filesz. These are exactly the bytes written to the
           image file, so this equals the size of zephyr.bin. It is what
           consumes a flash partition.

  resident Sum of the sizes of allocated sections whose address falls inside
           the image's SRAM region. It is what consumes the RAM pool, and it
           therefore includes bss and noinit.

For a single-pool target such as the Blackhole SMC, every allocated section
lives in SRAM, so `resident` covers text and rodata as well and is the only
number that constrains the build. For an XIP target such as the STM32 DMC,
text and rodata sit in flash and only data/bss/noinit are resident.

Whether an image is single-pool is derived from the ELF itself rather than from
CONFIG_XIP: `config ARC` does `imply XIP`, so the SMC reports CONFIG_XIP=y even
though its linker script collapses ROM and RAM into one region.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from elftools.elf.elffile import ELFFile

SHF_WRITE = 0x1
SHF_ALLOC = 0x2
SHF_EXECINSTR = 0x4
SHF_TLS = 0x400

# Sections whose name marks them as deliberately uninitialised. They are
# SHT_NOBITS like bss and consume the same pool, but are broken out because
# growth there usually means a buffer was resized rather than code was added.
NOINIT_RE = re.compile(r"(^|[._])noinit")


@dataclass
class Sections:
    """Section totals by kind. Informational: `stored` and `resident` are authoritative."""

    text: int = 0
    rodata: int = 0
    data: int = 0
    bss: int = 0
    noinit: int = 0
    tls: int = 0

    def add(self, kind: str, size: int) -> None:
        setattr(self, kind, getattr(self, kind) + size)


@dataclass
class ImageSize:
    stored: int
    resident: int
    single_pool: bool
    sram_base: int
    sram_size: int
    sections: Sections = field(default_factory=Sections)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sram_base"] = hex(self.sram_base)
        return d


def _classify(sh_type: str, flags: int, name: str) -> str:
    if sh_type == "SHT_NOBITS":
        return "noinit" if NOINIT_RE.search(name) else "bss"
    if flags & SHF_EXECINSTR:
        return "text"
    if flags & SHF_WRITE:
        return "data"
    return "rodata"


def measure_elf(elf_path: Path, sram_base: int, sram_size: int) -> ImageSize:
    """Measure one image. `sram_base`/`sram_size` come from CONFIG_SRAM_*."""
    sections = Sections()
    resident = 0
    outside_sram = False
    sram_end = sram_base + sram_size

    with open(elf_path, "rb") as f:
        elf = ELFFile(f)

        stored = sum(
            seg["p_filesz"] for seg in elf.iter_segments() if seg["p_type"] == "PT_LOAD"
        )

        for sec in elf.iter_sections():
            flags = sec["sh_flags"]
            size = sec["sh_size"]
            if not (flags & SHF_ALLOC) or size == 0:
                continue

            # TLS template sections are overlaid on other sections' addresses and
            # are allocated per-thread out of stacks, so they are not a fixed
            # reservation in the region. Counted separately, excluded from totals.
            if flags & SHF_TLS:
                sections.tls += size
                continue

            sections.add(_classify(sec["sh_type"], flags, sec.name), size)

            if sram_base <= sec["sh_addr"] < sram_end:
                resident += size
            else:
                outside_sram = True

    return ImageSize(
        stored=stored,
        resident=resident,
        single_pool=not outside_sram,
        sram_base=sram_base,
        sram_size=sram_size,
        sections=sections,
    )
