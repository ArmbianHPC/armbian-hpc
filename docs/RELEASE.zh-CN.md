# 固件发行

[English](RELEASE.md)

Release 标题只使用标签，例如 `v1.0.0`。正文采用 `docs/releases/<tag>.md` 中的简短英文更新说明，并链接 `<tag>.zh-CN.md` 简体中文版。

附件只上传刷机镜像和签名系统 OTA 包，两者均使用 XZ 压缩：

```text
armhpc_<版本>_<设备型号>_<系统代号>_<内核版本>_<文件系统>_<编译日期>.img.xz
armhpc-ota_<版本>_<设备型号>_<系统代号>_<内核版本>_<文件系统>_<编译日期>.tar.xz
```

编译日期使用实际 `YYYYMMDD`，内核版本不带软件包后缀。签名清单、载荷哈希、许可、构建来源及对应源码材料放入 OTA 包或镜像；校验文件、报告、源码归档和清单不作为额外附件上传。GitHub 的 Source code 链接由平台生成。

发布前校验两个本地文件和上传后的哈希，再删除旧附件。同名文件内容不同不得覆盖，需使用新版本。

源码检查和导出使用 `image-framework.py check-source` 与 `prepare-source-release`。固件记录包含精确源码 commit 及签名序号，并检查内核／模块／DTB／raw initramfs 配套、永久引导组件、许可及对应源码、身份清理和分区边界。二进制、私钥、凭据、MiniLoader 和节点备份保存在 Git 外。
