# SPDX-License-Identifier: Apache-2.0

cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})

include(${CMAKE_CURRENT_LIST_DIR}/../../cmake/git_version.cmake)

project(dmc)

FILE(GLOB app_sources src/*.c)
target_sources(app PRIVATE ${app_sources})

# C++ header soak test: proves the Zephyr and tt-system-firmware headers we
# use are C++-clean.
target_sources(app PRIVATE ${CMAKE_CURRENT_LIST_DIR}/../cpp_header_soak.cpp)
