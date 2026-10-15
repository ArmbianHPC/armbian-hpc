# Device compatibility

[简体中文](DEVICES.zh-CN.md)

ARMHPC is a modular framework. A board needs its own profile, boot chain, kernel/DTB, storage policy and verification. The supplied images below are ARM64 Debian Bookworm with the matching Linux 6.12.111 kernel. Additional models or PCB revisions require their own integration.

| Device | Profile and hardware | Image and storage | Interfaces and limits |
| --- | --- | --- | --- |
| WXY OEC Turbo, original eMMC | `wxy-oect-original`; RK3566, 4 GiB RAM and 8 GB eMMC | Rockchip raw GPT image; 512 MiB BOOT, 512 MiB SquashFS Rescue A/B, shared ext4; SATA normal-system ext4 64 GiB plus DATA | Ethernet, supported USB2 OTG serial and board LEDs. Natural watchdog timeout recovery has a known failure. |
| OES1PLUS `oes1plus-v2.2` | `wxy-oesp-v22`; S922X, 4 GiB RAM and 8 GB eMMC | USB Burning Tool 2.2.4 container after XZ decompression; MBR/EPT, FAT32 BOOT, SquashFS Rescue A/B and shared ext4; selected SATA disk gets 64 GiB ext4 system plus DATA | NET0 RTL8211F; NET1 RTL8153B currently enumerates at USB 2.0 480 Mbit/s. NET0 identification, status LEDs, GPIO fan control and performance policy are included. USB gadget console is disabled by default. |

## Choose a package

- Download a board-matching image or system OTA archive from [Releases](https://github.com/ArmbianHPC/armbian-hpc/releases/latest).
- Fresh images use `root` / `1`. Sign in and run `passwd`. Identity and SSH host keys are generated on the device.
- Use [armhpc-ota](https://github.com/ArmbianHPC/armhpc-ota/releases/latest) for the Windows/Linux controller and signed tool maintenance. A tool maintenance package does not replace the normal system or Rescue base image.
- Existing OTA installations retain the configured credentials and preserved identity/network settings. The normal-system hostname is retained according to the documented preservation policy.
- Select SATA targets explicitly and verify their persistent identity. Follow the board's installation and recovery guide before writing storage.

## Observations and acceptance

OESP 1.1.4 operator reports covered Rescue startup, both Ethernet interfaces, SATA-system recovery installation, A/B OTA switching and preserved device identity, hostname, network, SSH keys and a DATA marker. These observations do not constitute a new device run for 1.2.0. The 1.2.0 packages were checked for source pairing, software behavior, signatures, payload hashes and image packaging; no new physical-node run was performed during that release.

Hardware-watchdog reset, kexec handoff and early target-kernel failure recovery retain their individual recorded states. `hardware_verified` is not inferred from compilation, automated checks or an ordinary reboot. Read [known issues](KNOWN-ISSUES.md), [OESP integration](OESP.md), [commands](COMMANDS.md) and [sources](SOURCES.md).
