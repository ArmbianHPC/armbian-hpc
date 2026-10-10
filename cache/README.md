# 本地构建缓存

- `sources/armbian/`：固定 Armbian 构建源码快照。
- `sources/linux/`：由所选 SoC 指定的固定内核源码。
- `firmware/`：板卡逐项许可审核后的外设固件；当前 OECT 清单为空。
- `sources/oect-6.12/`：迁移时留下的历史资料。用户引导链、K3s、整包 firmware、工具和原厂启动附件均不作为当前构建输入。

活动入口为 `python scripts/image-framework.py fetch-sources TARGET`，只处理目标配置中的明确输入；`--verify` 只读校验。匹配 SHA256 的旧 Armbian/Linux 源码包可复用到新的按组件目录，旧缓存保留。

历史下载器位于 `tools/research/fetch-oect-references.py`，其下载清单和 `ALL_SHA256SUMS` 可能覆盖旧引导/K3s 参考文件，不能用于判断实际镜像内容。

主仓库只保留此说明，实际缓存全部忽略。源码获取规则、外设固件审核及 Armbian 整包固件禁用策略见 `docs/SOURCES.md`；实际 Release 内容检查见 `docs/RELEASE.md`。
