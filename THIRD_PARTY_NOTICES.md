# 许可与第三方来源

框架独立原创代码采用 GPL-2.0-only，全文见 LICENSE。与 OTA 同进程使用的原创运行时、接入、组装和校验模块采用 `GPL-2.0-only OR GPL-3.0-only`；逐文件标识与 LICENSES/ 中的全文共同说明许可范围。

第三方来源和作者声明不改写：

- OECT DTS 来源于 unifreq/linux-6.12.y 提交 4c0b3046f608fad852b55de1b234d43b8e178034；GMAC/PHY 与 eMMC 适配移植自 Linux 提交 53ec6d21fe1ec0677f9db56a23dda8d145eda102。原许可为 `(GPL-2.0+ OR MIT)`，保留原作者与声明。
- Armbian 补丁来自固定 v25.08 提交 147b9a23f09fae4d5ef8e73cf07ee69652ff4976，保留补丁原作者、来源与许可。
- Linux、U-Boot、Debian、外设固件与引导组件按实际版本原许可处理；本仓库的许可证不将它们改为单一许可证。

`sources/` 和 `cache/` 的本地上游仓库及二进制不进入源码归档。可分发引导输入必须有来源、完整许可、哈希及需要的对应源码；MiniLoader 由用户提供。

上游 K3s 使用 Apache-2.0，启动后的外部下载与安装保留其许可。仓库只包含原创的配置、下载验证及调用代码，不包含上游脚本、二进制或 airgap 镜像。

Source Release 不分发内核、引导链或系统镜像。完整镜像发行另行登记全部安装包、原许可通知及对应源码。源码附件以固定 commit 导出，逐文件许可与来源清单随包提供。
