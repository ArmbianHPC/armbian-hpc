<!-- SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only -->
# OES1PLUS v2.2

[简体中文](OESP.zh-CN.md)

## Indicators

The power indicator is green in both the system and Rescue environments, and red when a required check reports an error. Rescue does not require a SATA DATA mount. The front network indicator is green when either NET0 or NET1 has carrier, and red when neither has carrier; it indicates the local link, not Internet reachability.

NET0 uses PHY LED1 for the green link indicator and LED2 for the yellow RX/TX activity indicator. This mapping is confirmed on `oes1plus-v2.2`. Activity has no steady link rule. `python3 /usr/lib/armhpc/phy_leds.py --status` reads the current sysfs rules and hardware offload state. NET0 identification continues to use the green LED1 output.

This profile covers the `oes1plus-v2.2` PCB with an Amlogic S922X, 4 GiB DDR4, Samsung KLM8G1GETF eMMC, ASM1064 SATA controller, RTL8211F NET0 and RTL8153B NET1.

## Storage and boot

The eMMC image uses four MBR primary partitions after a 100 MiB Amlogic boot reservation: 512 MiB FAT32 BOOT, two 512 MiB XZ SquashFS Rescue slots and the remaining shared ext4 area. The selected SATA disk uses a 64 GiB ext4 root and an ext4 DATA partition in a separate GPT layout.

Vendor FIP and the temporary USB-loader DTB are local boot inputs. Their public redistribution requires a separate review. Source archives contain the integration, original notices and input hashes; they do not contain vendor binaries, signing keys, passwords or device backups. No eFuse operation is part of installation or OTA.

Use Amlogic USB boot to enter the USB commissioning image. Its early boot script makes no persistent environment changes. Commissioning media cannot run system or Rescue OTA, nor the ordinary SATA installer. Back up the actual eMMC user, boot0 and boot1 areas before replacing it. Use mounted external storage with enough free space for the backup and an uncompressed image, then export the private backup to the controller computer.

```sh
armhpc-rescue emmc backup --directory /mnt/backup --yes --json
armhpc-rescue emmc install --image /mnt/backup/armhpc.img --manifest /mnt/backup/image.manifest.json --backup /mnt/backup/armhpc-emmc-backup-DEVICE/backup.json --json
```

The second command verifies and previews. Add `--execute --yes` only after reviewing the matched device and verified backup. After installation, remove the boot USB and perform a cold start. A raw `.img` is not an Amlogic Burning Tool `.burn.img`.

### Installation without a USB drive

Use the completed native USB Burning Tool image described below for a full cable installation. It is distinct from a raw block-device image.

For recovery after an already initialized BOOT candidate, use the [task-bound USB Rescue recovery](USB-RECOVERY.md) tool. It loads the retained signed boot pair into RAM and continues restoration from Linux after device identity is checked.

The older `oesp-usb-install.py` source requires vendor CID/EXT_CSD commands that the observed OESP loader rejected. Its direct backup/write path is not included in the delivered recovery tool. Obtain complete private backups through the Linux `emmc backup` interface before replacing device storage.

## Device controls

```sh
armhpc-rescue ota status --json
armhpc-rescue ota doctor --json
armhpc-rescue identify --interface eth0 --seconds 10 --period-ms 1000 --json
armhpc-rescue fan status --json
armhpc-rescue fan set --mode auto --yes --json
```

Normal systems use the corresponding `armhpc` commands. Only NET0 supports identification. NET1 uses r8152; the hardware report separates Ethernet negotiation from USB bus speed. The fan supports on/off control with automatic 55°C activation and 47°C release. Invalid temperature readings force it on. RPM feedback is not claimed.

The image contains the reviewed `rtl_nic/rtl8153b-2.fw` firmware in the normal system, both Rescue bases and initramfs. Its original Realtek license is installed under `/usr/share/doc/armhpc-firmware`. Missing firmware does not necessarily prevent basic r8152 operation, but omits the applicable firmware patch. Firmware loading does not change USB bus wiring or negotiated USB speed. The current device tree assigns the shared high-speed PHY to ASM1064 PCIe; NET1's observed 480M bus is not advertised as full gigabit throughput.

The performance service applies the stock CPU/devfreq limits and performance governor. A timer retries after boot and records `/run/armhpc/performance.json` so delayed policy discovery is visible. Thermal protection remains active. This is not overclocking.

