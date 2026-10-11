# Firmware packaging

Prepare matched roots and signed payloads with the OECT build tools. The controller archive uses
`releases/<release-id>/manifest.json` and `blobs/<sha256>`. Include the corresponding sources and
license notices under `corresponding-sources/` and `licenses/`.

Use explicit top-level names when creating the tar archive. Entries starting with `./`, a root
`.` entry, symbolic links, or paths containing `..` are rejected by the controller.

`tools/build/pack-firmware.py` checks the staging signature and all eight system/Rescue artifacts,
creates the two XZ packages, and checks the complete decompressed image hash and controller archive.
Existing output files are preserved; use `--verify-only` to inspect them.

```sh
python3 tools/build/pack-firmware.py \
  --project /src/armbian-hpc --ota-source /src/armhpc-ota \
  --package-root /build/package --image /build/armhpc_1.0.0_wxy-oect_bookworm_6.12.111_squashfs_20261011.img \
  --output /build --trust /build/root/etc/armhpc/ota-trust.d \
  --stem armhpc_1.0.0_wxy-oect_bookworm_6.12.111_squashfs_20261011
```

Firmware filenames use the actual build date. Public distribution of a complete image additionally
requires the original permanent boot components' license and corresponding source records.
