# USB 虚拟串口

OECT USB2 刷机／OTG 口提供 CDC ACM 登录，Linux 设备为 ttyGS0；USB3 主机口不提供该功能。Windows 使用枚举出的 COM 登录，认证身份与管理员配置一致。

`armhpc usb-console status --json` 或 `armhpc-rescue usb-console status --json` 查询控制器、枚举状态及日志范围。串口支持 OTA 工具维护和载荷传输，断开后使用原任务查询和续传。
