# Firmware releases

[简体中文](RELEASE.zh-CN.md)

Use the tag as the release title, for example `v1.0.0`. Release descriptions use concise English change notes from `docs/releases/<tag>.md`, with a link to `<tag>.zh-CN.md` for Simplified Chinese.

## Attachments

Upload only the flashing image and the signed system OTA package. Both use XZ compression:

```text
armhpc_<version>_<model>_<distribution>_<kernel>_<filesystem>_<build-date>.img.xz
armhpc-ota_<version>_<model>_<distribution>_<kernel>_<filesystem>_<build-date>.tar.xz
```

Use the actual `YYYYMMDD` build date, the kernel version without its package suffix, and the model from the selected target. Example: `armhpc_1.0.0_wxy-oect_bookworm_6.12.111_squashfs_20261011.img.xz`.

Include signed manifests, payload hashes, license materials, build provenance, and corresponding source information inside the OTA archive or image. Additional checksums, reports, source archives, and inventories are not uploaded as separate release assets. GitHub generates its automatic Source code links.

The publishing policy verifies the two local packages and their uploaded hashes before removing obsolete attachments. Existing versioned files with different bytes must not be overwritten.

## Build and source records

Use `image-framework.py check-source --ref REF` and `prepare-source-release --ref REF --output OUT` to inspect and export source. A source tag and a device system release ID are separate identifiers; firmware records identify their exact source commits and signed payload sequence.

Firmware preparation verifies matched kernel, modules, DTB, and raw initramfs inputs. OECT image assembly uses the SquashFS A/B layout; permanent boot components, package licenses, corresponding sources, identity sanitisation, and partition boundaries must be checked before public distribution. Keep firmware binaries, private keys, credentials, MiniLoader, and node backups outside Git.
