# 主机名与网络配置

配置命令以 root 执行；使用管理员账号时在命令前加 `sudo`。

以下命令在普通系统中使用，Rescue 保持自动生成的主机名和自身网络配置。
使用 `--help` 查看参数，`--json` 返回 protocol 1 数据。

## 主机名

```sh
armhpc hostname show
armhpc hostname set compute-07 --yes
armhpc hostname configure
```

`set` 修改 `/etc/hostname`、`/etc/hosts` 的首个 `127.0.1.1` 主机名条目及运行中的主机名，
保留其他 hosts 条目和别名；不需要重启。旧终端提示符可能保留原名称，新开终端即可刷新。
`configure` 是交互向导，需要 SSH 或串口终端。

主机名使用 DNS 名称格式，总长度最多 64 字符，每段最多 63 字符。
`armbian`、`armhpc`、`localhost`、`localhost.localdomain` 是保留的默认占位名称。
开机服务尊重已有有效主机名，也支持通过 `hostnamectl` 或编辑主机名文件设置的名称。
没有有效名称时，普通系统自动生成 `armhpc-<MAC 后四位>`；Rescue 继续自动生成
`armhpc-rescue-<MAC 后四位>`。

修改主机名不会自动重命名已经注册的 K3s 节点，也不会修改其 `node-name` 配置；集群节点变更单独管理。

## 网络

内置写入功能管理由 **systemd-networkd** 接管的物理网口，不接管 NetworkManager、
ifupdown、网桥、Bond、VLAN 或隧道。有多个物理网口时必须指定 `--interface`。
自定义 DNS 需要 systemd-resolved 已启动，且 `/etc/resolv.conf` 已连接到它。

查看地址、路由、网口状态和最近一次配置事务：

```sh
armhpc network show --interface eth0 --json
```

先预览 DHCP 或静态 IPv4，不修改文件：

```sh
armhpc network set --interface eth0 --dhcp --plan --json
armhpc network set --interface eth0 --address 192.168.100.20/24 --gateway 192.168.100.1 --dns 192.168.100.1 --dns 1.1.1.1 --plan --json
```

立即应用对应配置：

```sh
armhpc network set --interface eth0 --dhcp --yes --json
armhpc network set --interface eth0 --address 192.168.100.20/24 --gateway 192.168.100.1 --dns 192.168.100.1 --dns 1.1.1.1 --yes --json
```

命令返回事务 ID 和 `confirm_command`。独立 systemd 服务在 2 秒后应用配置，SSH 可能断开。
通过新地址重新连接，或使用 USB 串口，在默认 **90 秒** 确认窗口内执行返回的命令：

```sh
armhpc network confirm --transaction <返回的事务ID> --yes --json
```

重新进行交互式 SSH 登录时，如果有待确认网络事务，会在应用配置菜单之前自动提示。
提示显示请求的配置和剩余时间：输入 `y` 确认保存，`r` 恢复旧配置，直接 Enter 进入终端稍后处理。
无输入等待到原期限便结束；无效输入不会重置倒计时。登录成功本身不代表确认。
没有待确认事务时不提示；非交互 SSH 命令、SFTP 和 Rescue 登录不提示。
已有的 `ARMHPC_CONFIGURATION_DISABLE=1` 设置可关闭自动登录提示，仍可手动确认。
管理员账号使用现有 sudo 权限完成确认。

确认需要物理链路和有效 IPv4；静态地址还须与请求的地址和前缀一致。
这项检查不代表互联网或 DNS 可达性已验证。倒计时使用本次启动的单调时间，UTC 校时不会改变期限。
可用 `--rollback-after 180` 修改确认窗口，允许 30–600 秒。

到期未确认，服务恢复原管理文件；新建的覆盖文件会删除，使原来的网络配置重新生效。
关闭 SSH、GUI 或终端不会中断该服务。确认前重启，会在网络启动前恢复旧配置；确认后重启保留新配置。

可手动回滚最近一次已确认或待确认配置：

```sh
armhpc network rollback --transaction <返回的事务ID> --yes --json
```

运行 `armhpc network configure` 可引导选择网口、DHCP/静态 IPv4、网关和 DNS。
网关可留空，填写时必须是所选子网中的有效地址。DNS 可重复填写 `--dns` 或使用逗号分隔。
管理配置内 IPv6 继续自动获取；高级路由或静态 IPv6 使用网络管理器直接配置。

文件写入 `/etc/systemd/network/00-armhpc-<网口>.network`，覆盖该网口原有的简单网络配置，
保留其他配置文件。存在更优先的匹配文件，或同名文件由其他程序创建时拒绝应用。
事务备份仅 root 可读，不包含在状态输出中。事务期间文件被其他方式修改时，会报告回滚错误，不覆盖人工修改。

配置修改与 OTA 共用写入锁；活动 OTA、工具维护或复位诊断期间拒绝配置修改。
发起 OTA 或工具维护之前，必须先确认或回滚网络配置。

## 保存范围

| 操作 | 普通系统主机名及 hosts 条目 | 普通系统网络配置 |
| --- | --- | --- |
| 普通系统重启 | 保留 | 保留已确认的配置 |
| OTA `apply` 更新 system/all | 从旧系统保留 | 按网络文件清单保留 |
| OTA `recover` 恢复系统 | 保留；部署中断时复用写盘前已核验的备份 | 从同一备份保留 |
| Rescue 菜单恢复现有 SATA 系统分区 | 随指定机器身份保留 | 恢复工厂网络配置 |
| Rescue 菜单整盘安装、重新分区 | 重新生成默认名称 | 工厂配置 |
| 烧录完整新镜像 | 取决于覆盖哪些磁盘，不保证被覆盖文件系统的配置保留 | 相同边界 |

Rescue 主机名不进入主机名保留清单，OTA 不整体覆盖旧 `/etc`。
恢复旧基线时保留已经安装的更新版签名维护运行时，使主机名开机修复继续生效。

## 上位机与补全

上位机命令栏支持上述非交互命令，并提供主机名、网络配置状态预设；它们只适用于普通系统，
不会自动转换为 Rescue 命令。交互向导使用 SSH 或串口终端。

Bash Tab 补全覆盖两组命令、公开操作和参数。已有终端可加载：

```sh
source /usr/lib/armhpc/completion.bash
```

设备运行时和上位机需安装包含这些命令的版本。仅修改源码不会让旧固件自动获得命令。
