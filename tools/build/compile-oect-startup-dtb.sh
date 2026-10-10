#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Reproduce the original OECT PHY0 DTB from the reviewed patched Linux tree.
set -euo pipefail
export LC_ALL=C.UTF-8
if test "$#" -ne 3; then
    printf 'Usage: %s PROJECT PATCHED_LINUX_TREE NEW_OUTPUT_DIRECTORY\n' "$0" >&2
    exit 2
fi
project=$(realpath -e "$1")
tree=$(realpath -e "$2")
output=$(realpath -m "$3")
test -d "$(dirname "$output")"
test ! -e "$output"
test ! -L "$output"
test "$(git -C "$tree" rev-parse HEAD)" = e2acc2211022246c77740d5df08265cc27eedcc5
mkdir "$output"
dts="$project/adapters/boards/wxy-oect-original/dts/rk3566-wxy-oec-turbo-4g.dts"
cpp -nostdinc -undef -D__DTS__ -x assembler-with-cpp \
    -I "$tree/include" -I "$tree/scripts/dtc/include-prefixes" -I "$tree/arch/arm64/boot/dts/rockchip" \
    "$dts" > "$output/preprocessed.dts"
dtc -@ -I dts -O dtb -o "$output/rk3566-wxy-oec-turbo-4g.dtb" "$output/preprocessed.dts" 2> "$output/dtc.log"
test "$(fdtget -t x "$output/rk3566-wxy-oec-turbo-4g.dtb" /ethernet@fe010000/mdio/ethernet-phy@0 reg)" = 0
actual_sha=$(sha256sum "$output/rk3566-wxy-oec-turbo-4g.dtb")
test "${actual_sha%% *}" = 8809fc109bf48304f3f8519d0a16ee568c4dd2094a6b3a5ca92f2160f996c77a
printf '%s\n' "$actual_sha"
