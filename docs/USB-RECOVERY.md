<!-- SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only -->
# OESP USB recovery

[简体中文](USB-RECOVERY.zh-CN.md)

Keep a verified private BOOT export before applying a BOOT candidate. A new export contains `BOOT-export.json`, `BOOT-original.img`, `BOOT-rescue.img` and its XZ copy. The descriptor binds the original task, persistent device ID, physical eMMC CID hash, BOOT range and signed recovery files. Keep these files private.

If Linux no longer starts, connect one OESP in USB download mode. Close USB Burning Tool. The recovery tool uses its existing WorldCup/libusb0 driver on Windows; run the controller as administrator. No driver installation, key provisioning or erase operation is performed.

The GUI's **OESP USB 恢复** action selects the private descriptor and the delivered USB tool. The Linux and Windows host CLI provide the equivalent operation:

```sh
armhpc-ota-cli usb-rescue --usb-tool-directory /path/to/ARMHPC-OESP-USB --export /private/BOOT-export.json --prepare --json
armhpc-ota-cli usb-rescue --usb-tool-directory /path/to/ARMHPC-OESP-USB --export /private/BOOT-export.json --yes --json
```

On Windows use `ARMHPC-OTA-CLI.exe` with Windows paths. The helper also runs directly through `oesp-usb-rescue.ps1 -Export <absolute-path>` or `./oesp-usb-rescue.sh boot --export <absolute-path> --load-ram --yes`.

The helper verifies the exported FAT32 image and signed original kernel/DTB plus recovery initramfs, loads only their validated RAM windows, and verifies RAM SHA256 before submitting `booti`. It never issues storage write commands. `awaiting-linux-confirmation` means submission is recorded; it does not confirm a successful boot. Reconnect to the original persistent device ID and query the original task. Do not repeat a boot submission solely because USB disappeared.

The forced recovery entry selects the retained confirmed Rescue, ignores a failed candidate pointer and pauses automatic OTA. It can bypass a missing BOOT label or unmountable BOOT filesystem when the MBR, retained signed Rescue and shared state remain intact. This bypass is restricted to explicit recovery; ordinary OTA continues to require a valid BOOT filesystem.

After Linux reconnects, query and preview the BOOT-only restore, then execute it and restore the original task's boot entries:

```sh
armhpc-rescue ota status --json
armhpc-rescue ota boot status --json
armhpc-rescue ota boot restore --request-id <original-id> --json
armhpc-rescue ota boot restore --request-id <original-id> --execute --yes --json
armhpc-rescue ota boot rollback --request-id <original-id> --yes --json
```

The GUI provides **恢复 BOOT** followed by **BOOT 回滚**. Restoration checks the physical eMMC CID, original device/task, retained running slot, exact MBR BOOT range and saved image SHA256. It writes only that BOOT partition, verifies readback, and preserves shared identity/time, DATA, FIP, environment and partition metadata. A partially written SATA system remains blocked after rollback. A failed restore can continue with the original task and image.

Exports made by earlier agents without the recovery descriptor/CID binding cannot use this operation. An intact signed Rescue and shared state are required; damaged early loaders, MBR, retained slot or shared state need their separate backup/flash recovery procedure. Real USB RAM handoff and damaged-BOOT recovery require device confirmation; software checks do not change `hardware_verified`.
