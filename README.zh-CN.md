# ARMHPC

[English](README.md)

ARMHPC **1.0.0** 是面向 ARM64／ARM32 系统的模块化 Linux 固件与设备管理框架，提供可配置的普通系统、Rescue 环境、通用系统组件及板卡构建方案。

## 架构

每个目标组合 SoC、板卡、构建后端、操作系统、存储布局及工作负载。设备管理和恢复等通用组件跨目标复用，板卡配置定义引导链、驱动、接口和分区要求。

WXY OEC Turbo（OECT）是首个接入设备，当前固件使用 SquashFS Rescue A/B、共享可写区及独立的 ext4 普通系统和 DATA 分区。这些存储选择属于该目标的配置；新增板卡通过同一框架提供各自的接入方案及兼容固件包。

## 功能

- 面向 ARM64／ARM32 的固件组合配置，明确选择源码和组件。
- 普通系统与独立 Rescue 环境，安装、恢复及共享设备身份。
- 签名 OTA 接入、Rescue A/B、系统离线部署和持久恢复记录。
- 网络与管理员初始化、SSH、时间恢复，以及按板卡配置的状态灯和网口灯。
- 在支持 USB 设备模式的目标上提供 OTG 虚拟串口。
- 可选应用组件，包括 K3s 安装及集群配置。

| 命令 | 用途 |
| --- | --- |
| `armhpc` | 管理普通系统。 |
| `armhpc-rescue` | 管理 Rescue、安装和恢复。 |
| `armhpc-kube` | 管理可选的 K3s 应用。 |

共享 OTA 引擎和上位机软件位于 [armhpc-ota](https://github.com/ArmbianHPC/armhpc-ota)。

## 配置构建

先列出已有目标，再选择设备对应的方案：

```sh
python3 scripts/image-framework.py list
python3 scripts/image-framework.py validate <target>
python3 scripts/image-framework.py plan <target>
python3 scripts/image-framework.py stage <target>
```

将 `<target>` 替换为列表中的目标 ID；`wxy-oect-k3s` 是首个提供的板卡方案。新增 SoC、板卡和目标配置参见[接入指南](docs/ADAPTERS.md)。配置模型支持两种架构，每个板卡仍需对应的启动文件、驱动及实机验证。

## 下载

[Releases](https://github.com/ArmbianHPC/armbian-hpc/releases) 标题只使用版本号，例如 `v1.0.0`。固件包通过文件名标明设备型号和软件组合：

```text
armhpc_<版本>_<设备型号>_<系统代号>_<内核版本>_<文件系统>_<编译日期>.img.xz
armhpc-ota_<版本>_<设备型号>_<系统代号>_<内核版本>_<文件系统>_<编译日期>.tar.xz
```

第一项为刷机镜像，第二项为包含普通系统和 Rescue 签名载荷的 OTA 包。编译日期使用实际日期，格式为 `YYYYMMDD`。请选择与设备目标配置匹配的包。固件包和私有构建输入保存在源码仓库外。

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
