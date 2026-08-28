/*
 * Copyright (c) 2026 Tenstorrent AI ULC
 *
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * C++ header soak test.
 *
 * This translation unit is compiled as C++ into both the SMC and DMC images.
 * Its only purpose is to prove our headers are C++-clean: if any is not
 * includable from C++ (missing `extern "C"`, use of a C++ keyword as an
 * identifier, implicit void* conversions in an inline/macro, etc.) the build
 * fails here. It includes two sets of headers:
 *
 *   1. Every <zephyr/...> header used anywhere in the tt-system-firmware
 *      sources and the shared code they pull in:
 *        grep -rhoE '#include[[:space:]]*<zephyr/[^>]+>' \
 *          app/smc app/dmc app/dmc_rom_update lib drivers soc include \
 *          | grep -oE '<zephyr/[^>]+>' | sort -u
 *      Excluded:
 *        - <zephyr/arch/arc/v2/linker.ld>: a linker script, not a C/C++ header.
 *        - <zephyr/drivers/i3c/target_device.h>: used only by the separate
 *          dm_test_app (not SMC/DMC); it is config-gated behind
 *          CONFIG_I3C_TARGET and does not compile in this configuration in C
 *          either.
 *        - <zephyr/drivers/pmci/pldm/pldm_oem_handler.h>,
 *          <zephyr/pmci/mctp/mctp_i3c_target.h> and
 *          <zephyr/shell/shell_dummy.h>: used only by drivers/pmci/pldm, which
 *          is gated behind CONFIG_PLDM. The first two pull in <libpldm/base.h>
 *          and <libmctp.h>, whose module include paths are only added when that
 *          config is on; the third needs CONFIG_SHELL_BACKEND_DUMMY_BUF_SIZE,
 *          which only exists once CONFIG_TT_PMCI_PLDM_SHELL selects the dummy
 *          backend. None of the three compile here in C either.
 *
 *   2. Every public library header we expose under include/tenstorrent/:
 *        ls include/tenstorrent | grep '\.h$'
 *      Excluded:
 *        - <tenstorrent/uart_tt_virt.h>: its inline helpers use C11
 *          <stdatomic.h> (atomic_uint, atomic_compare_exchange_strong), which
 *          is not valid in C++ before C++23, and the minimal C++ library
 *          (CONFIG_MINIMAL_LIBCPP) does not provide <atomic>. Re-add here once
 *          the header is made C++-clean.
 */

/* Architecture-specific: only valid on the ARC-based SMC core. */
#ifdef CONFIG_ARC
#include <zephyr/arch/arc/v2/aux_regs.h>
#endif

#include <zephyr/app_version.h>
#include <zephyr/arch/common/ffs.h>
#include <zephyr/arch/common/sys_bitops.h>
#include <zephyr/arch/cpu.h>
#include <zephyr/bindesc.h>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/dfu/mcuboot.h>
#include <zephyr/drivers/clock_control.h>
#include <zephyr/drivers/clock_control/clock_control_tt_bh.h>
#include <zephyr/drivers/dma.h>
#include <zephyr/drivers/dma/dma_arc_hs.h>
#include <zephyr/drivers/dma/dma_tt_bh_noc.h>
#include <zephyr/drivers/flash.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/gpio/gpio_emul.h>
#include <zephyr/drivers/gpio/gpio_utils.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/drivers/i3c.h>
#include <zephyr/drivers/jtag.h>
#include <zephyr/drivers/mbox.h>
#include <zephyr/drivers/mfd/max6639.h>
#include <zephyr/drivers/misc/bh_efuse.h>
#include <zephyr/drivers/misc/bh_fwtable.h>
#include <zephyr/drivers/mspi.h>
#include <zephyr/drivers/pinctrl.h>
#include <zephyr/drivers/pwm.h>
#include <zephyr/drivers/reset.h>
#include <zephyr/drivers/reset/reset_tt_bh.h>
#include <zephyr/drivers/sensor.h>
#include <zephyr/drivers/sensor/tenstorrent/pvt_tt_bh.h>
#include <zephyr/drivers/smbus.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/drivers/watchdog.h>
#include <zephyr/dt-bindings/gpio/gpio.h>
#include <zephyr/dt-bindings/i2c/i2c.h>
#include <zephyr/dt-bindings/pinctrl/tt_blackhole_smc-pinctrl.h>
#include <zephyr/dt-bindings/reset/tt-bh-reset.h>
#include <zephyr/init.h>
#include <zephyr/irq.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/logging/log_backend.h>
#include <zephyr/logging/log_backend_std.h>
#include <zephyr/logging/log_core.h>
#include <zephyr/logging/log_ctrl.h>
#include <zephyr/logging/log_msg.h>
#include <zephyr/logging/log_output.h>
#include <zephyr/rtio/rtio.h>
#include <zephyr/rtio/work.h>
#include <zephyr/shell/shell.h>
#include <zephyr/spinlock.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/sys/__assert.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/barrier.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/clock.h>
#include <zephyr/sys/crc.h>
#include <zephyr/sys/iterable_sections.h>
#include <zephyr/sys/libc-hooks.h>
#include <zephyr/sys/printk-hooks.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/sys/sys_io.h>
#include <zephyr/sys/util.h>
#include <zephyr/sys/util_macro.h>
#include <zephyr/sys_clock.h>
#include <zephyr/toolchain.h>
#include <zephyr/tracing/tracing.h>
#include <zephyr/types.h>
#include <zephyr/version.h>
#include <zephyr/zbus/zbus.h>

/* Public library headers exposed under include/tenstorrent/. */
#include <tenstorrent/bh_arc.h>
#include <tenstorrent/bh_chip.h>
#include <tenstorrent/bh_power.h>
#include <tenstorrent/bist.h>
#include <tenstorrent/bitrev.h>
#include <tenstorrent/dm_event.h>
#include <tenstorrent/event.h>
#include <tenstorrent/jtag_bootrom.h>
#include <tenstorrent/log_backend_ringbuf.h>
#include <tenstorrent/msgqueue.h>
#include <tenstorrent/occp.h>
#include <tenstorrent/post_code.h>
#include <tenstorrent/smbus_target.h>
#include <tenstorrent/smc_msg.h>
#include <tenstorrent/spi_flash_buf.h>
#include <tenstorrent/sys_init_defines.h>
#include <tenstorrent/tt_bindesc.h>
#include <tenstorrent/tt_boot_fs.h>
#include <tenstorrent/tt_smbus_regs.h>

/*
 * A definition so the translation unit is non-empty and the object carries a
 * symbol. Never called; existence is the whole point.
 */
extern "C" int tt_cpp_header_soak(void)
{
	return 0;
}
