<!-- SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only -->
# OES1PLUS v2.2

## 1.1.2 功能

普通系统、Rescue A/B 和 initramfs 均包含经过来源和许可核对的 `rtl_nic/rtl8153b-2.fw`。原始 Realtek 许可安装在 `/usr/share/doc/armhpc-firmware`。缺少固件不一定阻止 NET1 基本联网，但相应固件修正未应用；补固件不会改变 USB 总线速度。当前高速 PHY 分配给 ASM1064 PCIe，实际显示的 NET1 `480M` 不作为千兆吞吐承诺。

性能服务应用原有 CPU/devfreq 频率上限和 performance 调频策略，并通过定时器重试开机后才出现的调频节点；状态在 `/run/armhpc/performance.json`。不超频，保留温度保护。

共用命令覆盖普通系统主机名保留、网络引导配置和 SSH 登录确认/回滚、Tab 补全、K3s 角色向导及重装、时间恢复、签名维护、任务重试/取消及基线恢复。Rescue 主机名独立处理；更新仍保护所选 DATA 和设备身份。

内核、DTB 和原始 initramfs 配套不变时，通过 Windows/Linux 上位机上传签名 OTA 包，选择 system、rescue 或 all。普通系统在 Rescue 中部署，断流和重连继续原任务，健康确认通过后提交启动选择。

修改内核或 DTB 使用 `armhpc[-rescue] ota boot plan/apply/status/rollback`。流程校验 FAT32、版本化启动文件、厂商 autoscr 入口、原始 initramfs 和 ramdisk CRC；写入前要求导出已校验的本机 BOOT 备份与恢复镜像。写入限定在 Rescue，不修改厂商 FIP、环境保留区、boot0/boot1、DATA 或分区表。

已有 SATA 普通系统时，BOOT 候选须包含配套签名系统载荷。候选 Rescue 和必要复位检查通过后，`ota boot apply --commit --request-id <id>` 交给现有 OTA 引擎部署普通系统；系统健康确认后才提交 BOOT 任务。系统写入中断后的回滚仍阻止半更新系统启动，并选择原确认 Rescue。

真实看门狗超时和 panic 恢复与普通重启分别验证。`hardware_verified` 保持真实，BOOT 维护不自动放行未经验证的 kexec。

USB ACM 串口需手动执行 `armhpc[-rescue] usb-console start --json`；`stop --json` 恢复原 USB 角色。OESP 默认不开机启动，角色切换拒绝占用 NET1 的 Host 端口及其他 gadget。实际 OTG 接口、枚举和登录仍需在主板上确认。

[English](OESP.md)

## 指示灯

普通系统和 Rescue 正常运行时，电源状态灯均为绿色；检查发现错误时为红色。Rescue 不要求挂载 SATA DATA。机身网络灯在 NET0 或 NET1 任一端口有链路时显示绿色，两个端口都没有链路时显示红色；该指示表示本地链路，不代表互联网连通性。

NET0 使用 PHY LED1 控制绿色链路灯，LED2 控制黄色 RX/TX 活动灯，该映射已在 `oes1plus-v2.2` 主板上确认。活动灯不设置链路常亮规则。`python3 /usr/lib/armhpc/phy_leds.py --status` 读取当前 sysfs 规则与硬件控制状态。NET0 定位继续使用绿色 LED1。

适配 `oes1plus-v2.2` 主板：S922X、4 GiB DDR4、KLM8G1GETF eMMC、ASM1064、NET0 RTL8211F、NET1 RTL8153B。

eMMC 保留 100 MiB Amlogic 引导区，之后预置 512 MiB FAT32 BOOT、两个 512 MiB XZ SquashFS Rescue 槽和共享 ext4。所选 SATA 盘采用独立 GPT、64 GiB ext4 系统分区和剩余 ext4 DATA。

## 首次安装

通过 Amlogic USB 引导取得临时 Linux。USB 启动脚本不保存环境变量；临时系统禁止普通 OTA 和 SATA 安装。先将本机 eMMC 用户区、boot0、boot1 备份到容量充足的外接存储，并导出到电脑。

