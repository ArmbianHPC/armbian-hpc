<!-- SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only -->
# 1.3.0 底层镜像与固件升级

[English](BASE-IMAGE-1.3.0.md)

本版重新编译 OECT、OESP 的 Linux 6.12.111，增加管理网络、DATA 上已登记程序和服务恢复、普通系统 SSH 认证事务、防火墙、资源及加密 swap 配置档。本次为本地交付，软件检查与实机验收分别记录。

## 内核和启动

两个板型每次构建都合并共享 Kubernetes 功能基线，缓存构建也不能跳过。打包核验最终配置、模块依赖和 kernel/DTB/initramfs 配套；构建记录及对应源码随包提供。保留板级驱动和内存定义，实际容量以 MemTotal 为准。

普通系统按“核验 DATA → 恢复已登记资源 → 创建运行时目录和锁文件 → 按配置建立 swap → 启动 K3s/已登记服务”的顺序执行。DATA 必须是真实可写 ext4，UUID 和 SATA 序列号/WWN 相符。目录存在不能替代挂载。缺失、只读或身份变化时阻止依赖服务写入，SSH 入口保留。正常开机不会格式化或分区；Rescue 不依赖 SATA。

## 网络与防火墙

普通系统另行保留 DATA 身份记录并随 OTA 保留，换入没有清单的空盘也不能静默重置恢复状态；更换磁盘仍使用显式安装/恢复流程。

```sh
armhpc network configure
armhpc network set --interface eth0 --dhcp --management --family ipv4 --metric 100 --plan --json
armhpc network set --interface eth0 --address 192.168.100.20/24 --gateway 192.168.100.1 --management --management-target 192.168.100.10 --metric 100 --route-table 200 --source 192.168.100.20/32 --plan --json
```

预览后移除 `--plan`、加 `--yes` 应用；重新登录 SSH 确认显示的原事务，默认 90 秒未确认回滚。新设备管理接口为 eth0/IPv4；OESP eth1 路由优先级较低，未插线不阻塞管理就绪。就绪检查载波、有效地址和到管理目标/已保存 K3s API 的路由及源地址，未设置目标则检查所需地址族默认路由。单次等待最多 45 秒，不依赖固定公网探测地址。

```sh
armhpc firewall set --role worker --management-cidr 192.168.100.0/24 --cluster-cidr 192.168.100.0/24 --json
```

默认预览，显式 `--apply --yes` 后，从另一个 SSH 连接在 90 秒内确认。角色可选 worker/server/server-etcd；需要 NodePort 时显式启用。只管理 nftables 的 `table inet armhpc`，保留外部规则；外部拒绝策略仍可能阻止连接。应用前核对 CIDR、SSH 端口和角色。

## 状态和 K3s 恢复

防火墙预览包含已配置的 Pod CIDR，供 API、DNS 和 kubelet 指标访问；SSH 和隧道入口仍使用独立的可信管理/节点网段。支持 VXLAN、host-gw、wireguard-native，自定义 CNI 需明确适配其规则。

```sh
armhpc state status --json
armhpc state reconcile --json
armhpc state reconcile --apply --yes --json
armhpc-kube check --json
```

目录 `/data/node-config/armhpc` 保存资源清单及按 SHA256 校验的恢复副本。修复只补齐已登记的缺失文件，遇到文件冲突或缓存损坏报待修复，不覆盖整个旧 `/etc`、清空集群数据、改变节点身份或自动选新版本。

后加服务使用显式 JSON 清单，列出 unit、enabled、version、source 和根目录相对 files；例子见英文文档。

```sh
armhpc state register --manifest /root/example-service.json --json
armhpc state register --manifest /root/example-service.json --apply --yes --json
```

资源必须由 root 持有且不能被其他用户写入。路径、符号链接、容量、权限和全部文件先核验再写入清单；带 capabilities/xattrs 的程序需专用适配器。每个服务的 tmpfiles 也应登记，供沙箱建立前准备 `/run` 资源。

K3s 保留已选版本；加入集群无法可靠取得版本时要求填写完整版本。安装器、程序、同版本同架构 airgap 包和 SHA256 清单校验后保存在 DATA，安装不再依赖临时在线下载的程序。标记存在不能掩盖程序/单元缺失；一致的旧安装可迁移登记，资源缺失或冲突则进入待修复状态。

向导支持官方、自定义、Rancher 中国镜像以及显式离线目录。离线目录包含校验清单、对应架构程序、airgap 包和已固定哈希的 install.sh，TLS 校验保持启用。容器镜像源独立导入：

```sh
armhpc-kube registry import --file /root/registries.yaml --default-fallback deny --json
armhpc-kube registry import --file /root/registries.yaml --default-fallback deny --apply --yes --json
```

