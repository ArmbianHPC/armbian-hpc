# 框架构建

检出框架和 OTA 的兼容提交，版本号可以不同，参见[独立版本与兼容性](COMPATIBILITY.zh-CN.md)。通过 scripts/image-framework.py 的 list、validate、plan、stage 和 fetch-sources 准备目标。所有构建输出放在源码仓库之外。

OECT 使用 GPT/ext4 BOOT，OESP 使用独立校验的 Amlogic 保留区、MBR/FAT32 BOOT。两个目标均提供 SquashFS Rescue A/B、共享 ext4 和 SATA ext4 普通系统及 DATA。板级构建入口位于 tools/build。

`refresh-fixed-boot-roots.py` 接收显式源目录、原根文件系统、BOOT 清单、输出目录、辅助程序、板卡、发布 ID 与时间参数；刷新四个根文件系统的软件前后核对 kernel、DTB、原始 initramfs 与配套模块。它不重新生成 initramfs，可用于制作与已有固定 BOOT 相容的软件 OTA。改变内核或 DTB 需要配套 BOOT 维护路径。

使用 `prepare-firmware-inputs.py` 按固定来源、哈希及许可准备板卡固件。板级组装器生成签名载荷后，`prepare-firmware-package.py` 将精确提交、内核和保留 BOOT 源码、软件版本及许可放入 OTA 传输包。

`pack-firmware.py` 压缩并验证 IMG 与 OTA。OESP 应先用 package-oesp-burn.py 生成 USB Burning Tool 原生容器，再交给通用打包器。保留 BOOT 对应的内核配置和补丁也需随附。私有引导输入及凭据不进入 Git，公开镜像须有实际组件许可依据。

安装 requirements-test.txt 后执行 `PYTHONPATH=../armhpc-ota/src python3 -m pytest tests`，完整收集 unittest 与函数测试。发布通过一个清单列出全部机型的 IMG/OTA、大小、哈希和镜像再分发材料，统一核对后上传。
