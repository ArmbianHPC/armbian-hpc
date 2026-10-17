# Framework builds

[简体中文](BUILD.zh-CN.md)

Check out compatible framework and OTA commits; their release versions may differ. See [independent versions](COMPATIBILITY.md). Use `scripts/image-framework.py list`, `validate`, `plan`, `stage` and `fetch-sources` to prepare target inputs. Build outside source repositories.

OECT uses GPT and ext4 BOOT; OESP uses a separately audited Amlogic reservation, MBR and FAT32 BOOT. Both targets provide SquashFS Rescue A/B, shared ext4, and a SATA ext4 normal system and DATA partition. Use the board-specific kernel/root preparation and assembly entries under `tools/build`.

## Fixed BOOT software refresh

Use `tools/build/refresh-fixed-boot-roots.py` with explicit `--project`, `--ota-source`, `--input-roots`, `--boot-manifest`, `--work`, `--watchdog-binary`, `--board`, `--release` and `--epoch` inputs. It verifies all three BOOT hashes and matching modules in four roots before and after software refresh. It does not regenerate initramfs. This path creates software OTA updates compatible with an existing fixed BOOT pair. A changed kernel or DTB requires the separate paired BOOT maintenance path.

Run `tools/build/prepare-firmware-inputs.py` before packaging shared runtime resources. Board firmware is downloaded from its pinned source and checked against its hash and redistribution record.

## Packaging

Generate signed payloads with the board assembler. `tools/build/prepare-firmware-package.py` takes exact committed project sources, signed artifacts, sanitized public roots, a hash-pinned corresponding-source manifest and build provenance. It includes project/kernel/retained-BOOT sources, distribution package versions, copyright files and licenses in the OTA transport.

`tools/build/pack-firmware.py` compresses and verifies the complete IMG and canonical OTA archive. For OESP, first export the signed raw image through `package-oesp-burn.py` as the native USB Burning Tool container, then pass that container to the common packager. Include the kernel configuration and patches for the retained boot pair. Keep private boot inputs and credentials outside Git. Public image distribution requires actual component license evidence.

```sh
python3 -m pip install -r requirements-test.txt
PYTHONPATH=../armhpc-ota/src python3 -m pytest tests
```

The complete suite uses pytest to collect both unittest and function-based checks. Firmware publish uses a single manifest listing both files for every model, including their exact sizes, hashes and image redistribution evidence. See [release policy](RELEASE.md).

For offline builds, pass --from-directory VERIFIED_FIRMWARE_DIRECTORY to firmware preparation. The input bytes must match the same board SHA256 and license record; this option does not change trust or permit unreviewed firmware.

For an explicit repeat build from previously verified roots, the private firmware build input can set `offline_dependencies: true`. The kernel-root rebase then verifies that every declared dependency is installed and records its exact version in each root. Missing packages fail the build; network updates remain the default.
