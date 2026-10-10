# 管理员与 SSH 访问

使用安装时配置的管理员凭据。SSH 私钥、公钥、密码、主机密钥和设备身份均不进入通用源码或更新包。Windows 软件保存凭据时使用本机保护存储。

在独立挂载的离线 ext4 根目录中，可运行 `armhpc-rescue firstrun --rootfs /mnt/sata-root --username hpc --public-key-file /run/installer/administrator.pub`。该入口创建管理员、配置 sudo 和 SSH，拒绝运行根目录、私钥、多行公钥及不匹配的架构。

SATA 安装使用 `armhpc-rescue configuration` 或 `armhpc-rescue install --help`；整盘安装创建 64 GiB 系统分区和剩余 DATA。恢复使用 `armhpc-rescue restore` 或签名 OTA 的 `recover`，保留 DATA。写盘前展示磁盘摘要并要求 y/N，随后再次核对磁盘身份。

正常系统及 Rescue 可使用各自的 SSH 主机密钥。Windows 管理器按持久设备 ID 和环境保存已认可指纹；切换环境后按该环境的身份重连。USB2 OTG 串口使用同一个管理员登录。

OTA 保留管理员、SSH 身份、网络配置和指定用户数据。维护工具更新覆盖普通系统及 Rescue A/B 的运行时代码，不修改用户凭据。
