# 设备兼容说明

[English](DEVICES.md)

ARMHPC 为模块化框架。每个板卡需要自己的配置、引导链、内核／DTB、存储策略和验证。以下现有镜像均为 ARM64 Debian Bookworm，使用配套 Linux 6.12.111；其他型号或 PCB 版本需要单独接入。

| 设备 | 配置与硬件 | 镜像与存储 | 接口与限制 |
| --- | --- | --- | --- |
| WXY OEC Turbo，原版 eMMC | `wxy-oect-original`；RK3566、4 GiB RAM、8 GB eMMC | Rockchip GPT 整盘镜像；512 MiB BOOT、各 512 MiB SquashFS Rescue A/B、共享 ext4；SATA 64 GiB ext4 普通系统及其余 DATA | 网口、受支持的 USB2 OTG 串口及板卡灯控；自然看门狗超时恢复有已知故障。 |
| OES1PLUS `oes1plus-v2.2` | `wxy-oesp-v22`；S922X、4 GiB RAM、8 GB eMMC | XZ 解压后导入 USB Burning Tool 2.2.4；MBR/EPT、FAT32 BOOT、SquashFS Rescue A/B、共享 ext4；明确选择的 SATA 盘划分 64 GiB ext4 系统及 DATA | NET0 RTL8211F；NET1 RTL8153B 当前以 USB 2.0 480 Mbit/s 枚举；包含 NET0 定位、状态灯、GPIO 风扇与性能策略；USB 虚拟串口默认关闭。 |

## 选择安装包

- 从 [Releases](https://github.com/ArmbianHPC/armbian-hpc/releases/latest) 下载机型匹配的镜像或系统 OTA。
- 全新镜像使用 `root` / `1`，登录后执行 `passwd`。设备身份和 SSH 主机密钥在设备端生成。
- Windows／Linux 上位机及签名工具维护包从 [armhpc-ota](https://github.com/ArmbianHPC/armhpc-ota/releases/latest) 获取。工具维护包不替换普通系统或 Rescue 基础镜像。
- 已有设备 OTA 按保留策略保留登录、身份、网络和普通系统主机名等配置。
- 明确选择 SATA 目标并核对持久身份，写盘前按板卡说明准备安装与恢复入口。

## 运行记录与验收

OESP 1.1.4 用户记录覆盖 Rescue 启动、双网口、SATA 系统恢复安装、A/B OTA 切换，以及设备身份、主机名、网络、SSH 密钥和 DATA 标记保留。这些记录不等同于 1.2.0 新一轮实机运行。1.2.0 核对了源码配套、软件行为、签名、载荷哈希和镜像打包，该次发行没有操作实际节点。

硬件看门狗复位、kexec 交接及早期目标内核失败恢复，分别保留已有记录。`hardware_verified` 不由编译、自动化检查或普通重启推导。另见[已知问题](KNOWN-ISSUES.zh-CN.md)、[OESP 接入](OESP.zh-CN.md)、[设备命令](COMMANDS.md)及[来源](SOURCES.md)。
