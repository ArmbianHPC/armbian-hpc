# Armbian HPC

ARM64／ARM32 Linux 镜像配置与构建框架，版本 **1.0.0**。框架独立原创代码采用 GPL-2.0-only；与 OTA 共用的原创模块使用兼容双许可；第三方文件保留原许可证。

## OECT 布局与功能

| 介质 | 内容 |
| --- | --- |
| eMMC BOOT | 512 MiB ext4，内核、DTB 和 raw initramfs |
| eMMC Rescue A/B | 各 512 MiB SquashFS，按槽位使用可写层 |
| eMMC 共享区 | 剩余 ext4，身份、启动控制、恢复基线及维护记录 |
| SATA 系统 | 64 GiB ext4 |
| SATA DATA | 剩余 ext4 |

系统提供网络身份初始化、MAC 存储区读取及有效随机回退、按 MAC 后四位命名、SSH、USB2 OTG 虚拟串口、RGB 状态灯、PHY 链路灯、SATA 安装／恢复及在线 K3s 配置。

普通系统使用 `armhpc`，Rescue 使用 `armhpc-rescue`，K3s 使用 `armhpc-kube`。OTA 核心独立位于 `armhpc-ota`，框架仅提供接入与构建。

## 配置入口

```sh
python3 scripts/image-framework.py list
python3 scripts/image-framework.py validate wxy-oect-k3s
python3 scripts/image-framework.py plan wxy-oect-k3s
python3 scripts/image-framework.py stage wxy-oect-k3s
```

OECT 目标使用原装 4 GB + 8 GB 适配。其他板卡通过独立 SoC、板卡、后端、存储与应用配置接入；ARM32 提供架构映射及适配模板。

## 源码与发行

本仓库不包含镜像、MiniLoader、私钥、用户身份或上游 K3s 二进制。镜像及压缩包作为对应 Release 附件分发。引导输入由用户提供，或按组件许可与对应源码记录接入。

源码发布与完整镜像发布分别检查。本次 1.0.0 提供框架源码，完整 IMG 和系统 OTA 载荷另行构建。

见 [构建](docs/BUILD.md)、[适配](docs/ADAPTERS.md)、[命令](docs/COMMANDS.md)、[源码与固件](docs/SOURCES.md)、[发布](docs/RELEASE.md)、[许可](THIRD_PARTY_NOTICES.md)、[已知问题](docs/KNOWN-ISSUES.md)。
