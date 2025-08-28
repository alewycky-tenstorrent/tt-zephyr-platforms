/*
 * Copyright (c) 2024 Tenstorrent AI ULC
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <zephyr/ztest.h>
#include <zephyr/kernel.h>
#include <math.h>


/* picolib's floorf is compiled for soft float. This ensures that the ABI
 * is compatible with the hard float application.
 */

typedef float (*floorf_ptr_t)(float);
/* volatile to discourage optimization into a direct call */
static volatile floorf_ptr_t p_floorf = floorf;

ZTEST(fpu, test_floorf)
{
	k_float_enable(k_current_get(), 0);
	zexpect_equal(floorf(1234.5f), 1234.0f);
	zexpect_equal(p_floorf(2345.5f), 2345.0f);
}

ZTEST_SUITE(fpu, NULL, NULL, NULL, NULL, NULL);
