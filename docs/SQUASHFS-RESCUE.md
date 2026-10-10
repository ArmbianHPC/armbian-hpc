# Rescue A/B

OECT 默认采用 oect-squashfs-ab-v2。BOOT 为 512 MiB ext4；Rescue A/B 各 512 MiB SquashFS；剩余 OTA_SHARED 为 ext4。SATA 系统 64 GiB ext4，其余 DATA。

raw initramfs 选择目标并挂载只读下层与分槽可写层。源码组装使用明确的内核、DTB、initramfs 配套清单；根文件系统及模块与启动文件一致。

参见独立 armhpc-ota 的 SquashFS、恢复和发布文档。
