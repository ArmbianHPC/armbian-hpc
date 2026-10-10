# 上游源码

`armbian-build/` 保存原 Armbian Git 仓库及其历史。

当前 checkout 是 main，HEAD 为 `b18fec307ab159e978735704b1079742e7e30f3f`。

首个构建后端固定 v25.08：`147b9a23f09fae4d5ef8e73cf07ee69652ff4976`。

该版本完整快照位于 `cache/sources/armbian/`。

后续编译使用独立、固定版本的构建 checkout。

通用入口：`python scripts/image-framework.py fetch-sources TARGET`。Git 来源和 commit 由 backend 的 `acquire` 描述固定；已有 checkout 不重置、不切换。
