<!--
Copyright (c) 2026 Tenstorrent AI ULC
SPDX-License-Identifier: Apache-2.0
-->

# Firmware size tracking

Measures every Zephyr image in a build, records the history on an orphan git
branch, and reports what moved on a pull request.

Nothing here changes the build. `.github/workflows/build-fw.yml` already uploads
`zephyr.elf`, `zephyr.dts`, `zephyr.bin`, `zephyr.signed.bin` and `.config` for
every image of every board, so all measurements come from artifacts that exist
anyway.

## The two numbers

SMC and DMC are not the same shape, so one number will not do.

| | `stored` | `resident` |
|---|---|---|
| definition | Σ `PT_LOAD p_filesz` (equals `zephyr.bin`) | allocated sections inside the SRAM region |
| includes bss/noinit | no | **yes** |
| bounds | a flash partition | the RAM pool |

The Blackhole SMC is **single-pool**: `soc/tenstorrent/tt_blackhole/tt_blackhole_smc.ld`
defines no `FLASH_START`/`FLASH_SIZE`, so Zephyr's ARC linker script collapses
`ROMABLE_REGION` and `RAMABLE_REGION` into one `SRAM`. The app is RAM-loaded by
MCUboot into `csm_app`, 412 KiB, and text, rodata, data, bss, noinit, heap and
stacks all compete for it. So on SMC `resident` is the number that constrains
the build and it necessarily includes bss and noinit. The STM32 DMC is ordinary
XIP: 448 KiB flash and 144 KiB SRAM, independent.

Single-pool is detected from the ELF, not from `CONFIG_XIP`: `config ARC` does
`imply XIP`, so the SMC reports `CONFIG_XIP=y` while its linker is single-pool.
An image is single-pool when every allocated section lies inside its SRAM region.

## Budgets

An image can be bounded by five different ceilings, and in this tree they
disagree — for the DMC, 447 KiB (linker), 452 KiB (imgtool, which sizes from
*slot 1*, the external `dmfw`) and 444 KiB (the real gap to `dmfwtail`). The
tightest is enforced last, by `tt_boot_fs.py mkfs`, as

```
Range 22e000:29d800 overlaps with existing range 2740608:2744704
```

which names neither the image nor a size. So every applicable ceiling is
recorded and the one with the fewest bytes of headroom is marked `binding`.

| budget | source | bounds |
|---|---|---|
| `linker_ram` | `CONFIG_SRAM_SIZE` | `resident` |
| `linker_rom` | `CONFIG_FLASH_LOAD_SIZE` − `CONFIG_ROM_START_OFFSET` | `stored` |
| `imgtool` | `reg` size of `slot1_partition` | `signed` |
| `bootfs` | gap to the next tt-boot-fs partition | `placed` |
| `ramload` | `CONFIG_BOOT_IMAGE_EXECUTABLE_RAM_SIZE` | `resident` |

The declared `reg` size of a partition is deliberately *not* used: `mainimg`,
`safeimg` and `dmfwimg` each declare 4 KiB more than they can hold, because an
MCUboot slot includes a trailer that lives in a separate partition starting
4 KiB earlier.

## Scaling

Nothing enumerates images or boards. `collect.py` globs `*/zephyr/zephyr.elf`
(sysbuild) or `zephyr/zephyr.elf` (plain build) and derives the board, the SoC
and every budget from the co-located `.config` and `zephyr.dts`. Adding a
revision to `.github/boards.json` or an image to `app/smc/sysbuild.cmake` shows
up as a new series with no change here.

## Commands

```sh
# Measure a build tree (board is inferred from CONFIG_BOARD_REVISION)
scripts/size/collect.py --build-dir build-p150a --build-name smc-sysbuild -o head.json

# Several boards at once; one --build-name labels them all
scripts/size/collect.py --build-dir build-p150a --build-dir build-p300a \
    --build-name smc-sysbuild -o head.json

# Compare and render Markdown
scripts/size/report.py --head head.json --base base.json -o report.md

# History on the orphan branch
scripts/size/store.py put --data head.json
scripts/size/store.py list
scripts/size/store.py get --sha <sha>
scripts/size/store.py nearest --from <sha>      # newest stored ancestor
```

`report.py` is warn-only: it emits `::warning::` annotations for anything over
budget but exits 0. Pass `--fail-on-overflow` to make it a hard failure.

Requires `pyelftools`, and Zephyr's `dtlib` — found via `$ZEPHYR_BASE` or a
sibling `../zephyr` checkout.

## Data branch

`firmware-size-data`, written with git plumbing so nothing is ever checked out
and the branch can be created from nothing:

```
data/<yyyy>/<sha>.json   one commit, every board, every image
index.json               ordered [{sha, date, tag, source, images, path}]
schema.json
index.html               trend page
```

Per-board CI jobs can land independently: `store.py put` merges into an existing
file for the same commit rather than replacing it.

CI artifacts expire after 90 days, so a measurement is only permanent once it is
on this branch. The daily sweep in `size-report.yml` exists to make sure that
happens.

## Backfill

`backfill.py` rebuilds history. Default target is the 43 non-rc `v*` tags
(~470 sysbuilds); all 2211 commits is not worth it.

```sh
scripts/size/backfill.py --workspace ~/bh-zephyr-backfill --list
scripts/size/backfill.py --workspace ~/bh-zephyr-backfill --path-cache ~/west-cache
```

It mutates a west workspace, so point it at a dedicated checkout, not one you
are working in. Refs are processed serially because each needs its own
`west update`. Failures are recorded as gaps and the run continues — old tags
are not guaranteed to build with the current SDK. Re-running skips refs already
in the index.

Locally built numbers will not match CI byte for byte (different SDK, different
host, unsigned images), so they are tagged `source.kind = local-backfill`. Do
not read a toolchain change as a code regression.
