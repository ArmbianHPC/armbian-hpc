#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build the pinned ARM64 Meson kernel and the OES1PLUS v2.2 device tree."""
import argparse, hashlib, json, os, shutil, subprocess, tarfile
from pathlib import Path

VERSION = '6.12.111'
SOURCE_SHA256 = '9e59dc67624188fa12a6601f9598499cd6662a9066be572b59f935e3d7849810'

def run(*args, **kwargs):
    subprocess.run(list(map(str, args)), check=True, **kwargs)

def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('project', 'source-archive', 'inputs', 'work'):
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--jobs', type=int, default=8)
    a = p.parse_args()
    assert sha(a.source_archive) == SOURCE_SHA256, 'Kernel archive checksum differs'
    work = a.work.resolve(); assert work != Path('/') and not (work/'.git').exists()
    work.mkdir(parents=True, exist_ok=True)
    source, obj = work/'source', work/'obj'
    if not source.exists():
        unpack = work/'unpack'; unpack.mkdir()
        with tarfile.open(a.source_archive) as archive:
            archive.extractall(unpack, filter='data')
        (unpack/('linux-'+VERSION)).rename(source)
        unpack.rmdir()
        patches_dir=a.project/'adapters/boards/wxy-oesp-v22/patches'
        selected=(patches_dir/'amlogic-legacy-text-offset-pcie.patch').read_text()
        run('patch', '-p1', '--batch', '--forward', cwd=source, input=selected, text=True)
        shutil.copy2(patches_dir/'dwmac-meson8b.c', source/'drivers/net/ethernet/stmicro/stmmac/dwmac-meson8b.c')
        with (source/'arch/arm64/boot/dts/amlogic/Makefile').open('a') as stream:
            stream.write('\ndtb-$(CONFIG_ARCH_MESON) += meson-g12b-s922x-armhpc-oes1plus-v22.dtb\n')
    dts = a.project/'adapters/boards/wxy-oesp-v22/dts/meson-g12b-s922x-armhpc-oes1plus-v22.dts'
    destination = source/'arch/arm64/boot/dts/amlogic'/dts.name
    if not destination.exists() or destination.read_bytes() != dts.read_bytes():
        shutil.copy2(dts, destination)
    obj.mkdir(exist_ok=True)
    fan_source=source/'drivers/hwmon/gpio-fan.c'
    fan_text=fan_source.read_text()
    anchor='\tif (state >= fan_data->num_speed)\n'
    override='\t/* Disabled manual control must remain at full speed under thermal updates. */\n\tif (!fan_data->pwm_enable)\n\t\tstate = fan_data->num_speed - 1;\n\n'
    if override not in fan_text:
        assert fan_text.count(anchor)==1
        fan_source.write_text(fan_text.replace(anchor,override+anchor))
    options = ['ARCH=arm64', 'CROSS_COMPILE=aarch64-linux-gnu-', 'O='+str(obj), 'LOCALVERSION=-armhpc-meson64']
    if not (obj/'.config').exists():
        run('make', *options, 'defconfig', cwd=source)
        cfg = [str(source/'scripts/config'), '--file', str(obj/'.config')]
        enable = ['ARCH_MESON','EXT4_FS','VFAT_FS','SQUASHFS','SQUASHFS_XZ','OVERLAY_FS','DEVTMPFS','DEVTMPFS_MOUNT',
                  'BLK_DEV_INITRD','RD_XZ','RD_ZSTD','RD_GZIP','RD_LZ4','PCI','PCIE_MESON','SATA_AHCI','ATA','SCSI','BLK_DEV_SD',
                  'STMMAC_ETH','DWMAC_MESON8B','REALTEK_PHY','USB','USB_XHCI_HCD','USB_XHCI_PLATFORM','USB_DWC3','USB_DWC3_MESON_G12A',
                  'USB_RTL8152','GPIO_SYSFS','SENSORS_GPIO_FAN','THERMAL','THERMAL_OF','CPU_THERMAL','CGROUPS','MEMCG','CGROUP_PIDS',
                  'CGROUP_BPF','CGROUP_SCHED','CPUSETS','NAMESPACES','NET_NS','PID_NS','USER_NS','UTS_NS','IPC_NS',
                  'BPF','BPF_SYSCALL','BPF_JIT','NETFILTER','BRIDGE','BRIDGE_NETFILTER','VETH','VXLAN','IP_NF_IPTABLES',
                  'IP_NF_FILTER','IP_NF_NAT','NF_NAT','NF_CONNTRACK','NETFILTER_XT_MATCH_COMMENT','NETFILTER_XT_MATCH_MULTIPORT',
                  'NETFILTER_XT_MATCH_CONNTRACK','NETFILTER_XT_TARGET_MASQUERADE','NETFILTER_XT_TARGET_MARK','BLK_CGROUP',
                  'LEDS_CLASS','LEDS_GPIO','LEDS_TRIGGERS','LEDS_TRIGGER_NETDEV','LEDS_TRIGGER_HEARTBEAT','LEDS_TRIGGER_TIMER',
                  'LEDS_TRIGGER_DISK','WATCHDOG','MESON_GXBB_WATCHDOG','KEXEC','KEXEC_FILE','IKCONFIG','IKCONFIG_PROC',
                  'SERIAL_MESON','SERIAL_MESON_CONSOLE','MMC','MMC_MESON_GX','MMC_BLOCK','CONFIGFS_FS','SYSVIPC']
        for name in enable: cfg += ['--enable', name]
        for name in ('DRM','SOUND','MEDIA_SUPPORT','WIRELESS','WLAN','LOCALVERSION_AUTO','DEBUG_INFO','DEBUG_INFO_BTF'):
            cfg += ['--disable', name]
        run(*cfg)
        run('make', *options, 'olddefconfig', cwd=source)
    run(source/'scripts/config', '--file', obj/'.config', '--enable', 'SQUASHFS_XATTR', '--enable', 'PHYLIB_LEDS',
        '--enable', 'CPU_FREQ_GOV_PERFORMANCE', '--enable', 'DEVFREQ_GOV_PERFORMANCE',
        '--enable', 'USB_DWC2', '--enable', 'USB_DWC2_DUAL_ROLE', '--enable', 'USB_GADGET',
        '--enable', 'USB_LIBCOMPOSITE', '--enable', 'USB_CONFIGFS', '--enable', 'USB_CONFIGFS_ACM',
        '--enable', 'USB_ROLE_SWITCH')
    run('make', *options, 'olddefconfig', cwd=source)
    env = {**os.environ, 'KBUILD_BUILD_USER':'armhpc', 'KBUILD_BUILD_HOST':'builder', 'KBUILD_BUILD_VERSION':'1'}
    run('make', *options, '-j'+str(a.jobs), 'Image', 'modules', 'amlogic/meson-g12b-s922x-armhpc-oes1plus-v22.dtb', cwd=source, env=env)
    release = subprocess.check_output(['make', '-s', *options, 'kernelrelease'], cwd=source, text=True).strip()
    modules = work/'modules'; modules.mkdir(exist_ok=True)
    run('make', *options, 'INSTALL_MOD_PATH='+str(modules), 'INSTALL_MOD_STRIP=1', 'modules_install', cwd=source)
    result = {'kernel_release':release,'source_version':VERSION,'source_sha256':SOURCE_SHA256,
              'image_sha256':sha(obj/'arch/arm64/boot/Image'),
              'dtb_sha256':sha(obj/'arch/arm64/boot/dts/amlogic/meson-g12b-s922x-armhpc-oes1plus-v22.dtb')}
    (work/'KERNEL-RESULT.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2), flush=True)

if __name__ == '__main__': main()
