# 网口绿灯定位

[English](NETWORK-IDENTIFY.md)

以 root 执行命令，使网口的绿色 PHY LED 有规律地闪烁。命令启动限时后台服务，确认开始闪烁后返回；到时或手动停止后恢复原来的硬件链路／活动灯规则。

## 命令

普通系统：

```sh
armhpc identify --seconds 30 --json
armhpc identify --status --json
armhpc identify --stop --json
```

Rescue 使用 `armhpc-rescue identify`，参数相同。上位机命令栏包含开始、停止、查询预设，会根据设备环境转换命令入口；多选设备可使用原有的 1–8 并发设置。

| 参数 | 含义 |
| --- | --- |
| `--interface eth0` | 指定物理网口；默认使用板卡配置中的网口，或唯一的物理网口。 |
| `--seconds 30` | 持续 1–600 秒，默认 30 秒。 |
| `--period-ms 1000` | 完整亮灭周期为 200–5000 毫秒；默认 1000 毫秒，亮、灭各占一半。 |
| `--status` | 查询最近一次定位任务。 |
| `--stop` | 停止并恢复原来的 LED 规则。 |
| `--json` | 输出 protocol 1 JSON，供上位机和脚本读取。 |

网口需要有链路，并有唯一的绿色 LED 与该 PHY 或网卡关联；LED 已使用 `netdev` 硬件卸载触发器。定位仅改变该灯的链路规则，链路消失或 PHY 拒绝设置时结束并报告错误。

## 结果与恢复

`running` 表示已开始闪烁。`completed` 或 `stopped` 且 `restored: true` 表示原来的规则已恢复。`failed` 返回错误和恢复结果。进程异常中断后可执行 `--stop` 恢复；再次开始也会先恢复上一份规则，再记录新快照。`restore-failed` 需要检查错误并重试 `--stop`。

SSH 或串口断开后，后台任务继续运行。同一网口正在闪烁时拒绝重复启动。重启会结束定位，由正常 PHY 初始化设置开机灯态。

## 板卡配置

发现过程检查绿色 LED 与物理设备的关联，兼容子节点 `of_node` 和父节点 `device/of_node` 两种映射；板载状态灯不会被选中。`/etc/armhpc/phy-led.json` 中的 `identify_index` 可选定配置网口的一个输出；OECT 的绿色链路灯使用索引 1。
