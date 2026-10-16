# ARMHPC

[简体中文](README.zh-CN.md)

ARMHPC **1.2.1** is a modular Linux firmware and device-management framework for ARM64 and ARM32 systems. It provides configurable normal-system and Rescue environments, reusable system components, and board-specific build profiles.

OESP OES1PLUS v2.2 adds S922X, dual Ethernet, ASM1064 SATA, FAT32 BOOT, fan control and board-specific LEDs through its own target profile.

## Architecture

Each target combines a SoC, board, build backend, operating system, storage layout, and workload. Common device-management and recovery components are reused across targets; board profiles define the boot chain, drivers, interfaces, and partition requirements.

WXY OEC Turbo (OECT) is the first integrated device. Its current firmware uses SquashFS Rescue A/B, a shared writable area, and a separate ext4 normal system and DATA partition. These storage choices belong to that target's profile. Additional boards supply their own integration and compatible firmware packages through the same framework.

## Features

- Configurable firmware composition for ARM64 and ARM32, with explicit source and component selection.
- Normal-system and independent Rescue environments, installation, recovery, and shared device identity.
- Signed OTA integration, Rescue A/B, offline system deployment, and persistent recovery records.
- Network and administrator initialisation, SSH access and clock recovery.
- Persistent normal-system hostnames and guided IPv4/DNS configuration with confirmation and timed rollback.
- Board-configured status and Ethernet LEDs, green-port identification and Bash command completion.
- USB OTG serial access on targets with a supported device-mode controller.
- Optional workload components, including K3s installation, cluster configuration, node-local reset/reinstall and a pinned Rancher China download source.

| Command | Purpose |
| --- | --- |
| `armhpc` | Manage the normal system. |
| `armhpc-rescue` | Manage Rescue, installation, and recovery. |
| `armhpc-kube` | Manage the optional K3s workload. |

The shared OTA engine and controller software are maintained in [armhpc-ota](https://github.com/ArmbianHPC/armhpc-ota).

## Configure a build

List the available targets, then choose a target for the intended device:

```sh
python3 scripts/image-framework.py list
python3 scripts/image-framework.py validate <target>
python3 scripts/image-framework.py plan <target>
python3 scripts/image-framework.py stage <target>
```

Replace `<target>` with an ID from the list; `wxy-oect-k3s` is the first supplied board profile. Add new SoC, board, and target definitions using the [integration guide](docs/ADAPTERS.md). The configuration model supports both architectures; each board still needs matching boot files, drivers, and hardware verification.

## Downloads

[Releases](https://github.com/ArmbianHPC/armbian-hpc/releases) use version-only titles such as `v1.0.0`. Firmware packages identify the device model and software combination:

```text
armhpc_<version>_<model>_<distribution>_<kernel>_<filesystem>_<build-date>.img.xz
armhpc-ota_<version>_<model>_<distribution>_<kernel>_<filesystem>_<build-date>.tar.xz
```

The first file is a compressed flashing image. The second contains signed normal-system and Rescue payloads for the OTA controller. The build date is `YYYYMMDD` and reflects the actual compilation date. Choose the package matching the device's target profile. Firmware packages and private build inputs are kept outside this source repository.

## Initial access

The supplied v1.2.0 images use `root` with initial password `1`. Sign in through SSH and run `passwd` to set your password. Existing devices retain their login configuration during OTA.

## Documentation

- [Independent versions and compatibility](docs/COMPATIBILITY.md)

- [Device compatibility](docs/DEVICES.md)
- [Contributing](CONTRIBUTING.md)
- [Maintenance](docs/MAINTENANCE.md)
- [Questions and ideas](https://github.com/ArmbianHPC/armbian-hpc/discussions)

- [OES1PLUS v2.2 integration](docs/OESP.md) / [简体中文](docs/OESP.zh-CN.md)

- [Building](docs/BUILD.md)
- [Board and component integration](docs/ADAPTERS.md)
- [Device commands](docs/COMMANDS.md)
- [Sources and firmware inputs](docs/SOURCES.md)
- [Release policy](docs/RELEASE.md)
- [v1.2.0 release notes](docs/releases/v1.2.0.md) / [简体中文](docs/releases/v1.2.0.zh-CN.md)
- [Known issues](docs/KNOWN-ISSUES.md)

## License

Independent original framework code is licensed under **GPL-2.0-only**. Original modules shared with the OTA process use **GPL-2.0-only OR GPL-3.0-only**. Third-party sources retain their original licenses, authors, and provenance. See [LICENSE](LICENSE), [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), and [LICENSE-MAP.json](LICENSE-MAP.json).

MiniLoader, signing keys, credentials, device backups, and upstream K3s binaries are not included in the source repository.