```sh
armhpc-rescue emmc backup --directory /mnt/backup --yes --json
armhpc-rescue emmc install --image /mnt/backup/armhpc.img --manifest /mnt/backup/image.manifest.json --backup /mnt/backup/armhpc-emmc-backup-DEVICE/backup.json --json
```

第二条只校验和预览。核对设备与备份后，添加 `--execute --yes` 写入。完成后移除启动 U 盘并断电冷启动。完整原始 `.img` 与 USB Burning Tool 的 `.burn.img` 格式不同。

### 不用 U 盘的 USB 直刷

完整线刷使用下文的原生 USB Burning Tool 镜像，不能将原始块设备镜像改名后导入。

已初始化 BOOT 候选的故障恢复使用[按原任务绑定的 USB Rescue 恢复](USB-RECOVERY.zh-CN.md)。它在内存中启动保留的签名配套文件，进入 Linux 核对设备身份后继续恢复。

旧 `oesp-usb-install.py` 依赖厂商 CID／EXT_CSD 读取命令，此前实机引导程序已拒绝这些命令；它的直接备份和写入路径不纳入交付的恢复工具。替换设备存储前，通过 Linux 的 `emmc backup` 接口取得完整私有备份。

## 功能与检查

```sh
armhpc-rescue ota status --json
armhpc-rescue ota doctor --json
armhpc-rescue identify --interface eth0 --seconds 10 --period-ms 1000 --json
armhpc-rescue fan status --json
armhpc-rescue fan set --mode auto --yes --json
```

普通系统使用对应 `armhpc` 命令。定位仅支持 NET0；NET1 报告实际 USB 总线速度，独立记录传输性能。风扇为启停控制，自动模式 55°C 开启、47°C 解除；温度读取失败时强制开启，不将标称速度当作转速反馈。

系统及 Rescue OTA 使用与固定 BOOT 配套的内核、DTB 和 initramfs；其他内核交接在硬件验收前拒绝。软件校验、模拟结果和本机实际开机、灯态、温控、盘位与恢复结果分别记录。

构建入口、输入及参数见英文文档。工作目录、私有输入和产物均位于源码仓库外。签名系统 OTA 包通过统一 Windows/Linux 上位机经 SSH/SFTP 上传。

根文件系统准备默认选择 `--target wxy-oesp-v22-k3s`；基础系统使用 `--target wxy-oesp-v22-base` 和独立工作目录。两种目标不能共用已经准备的根目录。

### USB Burning Tool 完整包导出

`tools/build/package-oesp-burn.py` 将已签名完整原始镜像导出为 USB Burning Tool 2.2.4 容器。Linux/WSL 下提供 `--image`、`--manifest`、`--trust`、`--ota-source`、`--reference-image`、`--packer`、`--output` 和 `--name` 指定的 IMG 文件名。必须使用新的输出目录；产物包含原生 IMG、XZ 压缩包、SHA256 和 `BURN-EXPORT.json`。

