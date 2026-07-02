# SPDX-License-Identifier: Apache-2.0

zephyr_library()
zephyr_library_sources_ifdef(CONFIG_JTAG_BITBANG jtag_bitbang.c)
zephyr_library_sources_ifdef(CONFIG_JTAG_EMUL jtag_emul.c)
zephyr_library_sources_ifdef(CONFIG_JTAG_SHELL jtag_shell.c)

zephyr_syscall_header_ifdef(CONFIG_JTAG
  ${ZEPHYR_TT_SYSTEM_FIRMWARE_MODULE_DIR}/include/zephyr/drivers/jtag.h)