The common commands include persistent normal-system hostnames, guided network changes with SSH login confirmation and rollback, Bash completion, K3s role configuration/reset/reinstall, clock recovery, signed maintenance, task retry/cancel and baseline restoration. Rescue hostname handling remains independent. Updates retain the selected DATA partition and device identity; task and storage checks still apply.

### System and Rescue OTA

Same fixed-kernel, fixed-DTB and fixed-initramfs packages update the normal system and inactive Rescue slot through the shared engine. Upload the signed OTA archive with the Windows/Linux controller; select `system`, `rescue` or `all`. An interrupted transfer or reconnect continues the original task. The ordinary system deploys while running Rescue, with configuration retention and health confirmation before boot selection is committed.

### Kernel and DTB maintenance

OESP uses `armhpc[-rescue] ota boot plan/apply/status/rollback` for a signed, paired BOOT candidate. FAT32 snapshots, versioned boot files, vendor `autoscr` entries, raw initramfs identity and legacy ramdisk CRCs are checked before switching an entry. Export the verified private BOOT backup and recovery image to the controller before applying. BOOT writes run in Rescue and exclude the Amlogic FIP, environment reservation, boot0/boot1, DATA and partition table. [USB recovery](USB-RECOVERY.md) describes volatile Rescue startup and the task-bound `ota boot restore` command.

A candidate with an installed SATA system must include its matching signed system payload. After the candidate Rescue and required reset checks are accepted, `ota boot apply --commit --request-id <id>` submits the paired normal-system update to the existing OTA engine. The BOOT task is committed only after that system job passes its health checks. Rollback after a partial system write keeps ordinary-system boot blocked and selects the previous confirmed Rescue.

Natural watchdog expiry and panic recovery are separate from ordinary reboot. `hardware_verified` remains false until the corresponding real-device checks are completed. An unverified kexec handoff is not enabled by BOOT maintenance or a successful ordinary reboot.

### Optional USB console

The image includes the DWC2/gadget support and authenticated USB ACM console. It starts manually using `armhpc[-rescue] usb-console start --json`; `stop --json` restores the previous USB role. Automatic startup is disabled on OESP. The role change refuses a host port used by NET1 and other gadget owners. Confirm the physical OTG connector and enumeration before relying on this channel. A Linux gadget cannot provide logs from DDR or U-Boot.

## Build

Use the pinned Linux 6.12.111 archive and the source integration under this board profile with `tools/build/build-oesp-kernel.py`. Generate the local prefix with `tools/build/prepare-oesp-prefix.py`, using the pinned ampart source and private OESP loader skeleton. Prepare fresh ARM64 Bookworm roots using `tools/build/prepare-oesp-rootfs.py`, providing a trusted Debian archive keyring, the public OTA trust key and a private local administrator hash input. Assemble with `tools/build/assemble-oesp-squashfs.py` and package with `tools/build/package-image.py`. All work, private boot inputs and output paths must be outside source repositories.

The matching system OTA archive contains signed manifests and both payloads. It excludes the vendor boot chain. Use the common Windows/Linux controller to upload it through SSH/SFTP.

The root preparation tool defaults to `--target wxy-oesp-v22-k3s`. Use `--target wxy-oesp-v22-base` and a separate work directory for the base system without the K3s component. Do not reuse a root preparation directory across workload targets.

### USB Burning Tool container export

`tools/build/package-oesp-burn.py` exports a verified, signed complete raw image for USB Burning Tool 2.2.4. Run it on Linux/WSL with `--image`, `--manifest`, `--trust`, `--ota-source`, `--reference-image`, `--packer`, `--output` and an IMG basename in `--name`. The output directory must be new. The exporter produces the native IMG, its XZ compression, SHA256 and `BURN-EXPORT.json`.

