# SPDX-License-Identifier: GPL-2.0-only
BOARD_NAME="ARMHPC WXY OEC Turbo"
BOARDFAMILY="rk35xx"
BOOT_SOC="rk3566"
BOOTCONFIG="none"
KERNEL_TARGET="current"
FULL_DESKTOP="no"
BOOT_FDT_FILE="rockchip/rk3566-wxy-oec-turbo-4g.dtb"
IMAGE_PARTITION_TABLE="gpt"
SERIALCON="ttyS2"



# Armbian v25.08 gives core patches precedence over user files with the same name.
# Build a private copy of its pinned patch set, then apply board compatibility fixes.
function extension_prepare_config__armhpc_oect_patches() {
    local patch_dir="${USERPATCHES_PATH}/kernel/armhpc-oect-6.12"
    mkdir -p "${patch_dir}"
    cp -a "${SRC}/patch/kernel/archive/rockchip64-6.12/." "${patch_dir}/"
    cp -a "${USERPATCHES_PATH}/kernel/armhpc-oect-overrides/." "${patch_dir}/"
    # Copy through the patcher's DT directory, after its Git reset/clean step.
    local dts="rk3566-wxy-oec-turbo-4g.dts"
    mkdir -p "${patch_dir}/dt"
    cp "${USERPATCHES_PATH}/kernel/armhpc-oect/${dts}" "${patch_dir}/dt/${dts}"
    # DEFAULT_MCLK_FS and its fallback are already in Linux 6.12.111.
    rm -f -- "${patch_dir}/rk3308-i2s-default-rate.patch"
    declare -g KERNELPATCHDIR="armhpc-oect-6.12"
    declare -g COLUMNS="${COLUMNS:-160}"
}