先排空并停止 K3s，再应用。CA、客户端证书和私钥复制到 DATA 私有目录，查询不回显认证内容。默认端点回退开关按 K3s 对已声明镜像源的行为生效，不等同于出口防火墙。持久 kubelet 配置目录要求 K3s v1.32 或以上，已有更低版本保留，启用配置档前需明确适配。

## SSH 认证

镜像初始登录为 `root/1`。普通系统实际创建/加入 K3s 且本节点 Ready 后，下次交互式 SSH 登录提示改密、配置密钥并禁用密码或稍后处理；非交互命令及 SFTP 不进入向导。Rescue 的登录设置独立。

```sh
armhpc auth status --json
armhpc auth password
armhpc auth keys --json
armhpc auth key-add --public-key-file /root/administrator.pub --yes --json
armhpc auth key-only --public-key-file /root/administrator.pub --yes --json
```

保留原连接。密钥策略需要第一次新的指定密钥 SSH 会话确认，再从第二次新密钥会话确认已变更策略；按提示执行原事务 confirm。原会话和密码会话不能代替。90 秒超时或重启回滚，应用前检查 sshd 实际生效配置和外部冲突。本机控制台 PAM/密码入口保留；卸载 K3s 不恢复初始密码。

## swap、资源和健康

```sh
armhpc swap status --json
armhpc swap set --profile host-noswap --size-gib 16 --json
armhpc swap set --profile limited --size-gib 16 --json
armhpc resources set --role worker --memory-gib 4 --json
armhpc health status --json
```

配置档默认预览，排空并停止 K3s 后显式 `--apply --yes`。swap 默认 disabled；host-noswap 允许宿主 swap、Pod NoSwap，limited 使用 Pod LimitedSwap。文件必须位于核验过的 DATA、由 root 持有、0600 且完全分配；未知现有文件需显式 import。dm-crypt 使用每次启动随机密钥，不保存密钥、不支持休眠。身份、inode 或文件分配不符时拒绝启用。默认阻止依赖服务启动，仅显式 `--allow-degraded-noswap` 时允许降级。

`MemorySwapMax=0` 仅设置 K3s 服务本身，Pod 的实际 cgroup 祖先和 kubelet 生效行为仍需实机负载核验。资源预算依据实际内存和角色生成，保留完整驱逐信号；未显式配置则继承 kubelet 默认值，外部冲突先报告。

健康采集每 60 秒运行，服务单次最多 15 秒；原子输出 JSON 和 textfile 指标。DATA 可用时保留有容量限制的启动样本和 Linux 日志，不可用时写运行时目录。显示采集年龄和失败结果。无法确认的复位原因记录 unknown，不伪造引导日志或未经验证的 ramoops 内存配置。

## 固件 OTA 与回滚

使用配套 1.3.0 上位机和维护包，导入对应板型 TAR.XZ 并选 **all**。嵌套 BOOT 包签名用途为 firmware-update、最低 agent 1.3.0。流程先维护三环境工具，核验板型/布局/签名/哈希/原 BOOT 配套，再进入已确认 Rescue，导出并核验本机私有 BOOT 恢复备份，部署新 BOOT 和备用 Rescue，确认启动后更新配套 SATA 系统，核验恢复资源和服务再提交。

保留电脑上的私有恢复备份。断流或关闭上位机不重新创建任务，不将未知结果算作成功；继续查询原任务 ID。原 BOOT 不属于受支持的配套时，切换前拒绝，需匹配迁移包或镜像。

正常固件启动确认不更新看门狗/kexec 验收。早期内核完全不能执行恢复流程时仍可能需要导出的 BOOT 恢复入口及现场处理。回滚仅处理相关启动资源和选择，保留身份、最新时间和 DATA，不自动降级 K3s/etcd 数据格式。

## 单台验收

上传随包 verify-node-1.3.py：

```sh
python3 /root/verify-node-1.3.py
python3 /root/verify-node-1.3.py --baseline
# 完成约定的升级/切换后，在普通系统运行：
python3 /root/verify-node-1.3.py --compare
```

baseline 显式在已核验 DATA 写入 1 MiB 标记，保留私有身份和配置基线；compare 只打印比较结果。报告仅保存于真实可写 OTA_SHARED。脚本不签发硬件通过。

原清单 3–10、12–13 项与实现/证据的对应表见英文文档。实机仍需分别完成双网口/缺失 DHCPv6、DATA 延迟/缺失/只读、认证超时、OTA 中断、CPU 配额/seccomp、跨节点 Service/DNS/NAT/NetworkPolicy、logs/exec、镜像冷启动及实际 swap 检查；每板还需 3 次普通重启、3 次冷启动、一次受控断电和 24 小时负载。OECT 看门狗恢复、node8 异常复位原因及 node4 物理容量继续按实际证据记录。
