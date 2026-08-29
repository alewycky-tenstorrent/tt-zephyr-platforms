# Copyright (c) 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

# The SMC devicetrees deliberately place several flash chip candidates at
# the same SPI bus position; the flash mux selects among them at boot.
if("${BOARD_QUALIFIERS}" MATCHES "/smc")
  list(APPEND EXTRA_DTC_FLAGS "-Wno-unique_unit_address_if_enabled")
endif()
