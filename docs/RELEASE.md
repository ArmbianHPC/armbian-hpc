# 源码与镜像发行

源码发布使用 `image-framework.py check-source --ref REF` 和 `prepare-source-release --ref REF --output OUT`。源码 commit 必须包含完整实现，归档排除缓存、镜像、引导二进制、私钥、节点身份和生成目录。

完整镜像发布继续使用 `check-release` 与 `prepare-release`，检查实际文件系统、构建输入、对应源码、第三方许可及硬件记录。源码发行不生成或代替镜像记录。

GitHub v1.0.0 提供本框架源码。源码附件记录 commit、文件哈希和许可映射；依赖固定在配置、来源及构建清单中。镜像及 .xz 压缩形式在另行生成后作为附件提供，不进入 Git。
