# 框架构建

1. 检出 armbian-hpc 与 armhpc-ota 的匹配版本。
2. 使用 `image-framework.py validate/plan/stage` 准备目标配置；`fetch-sources` 获取固定来源。
3. 通过固定 Armbian 后端构建 rootfs、内核、模块和 DTB，或显式提供经过哈希校验的构建输入。
4. `tools/build/prepare-rootfs-artifact.sh` 展开根文件系统并应用目标运行时；`prepare-ota-root.py` 使用独立 OTA 安装器。
5. OECT 使用 `prepare-oect-squashfs.py` 准备 normal/rescue/public/boot 根目录，输出新的 boot-manifest.json，`assemble-oect-squashfs.py` 装配 eMMC A/B。

组装输入明确指定 release、sequence、epoch、boot-manifest、base-image SHA256 和 base-provenance.json、签名 key-id 及目录，不依赖某次历史构建的路径或标记文件。启动清单包含 kernel_release 及 kernel/dtb/initramfs 的 SHA256；模块必须与实际内核配套。

系统 OTA 载荷由 `tools/build/prepare-oect-release.py` 准备。普通系统为 ext4 压缩镜像，Rescue 为 SquashFS；SOURCE_DATE_EPOCH 与版本记录来自构建参数。

完整 IMG、OTA 载荷与压缩包生成在源码目录外；公开发行按 RELEASE.md 另行检查许可和对应源码。

`prepare-oect-squashfs.py` 校验输入 BOOT 清单，生成新的原始 initramfs 和输出清单；随后运行 `compare-squashfs.py`，再将输出清单传给组装器。共享目录由组装器初始化，序号写入控制记录。

固件 XZ 归档与上位机包格式校验见 [Firmware packaging](FIRMWARE-PACKAGING.md)。
