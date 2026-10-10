# 按目标准备源码

通用入口读取目标选择的 backend、SoC、board、OS、storage 和 workload 的 `inputs`。ARM32 和 ARM64 使用同一入口，内核版本和来源由各自 SoC 配置决定。

```sh
python scripts/image-framework.py plan wxy-oect-k3s
python scripts/image-framework.py fetch-sources wxy-oect-k3s
python scripts/image-framework.py fetch-sources wxy-oect-k3s --verify
python scripts/image-framework.py validate wxy-oect-k3s --verify-sources
```

`plan` 和普通 `validate` 可以在没有下载缓存的新 checkout 中运行，`missing_inputs` 列出缺少的文件。它们不下载文件。`--verify-sources`、`stage` 要求全部输入实际存在且 SHA256 匹配。

`fetch-sources --verify` 只读校验，不补下载、不写入 receipt。普通获取命令写入 `build/source-receipts/<target>.json`。

## 获取描述

每个输入声明固定 SHA256 和 `acquire`：

| 类型 | 适用内容 | 必要字段 |
| --- | --- | --- |
| `repository` | 随主仓库保存的 DTS、补丁等 | `type`；文件必须已存在 |
| `https` | 固定版本的源码归档 | `type`、`url`；无凭据 HTTPS |
| `git-archive` | 固定 commit 的构建框架快照 | `type`、`url`、完整 `commit`、工作区内 `git_dir`、归档 `prefix` |

例如 SoC 的内核输入：

```json
{
  "id": "kernel-source",
  "path": "cache/sources/linux/linux-6.12.111.tar.xz",
  "sha256": "9e59dc67624188fa12a6601f9598499cd6662a9066be572b59f935e3d7849810",
  "acquire": {
    "type": "https",
    "url": "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.111.tar.xz"
  }
}
```

Git 快照使用固定对象执行 `git archive`，只在归档命令固定 `core.autocrlf=false`、`core.eol=lf`，不会切换或重置已有上游 checkout。缺少对象时获取指定 commit；缺少仓库时在声明的 `sources/` 目录创建 bare Git 缓存。

下载和归档先写入同目录临时文件，SHA256 匹配后才转成正式输入。已有正式输入校验失败时保留并报错，不自动覆盖。

新缓存按组件整理：

- `cache/sources/armbian/`：固定构建框架源码。
- `cache/sources/linux/`：固定内核源码。
- `cache/firmware/`：经过板卡逐项许可审核的外设固件；当前 OECT 清单为空。
- `cache/boot-chain/`：逐项审核的永久引导组件；当前 OECT 清单为空，仍阻止完整镜像构建。
- `sources/`：独立上游 Git 仓库或对象缓存。

`reuse_paths` 只供迁移时复用同 SHA256 的旧源码包。OECT 历史缓存保留，新的活动路径不再绑定 `oect-6.12`。

## 历史资料工具

原 `scripts/fetch-oect-sources.py` 已迁到 `tools/research/fetch-oect-references.py`。它只用于 OECT 的历史来源研究，不能作为通用构建准备入口。

其中 `--include-legacy-references` 会取得旧引导链、固件或 K3s 参考资料。它们继续放在被忽略的旧缓存中；不会自动成为活动输入。永久组件必须另行登记与审核，不能直接复制原镜像启动区用于公开分发。

## 永久引导组件

板卡可选择用户完整引导链，或 `reviewed-components` 永久引导链。后者在 `boot.permanent_components` 登记独立清单，不能通过普通源码 `inputs`、外设固件或 overlay 接入。

每项需要 `id`、`role`、`path`、`sha256`、`size_bytes`、`offset_bytes`、`source_url`、`source_commit`、`license`、`license_file`、`license_sha256`、`reviewed`、`redistributable` 和 `corresponding_sources`。角色为 `idbloader`、`uboot`、`trust` 或 `boot-environment`。U-Boot/GPL 项必须有完整对应源码。清单为空或缺少缓存时，目标保持不可构建。

`stage` 校验已登记组件，仅输出清单，不将引导二进制复制到 rootfs 或源码。实际 IMG 组装仍待实现。MiniLoader 不能登记为永久组件。

## 外设固件策略

OS 配置固定：

```json
"firmware_policy": {"install_armbian_bundle": false, "selection": "board_allowlist"}
```

Armbian 的整包 `firmware` / `full_firmware` 构建与安装均关闭。OS/workload 软件包不能添加 `armbian-firmware`、`linux-firmware` 或 `firmware-*` 来绕过逐文件审核。

板卡 `firmware` 默认空列表。如果网卡等外设确实需要固件，登记以下字段：

```json
{
  "id": "device-firmware",
  "path": "cache/firmware/my-board/device.bin",
  "sha256": "填写64位SHA256",
  "destination": "/lib/firmware/vendor/device.bin",
  "license": "填写组件原许可证",
  "license_file": "adapters/boards/my-board/licenses/device-firmware.txt",
  "reviewed": true,
  "redistributable": true,
  "acquire": {"type": "https", "url": "填写固定版本HTTPS来源"}
}
```

`reviewed` 和 `redistributable` 由维护者在查明许可后填写，不能用字段本身代替许可审核。许可文本进入板卡源码；二进制只进入被忽略的缓存及经过审核的镜像。项目 GPLv2 不改变该组件原许可。

Bookworm 使用 merged `/usr`，stage 将外设固件放在 `usr/lib/firmware/`，运行时通过 `/lib/firmware` 访问。后端的镜像定制和 Release 检查会校验实际固件清单与 SHA256，拒绝整包固件及未登记文件。

引导链不能通过外设固件清单进入镜像。永久组件使用独立审核清单，MiniLoader 仍由用户准备。


当前 Armbian 活动包为 `armbian-build-v25.08-147b9a23f09f-lf.tar`，SHA256 为 `b66249d950c77196a9f57be6d0ce444e086a788d33993947a59cbfdea7fdd53c`。旧 CRLF 包保留为历史资料，不满足新活动输入的校验值。