The reference image is `onethingcloud-oes-plus-usbboot.burn.img` from [the OESP Scheme C archive](https://github.com/ophub/kernel/releases/download/tools/wxy-oes-plus-ubuntu-s922x-usbboot.tar.xz); its SHA256 is pinned in the exporter. Ordinary DDR/U-Boot and `meson1` DTB have distinct inputs from their encrypted counterparts. The encrypted FIP also must match the permanent FIP in the signed raw image. Never fill all ordinary and encrypted roles with one file.

Only `PARTITION:1` writes the completed raw image. Separate `PARTITION:bootloader`, `PARTITION:_aml_dtb` and key provisioning entries are excluded. The container CRC, embedded image SHA256 and XZ stream are checked. Keep both erase options, reset-after-burn and overwrite-keys unchecked for this full-image package; the options for the reference USB-boot-only package do not apply. Use a direct PC USB connection and disconnect USB and power after a successful burn, then cold boot. Export validation does not certify device enumeration or a successful physical flash.

### Vendor environment format

The pinned vendor FIP reads 64 KiB environment blocks with a four-byte little-endian CRC32 followed immediately by the environment data. Both local copies at 4 MiB and 4 MiB + 64 KiB use this format. Generate them without the `mkenvimage -r` redundancy flag; that flag inserts another byte and changes the CRC coverage. The prefix generator and native package exporter validate this format before delivery.

The compatibility evidence is the first-117-MiB OESP backup from the same vendor FIP, whose two environment blocks at 116 MiB have the four-byte header. Source and image checks are recorded separately from cold-boot results. A successful USB Burning Tool transfer does not establish that Linux, network configuration or SSH has started.

### OESP vendor boot compatibility

The permanent environment uses the `oesp-autoscr-v1` entry. It loads `boot.scr` from `mmc 1` at `0x01020000` and executes `autoscr 1020000`, following the [OESP vendor autoscript](https://github.com/retro98boy/amlogic-fip-blobs/blob/4312e61a057ffc409b2c8ffac5f6e97b26c7be92/onethingcloud-oes-plus/aml_autoscript.cmd). If eMMC loading returns, the entry scans USB devices 0–3 and passes the selected device to the same script. The published vendor command list exposes `autoscr`; do not substitute `source`. The prefix generator and native exporter validate the complete entry as well as its CRC and both environment copies. The script buffer is included in the kernel/DTB/initramfs memory-overlap check.

The environment is generated from an allowlisted profile. It does not import a reference device's MAC, serial number, network address, Android preboot actions or `storeboot` fallback. FIP remains at user-area sector 1, as in the OESP Armbian image layout. A blank boot0/boot1 readback alone is not evidence that this user-area boot path is invalid. Adding a separate native bootloader item would introduce the vendor `save_setting` flow: its cached Android EPT can direct an environment write into the Rescue region. The current whole-image export therefore keeps that operation excluded and reports its boot-area scope in `BURN-EXPORT.json`.

The generated reserved area contains only the minimal EPT. Its SHA256 is `e8946f1e4fafb0ef39b3452a560630a8384aa9e5bd243b950a325f59cce5d717`, matching the pinned [OESP Armbian reserved area](https://github.com/retro98boy/amlogic-fip-blobs/tree/4312e61a057ffc409b2c8ffac5f6e97b26c7be92/onethingcloud-oes-plus). Do not insert the encrypted Android partition DTB into this area: vendor storage initialization can derive and replace partition metadata from it. The DTB used for USB loader initialization stays in the native container's temporary loader role; the Linux DTB is loaded from FAT32 BOOT.

The OESP vendor entry loads `Image` at `0x08080000`, the Linux DTB at `0x08008000` and `uInitrd.uboot` at `0x13000000`, with `fdt_high=0x20000000`. These follow the [OESP Armbian boot script](https://github.com/retro98boy/armbian-build/blob/f9ed2d650a2f37f885bd5dd01746d9dc8bdb4d6d/config/bootscripts/boot-cainiao-xiaoyi-pro.cmd). The kernel retains the legacy Amlogic text-offset patch; the assembler checks both the load and final entry ranges for overlap.

`uInitrd.uboot` has a Linux/ARM legacy ramdisk header, including header and payload CRC32. Its payload is byte-identical to the raw `uInitrd`. The vendor command uses `booti <kernel> <ramdisk> <dtb>` without a raw-size argument. OTA manifests, independent Rescue boot copies and the fixed-boot identity continue to bind the raw initramfs; they do not include the vendor envelope. The exporter also verifies the minimal reserved area before producing a native image.

Full USB transfer, file hashes and host-side envelope checks do not establish a cold-boot result. If a corrected entry still produces neither a Linux network connection nor a distinct kernel LED state, collect real hardware UART output or read-back evidence before changing another board setting. The Linux USB gadget cannot capture DDR, BL31 or U-Boot execution.
