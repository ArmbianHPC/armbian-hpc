# 命令 Tab 补全

[English](COMPLETION.md)

`armhpc`、`armhpc-rescue` 和 `armhpc-kube` 支持 Bash Tab 补全，可用于设备上的 SSH 和 USB 串口终端。

## 示例

下面的 `<Tab>` 表示按下 Tab 键：

```text
armhpc id<Tab>                               → armhpc identify
armhpc identify --inter<Tab>                  → --interface
armhpc identify --interface e<Tab>            → 匹配的物理网口
armhpc ota app<Tab>                           → armhpc ota apply
armhpc ota apply --target s<Tab>              → system
armhpc-rescue ota tools up<Tab>               → update
armhpc-rescue ota tools update --bundle <Tab> → 本地文件和目录
armhpc led --heartbeat o<Tab><Tab>            → on / off
```

有多个匹配项时，连续按两次 Tab 查看候选。支持常用参数、OTA 目标值、BOOT 与工具维护操作、物理网口名和文件路径；默认共享状态目录中已有的版本、任务和看门狗记录 ID 也会作为候选。支持 `--target=system` 等写法，内部服务命令和 worker 参数不参与补全。

## 安装与生效

固件的公共 overlay 安装补全库、Bash 登录加载脚本及 `bash-completion` 自动加载文件。签名工具维护会把相同的五个资源安装到普通系统和 Rescue A/B 可写层，并备份原文件；回滚恢复原内容，恢复旧系统基线时随已维护的运行时保留。

安装后，新开的 Bash 登录会话自动加载，即使没有安装可选的 `bash-completion` 包也能使用；安装该包时，交互式非登录 shell 也可按命令名加载。现有终端可执行：

```sh
source /usr/lib/armhpc/completion.bash
```

补全过程只读取本地文件名和网口名；按 Tab 不会执行 ARMHPC 命令，也不会访问更新服务器。这套脚本使用 Bash 补全接口，其他 shell 需要各自接入。
