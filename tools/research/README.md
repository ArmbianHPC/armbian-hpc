# 历史资料研究工具

`fetch-oect-references.py` 是原 OECT 专用资料下载工具，迁入此目录后不再作为活动构建入口。

它保留已有来源锁定、历史清单校验及显式 `--include-legacy-references` 研究选项。默认缓存位于被忽略的 `cache/sources/oect-6.12/`；其中可能包含其他许可的固件、引导或 K3s 资料。

通用构建只使用目标准备入口及声明的源码输入：

```sh
python scripts/image-framework.py fetch-sources TARGET
python scripts/image-framework.py fetch-sources TARGET --verify
```

研究缓存不能整体复制进源码仓库或 Release 镜像。
