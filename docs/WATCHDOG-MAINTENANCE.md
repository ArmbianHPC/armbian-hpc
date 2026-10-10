# 看门狗管理

看门狗与受控 BOOT 操作调用独立 OTA 引擎。使用 armhpc[-rescue] watchdog status/report 查询，Rescue 内提供准备与执行操作。

Guardian 是硬件看门狗唯一持有者；操作前核对当前根、网络、任务、SATA 挂载和供电条件。重连后按原诊断 ID 查询，人工断电恢复如实关闭记录。

BOOT 维护有备份、恢复镜像导出、版本化入口和回滚；保持设备身份、共享时间及 DATA。现象与恢复方法见 KNOWN-ISSUES.md。
