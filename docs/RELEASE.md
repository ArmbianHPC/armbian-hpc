# Firmware releases

[简体中文](RELEASE.zh-CN.md)

Use the tag as the release title, for example `v1.2.1-r1`, with a lowercase `r`. Release descriptions use concise English change notes from `docs/releases/<tag>.md`, with a link to `<tag>.zh-CN.md` for Simplified Chinese.

## Attachments

Upload one flashing image and one signed system OTA package per selected model. The selected OECT and OESP package set contains four files in total. Both use XZ compression:

```text
armhpc_<version>_<model>_<distribution>_<kernel>_<filesystem>_<build-date>.img.xz
armhpc-ota_<version>_<model>_<distribution>_<kernel>_<filesystem>_<build-date>.tar.xz
```

Use the actual `YYYYMMDD` build date, the kernel version without its package suffix, and the model from the selected target. Example: `armhpc_1.0.0_wxy-oect_bookworm_6.12.111_squashfs_20261011.img.xz`.

Include signed manifests, payload hashes, license materials, build provenance, and corresponding source information inside the OTA archive or image. Additional checksums, reports, source archives, and inventories are not uploaded as separate release assets. GitHub generates its automatic Source code links.

Provide one --manifest JSON with the tag and all packages (file, size, sha256, plus reviewed redistribution evidence for each IMG). The publishing policy verifies the complete local package set and their uploaded hashes before removing obsolete attachments. Existing versioned files with different bytes must not be overwritten.

## Build and source records

Use `image-framework.py check-source --ref REF` and `prepare-source-release --ref REF --output OUT` to inspect and export source. A source tag and a device system release ID are separate identifiers; firmware records identify their exact source commits and signed payload sequence.

Firmware preparation verifies matched kernel, modules, DTB, and raw initramfs inputs. OECT image assembly uses the SquashFS A/B layout; permanent boot components, package licenses, corresponding sources, identity sanitisation, and partition boundaries must be checked before public distribution. Keep firmware binaries, private keys, credentials, MiniLoader, and node backups outside Git.


## Release numbering

Use `vA.B.C-rN`, with a lowercase `r` and a positive revision number. For each repository independently, advance the base version by one patch, one minor with patch reset to zero, or one major with minor and patch reset to zero. Examples: `1.2.3` to `1.2.4`, `1.3.0`, or `2.0.0`. Within the same base version, revisions increase but need not be consecutive: `r1`, `r3`, and `r9` may be published directly. Previously published numeric-only tags remain supported.

The release title equals its tag. Artifact filenames include the revision. Compatibility comparisons continue to use the numeric base version, explicit capabilities and the signed manifest requirements. Firmware signature sequences still increase independently. Publication checks the repository's published release history before any upload or metadata change. Existing versioned assets remain immutable.
