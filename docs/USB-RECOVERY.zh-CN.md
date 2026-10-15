<!-- SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only -->
# OESP USB 恢复

[English](USB-RECOVERY.md)

应用 BOOT 候选前，先导出并校验私有 BOOT 备份。新版导出包含 `BOOT-export.json`、`BOOT-original.img`、`BOOT-rescue.img` 和其 XZ 文件。清单绑定原任务、持久设备 ID、实际 eMMC CID 哈希、BOOT 范围和签名启动配套文件。请保管这些文件。

Linux 无法启动时，只连接一台进入 USB 下载模式的 OESP，关闭 USB Burning Tool。Windows 使用已安装的 WorldCup/libusb0 驱动，上位机需要以管理员身份运行。工具不会安装驱动、烧录密钥或执行擦除。

GUI 点击“**OESP USB 恢复**”，选择私有导出清单及随包 USB 工具。上位机 CLI 的等价操作为：

```sh
armhpc-ota-cli usb-rescue --usb-tool-directory /path/to/ARMHPC-OESP-USB --export /private/BOOT-export.json --prepare --json
armhpc-ota-cli usb-rescue --usb-tool-directory /path/to/ARMHPC-OESP-USB --export /private/BOOT-export.json --yes --json
```

Windows 使用 `ARMHPC-OTA-CLI.exe` 和 Windows 路径。也可直接运行 `oesp-usb-rescue.ps1 -Export <绝对路径>`；Linux 可运行 `./oesp-usb-rescue.sh boot --export <绝对路径> --load-ram --yes`。

工具从经过校验的 FAT32 恢复镜像取出原内核、DTB 和签名恢复 initramfs，限定内存范围上传并回读 SHA256，然后提交 `booti`。整个 USB 步骤不发送存储写入命令。`awaiting-linux-confirmation` 只表示已记录提交，仍需按原设备身份重连、查询原任务。不能仅凭 USB 消失认定启动成功，也不要因此重复提交。

强制恢复入口选择保留的已确认 Rescue，绕过失败的候选指针，暂停自动部署。MBR、保留的签名 Rescue 及共享状态完整时，可绕过 BOOT 标签缺失或文件系统无法挂载的问题。普通 OTA 仍要求正常 BOOT 文件系统。

Linux 重连后按原任务查询、预览、恢复 BOOT，再回滚启动入口：

```sh
armhpc-rescue ota status --json
armhpc-rescue ota boot status --json
armhpc-rescue ota boot restore --request-id <原任务ID> --json
armhpc-rescue ota boot restore --request-id <原任务ID> --execute --yes --json
armhpc-rescue ota boot rollback --request-id <原任务ID> --yes --json
```

GUI 对应“**恢复 BOOT**”及“**BOOT 回滚**”。设备核对实际 eMMC CID、设备／任务绑定、正在运行的保留槽、MBR 中精确 BOOT 范围和备份哈希，只回写 BOOT 分区并回读校验。共享身份、时间、DATA、FIP、环境和分区元数据保持。若 SATA 系统已有部分写入，回滚后仍阻止选择该系统启动。恢复中断后可继续原任务和原备份。

旧 agent 导出若缺少恢复清单和 CID 绑定，不能使用该操作。早期引导、MBR、保留槽或共享区损坏，需要相应备份和刷机恢复路径。USB 内存交接与 BOOT 损坏恢复仍需实机确认；软件检查不会改变 `hardware_verified`。
