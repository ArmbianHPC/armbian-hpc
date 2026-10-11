# ARMHPC

[English](README.md)

ARMHPC **1.0.0** 是面向 ARM64／ARM32 设备的 Linux 固件配置与构建框架，提供普通系统和 Rescue 环境。

## OECT 适配

当前 OECT 目标为原装 WXY OEC Turbo，配置为 4 GB 内存和 8 GB eMMC。

| 存储 | 内容 |
| --- | --- |
| eMMC BOOT | 512 MiB ext4，包含内核、DTB 和 raw initramfs |
| eMMC Rescue A/B | 各 512 MiB SquashFS，每槽具有独立可写层 |
| eMMC 共享区 | 剩余 ext4，用于身份、启动控制、恢复包和维护记录 |
| SATA 系统 | 64 GiB ext4 |
| SATA DATA | SATA 剩余空间，ext4 |

功能包括设备身份和 MAC 初始化、按 MAC 后四位命名、SSH、USB2 OTG 虚拟串口、状态灯及网口灯、SATA 安装与恢复、时间恢复和 K3s 配置。

普通系统使用 `armhpc`，Rescue 使用 `armhpc-rescue`，K3s 使用 `armhpc-kube`。共享升级核心位于 [armhpc-ota](https://github.com/ArmbianHPC/armhpc-ota)。

## 配置构建

```sh
python3 scripts/image-framework.py list
python3 scripts/image-framework.py validate wxy-oect-k3s
python3 scripts/image-framework.py plan wxy-oect-k3s
python3 scripts/image-framework.py stage wxy-oect-k3s
```

目标配置选择 SoC、板卡、后端、操作系统、存储布局及工作负载。其他板卡需要独立接入；ARM32 使用相同配置模型和对应架构、辅助程序。

## 下载

[Release](https://github.com/ArmbianHPC/armbian-hpc/releases) 标题只使用版本号，例如 `v1.0.0`，固件附件统一为两项：

```text
armhpc_<版本>_<设备型号>_<系统代号>_<内核版本>_<文件系统>_<编译日期>.img.xz
armhpc-ota_<版本>_<设备型号>_<系统代号>_<内核版本>_<文件系统>_<编译日期>.tar.xz
```

第一项为刷机镜像，第二项为包含普通系统和 Rescue 签名载荷的 OTA 包。编译日期使用实际日期，格式为 `YYYYMMDD`。固件包和私有构建输入保存在源码仓库外。

## 文档

- [构建](docs/BUILD.md)
- [板卡与组件接入](docs/ADAPTERS.md)
- [设备命令](docs/COMMANDS.md)
- [源码与固件输入](docs/SOURCES.md)
- [发布规则](docs/RELEASE.zh-CN.md)
- [v1.0.0 更新说明](docs/releases/v1.0.0.zh-CN.md)
- [已知问题](docs/KNOWN-ISSUES.md)

## 许可证

框架独立原创代码采用 **GPL-2.0-only**；与 OTA 同进程组合的原创模块采用 **GPL-2.0-only OR GPL-3.0-only**。第三方文件保留原许可证、作者及来源，详见 [LICENSE](LICENSE)、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 [LICENSE-MAP.json](LICENSE-MAP.json)。

MiniLoader、签名密钥、凭据、设备备份和上游 K3s 二进制不进入源码仓库。
