# ARMHPC

[简体中文](README.zh-CN.md)

ARMHPC **1.0.0** is a configurable Linux firmware framework for ARM64 and ARM32 devices, with normal-system and Rescue environments.

## OECT support

The current OECT target is the original WXY OEC Turbo with 4 GB RAM and 8 GB eMMC.

| Storage | Contents |
| --- | --- |
| eMMC BOOT | 512 MiB ext4 with kernel, DTB, and raw initramfs |
| eMMC Rescue A/B | Two 512 MiB SquashFS slots, each with its own writable layer |
| eMMC shared area | Remaining space as ext4 for identity, boot control, recovery packages, and maintenance records |
| SATA system | 64 GiB ext4 |
| SATA DATA | Remaining SATA space as ext4 |

Features include device identity and MAC initialisation, hostnames based on the MAC suffix, SSH, USB2 OTG serial console, status and Ethernet LEDs, SATA installation and recovery, clock recovery, and K3s configuration.

Use `armhpc` in the normal system, `armhpc-rescue` in Rescue, and `armhpc-kube` for K3s operations. The shared OTA engine is maintained in [armhpc-ota](https://github.com/ArmbianHPC/armhpc-ota).

## Configure a build

```sh
python3 scripts/image-framework.py list
python3 scripts/image-framework.py validate wxy-oect-k3s
python3 scripts/image-framework.py plan wxy-oect-k3s
python3 scripts/image-framework.py stage wxy-oect-k3s
```

Targets select a SoC, board, backend, operating system, storage layout, and workload. Other boards require their own integration. ARM32 uses the same configuration model with the corresponding architecture and helpers.

## Downloads

[Releases](https://github.com/ArmbianHPC/armbian-hpc/releases) use version-only titles such as `v1.0.0` and contain two firmware packages:

```text
armhpc_<version>_<model>_<distribution>_<kernel>_<filesystem>_<build-date>.img.xz
armhpc-ota_<version>_<model>_<distribution>_<kernel>_<filesystem>_<build-date>.tar.xz
```

The first file is a compressed flashing image. The second contains signed system and Rescue payloads for the OTA controller. The build date is `YYYYMMDD` and reflects the actual compilation date. Firmware packages and private build inputs are kept outside this source repository.

## Documentation

- [Building](docs/BUILD.md)
- [Board and component integration](docs/ADAPTERS.md)
- [Device commands](docs/COMMANDS.md)
- [Sources and firmware inputs](docs/SOURCES.md)
- [Release policy](docs/RELEASE.md)
- [v1.0.0 release notes](docs/releases/v1.0.0.md) / [简体中文](docs/releases/v1.0.0.zh-CN.md)
- [Known issues](docs/KNOWN-ISSUES.md)

## License

Independent original framework code is licensed under **GPL-2.0-only**. Original modules shared with the OTA process use **GPL-2.0-only OR GPL-3.0-only**. Third-party sources retain their original licenses, authors, and provenance. See [LICENSE](LICENSE), [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), and [LICENSE-MAP.json](LICENSE-MAP.json).

MiniLoader, signing keys, credentials, device backups, and upstream K3s binaries are not included in the source repository.