参考输入为 [OESP 方案 C 压缩包](https://github.com/ophub/kernel/releases/download/tools/wxy-oes-plus-ubuntu-s922x-usbboot.tar.xz) 中的 `onethingcloud-oes-plus-usbboot.burn.img`，SHA256 固定在导出器内。普通版 DDR/U-Boot、`meson1` DTB 与各自加密版使用不同文件；加密 FIP 必须与已签名原始镜像的永久 FIP 一致。不能将同一个文件填入所有普通版与加密版加载项。

完整原始镜像只通过 `PARTITION:1` 写入；不包含独立 `PARTITION:bootloader`、`PARTITION:_aml_dtb` 或密钥烧录项。导出检查容器 CRC、内嵌镜像 SHA256 和 XZ 完整性。此完整包取消“擦除 flash”“擦除 bootloader”“烧录成功后重启”和“覆盖烧录密钥”；参考底包的勾选方式不适用于此包。数据线直连电脑 USB，烧录成功后断开 USB 与电源，再冷启动。导出校验与实机枚举、刷写成功分别记录。

### 厂商环境格式

固定版本厂商 FIP 使用 64 KiB 环境块：前四字节为小端 CRC32，其后直接跟环境数据。4 MiB 与 4 MiB + 64 KiB 的两份本地副本均采用此格式。生成时不使用 `mkenvimage -r`；该选项会增加一个标志字节并改变 CRC 计算范围。前缀生成器与线刷导出器均在交付前校验这一格式。

兼容性依据是使用相同厂商 FIP 的 OESP 前 117 MiB 备份，其中 116 MiB 处两份环境块为四字节头。源码与镜像校验、冷启动结果分别记录。USB Burning Tool 返回传输成功，仅说明刷写过程结束，Linux、网络配置和 SSH 是否启动仍需设备证据。

### OESP 厂商引导兼容

永久环境采用 `oesp-autoscr-v1` 入口：按照 [OESP 厂商启动脚本](https://github.com/retro98boy/amlogic-fip-blobs/blob/4312e61a057ffc409b2c8ffac5f6e97b26c7be92/onethingcloud-oes-plus/aml_autoscript.cmd)，从 `mmc 1` 将 `boot.scr` 加载到 `0x01020000`，再执行 `autoscr 1020000`。eMMC 加载返回后扫描 USB 设备 0–3，并向同一脚本传递实际选中的设备。公开厂商命令清单提供的是 `autoscr`，不能替换为 `source`。前缀生成器和线刷导出器同时核对完整入口、CRC 和两份环境副本；脚本缓冲区纳入内核、DTB、initramfs 地址重叠检查。

环境由限定字段生成，不导入参考设备的 MAC、序列号、网络地址、Android preboot 动作或 `storeboot` 回退。FIP 继续位于用户区第 1 扇区，与 OESP Armbian 镜像布局一致。仅凭 boot0/boot1 读回为空不能认定用户区引导无效。单独增加原生 bootloader 烧录项会引入厂商 `save_setting` 流程，其缓存的 Android EPT 可能把环境写入定位到 Rescue 区域。因此当前整盘导出保持排除该操作，并在 `BURN-EXPORT.json` 明确记录硬件引导区的写入范围。

生成的保留区只包含最小 EPT，SHA256 为 `e8946f1e4fafb0ef39b3452a560630a8384aa9e5bd243b950a325f59cce5d717`，与固定版本的 [OESP Armbian 保留区](https://github.com/retro98boy/amlogic-fip-blobs/tree/4312e61a057ffc409b2c8ffac5f6e97b26c7be92/onethingcloud-oes-plus)一致。不能把加密的 Android 分区 DTB 填入这一区域：厂商存储初始化可能从它重新生成分区元数据。USB 下载阶段的厂商 DTB 只作为线刷容器的临时加载项；Linux DTB 从 FAT32 BOOT 加载。

OESP 厂商入口在 `0x08080000` 加载 `Image`、在 `0x08008000` 加载 Linux DTB、在 `0x13000000` 加载 `uInitrd.uboot`，并设置 `fdt_high=0x20000000`。这些地址采用 [OESP Armbian 启动脚本](https://github.com/retro98boy/armbian-build/blob/f9ed2d650a2f37f885bd5dd01746d9dc8bdb4d6d/config/bootscripts/boot-cainiao-xiaoyi-pro.cmd)的配置。内核保留 Amlogic 旧引导链的 text-offset 补丁；组装器同时检查加载地址和最终入口范围是否重叠。

`uInitrd.uboot` 使用 Linux/ARM 传统 ramdisk 头，包含头部和载荷 CRC32，载荷与原始 `uInitrd` 逐字节相同。厂商入口使用 `booti <kernel> <ramdisk> <dtb>`，不传原始载荷大小参数。OTA 清单、Rescue 独立启动副本和固定引导身份仍绑定原始 initramfs，不包含这个封装。原生线刷导出器同时校验最小 EPT 保留区。

USB 传输成功、文件哈希与上位机封装检查不能代替冷启动结果。如果修正入口后仍没有 Linux 网络连接或可区分的内核灯态，应先取得真实硬件 UART 输出或设备读回证据，再改变其他板级设置。Linux USB gadget 无法采集 DDR、BL31 或 U-Boot 的执行过程。
