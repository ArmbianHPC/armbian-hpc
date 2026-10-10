# ARMHPC 命令

使用 `<命令> --help` 查询参数。普通系统、Rescue 和 K3s 分别使用以下入口。

## armhpc

| 子命令 | 用途 |
| --- | --- |
| `armhpc watchdog` | 查询看门狗状态和诊断报告。 |
| `armhpc usb-console` | 查看、启停 OTG 虚拟串口，读取当前或上次 Linux 启动日志。 |
| `armhpc ota` | 查询、预览和执行双系统签名 OTA；普通系统更新先进入 Rescue。 |
| `armhpc configuration` | 打开正常系统的应用配置向导。 |
| `armhpc firstrun` | 为离线 ext4 根文件系统创建管理员并导入 SSH 公钥。 |
| `armhpc led` | 查看 RGB 灯态、设置蓝色心跳或清除错误提示。 |
| `armhpc rescue` | 请求下一次进入 Rescue；--reboot 立即重启，--cancel 取消。 |

## armhpc-rescue

| 子命令 | 用途 |
| --- | --- |
| `armhpc-rescue watchdog` | 准备、执行和报告受控复位诊断；并发仅 1。 |
| `armhpc-rescue usb-console` | 查看、启停 OTG 虚拟串口，读取当前或上次 Linux 启动日志。 |
| `armhpc-rescue ota` | 在 Rescue 内管理系统更新、备用救援槽与失败恢复。 |
| `armhpc-rescue configuration` | 打开交互式救援菜单。 |
| `armhpc-rescue status` | 核对网络、磁盘、工厂归档及 BOOT 配套。 |
| `armhpc-rescue install` | 安装 SATA：64 GiB ext4 系统分区和剩余数据分区。 |
| `armhpc-rescue restore` | 恢复 SATA 系统分区，保留 DATA 和选定机器身份。 |
| `armhpc-rescue provision` | 从本地交互终端配置 Rescue 管理员和 SSH 公钥。 |
| `armhpc-rescue firstrun` | 为离线 ext4 根文件系统配置管理员和 SSH 公钥。 |
| `armhpc-rescue led` | 查看或管理 Rescue RGB 灯态及蓝色心跳。 |
| `armhpc-rescue boot` | 请求下一次仍进入 Rescue，或取消待执行请求。 |
| `armhpc-rescue reboot` | 正常重启；有有效 SATA 系统时自动进入正常系统。 |
| `armhpc-rescue console` | 进入本地登录式 Bash 终端。 |

## armhpc-kube

| 子命令 | 用途 |
| --- | --- |
| `armhpc-kube configuration` | 联网安装、创建或加入 K3s 集群，修复或重试配置。 |
| `armhpc-kube check` | 检查 K3s 数据挂载、绑定目录和安装状态。 |

OTA 的 `status`、`doctor`、`plan`、`tools status/update`、`apply`、`recover` 和 `logs` 使用同一签名升级引擎。`--json` 保持 protocol 1；写盘需明确确认。
