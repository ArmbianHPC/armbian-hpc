# 新增芯片和板卡适配

## 配置组合

一个 `target` 选择 `backend`、`board`、`os`、`storage` 和 `workload`。

板卡通过 `soc` 引用芯片配置。

架构必须同时受后端、芯片、板卡、操作系统和应用支持。

目标可以覆写启动介质、存储标签、根分区大小、挂载点和外置接口。

芯片来源、启动链和应用安装方式通过各自配置选择，避免目标覆写破坏适配关系。

## 运行命令命名

正常系统使用 `armhpc` 管理，K3s 使用 `armhpc-kube`，Rescue 使用 `armhpc-rescue`。板卡不能新增专有命令别名，内部实现存放于 `/usr/lib/armhpc`。

`installation-policy.json` 输出三个入口路径、内部管理员模块路径和管理员子命令。正常镜像只暴露 `armhpc` 与所选 K3s 应用的 `armhpc-kube`；Rescue 只暴露 `armhpc-rescue`。后台服务直接调用内部模块。详见 [运行命令](COMMANDS.md)。

## 1. 确定架构与硬件

先确定芯片型号、板卡型号、硬件版本、内存、启动介质和实际外置接口。

相同芯片不能保证 DTS、DDR 初始化、网卡、电源和启动布局相同。

ARM64 使用 `arm64`；ARM32 首阶段使用 ARMv7 hard-float ABI `armhf`。

不能把 OECT 目标的 `architecture` 改成 `armhf` 来获得 ARM32 适配。

K3s 运行程序会同时检查内核机器类型和 `dpkg --print-architecture`。

当前 ARM32 机器类型允许 `armv7l`、`armv8l`，不默认接受 ARM64 内核搭配 ARM32 用户空间。

若需要混合 ABI，先补齐对应验证及安装架构逻辑。

## 2. 创建芯片配置

从 `templates/<architecture>/soc.json` 建立 `adapters/soc/<soc-id>.json`。

文件名必须与 `id` 一致，标识使用小写字母、数字和连字符。

填写以下内容：

- `architectures`：准备支持的实际架构。
- `backend_families`：具体构建后端的 family 映射。
- `kernel`：内核 family、branch、版本、源码、固定 ref 和完整 commit。
- `kernel_requirements`：芯片所需驱动及允许值。
- `inputs`：源码路径、SHA256 和 `acquire` 获取描述，见 [SOURCES.md](SOURCES.md)。
- `blockers`：尚未完成的构建工作。
- `validation_todo`：镜像生成后需要执行的实机验收。

例如：

```json
"kernel_requirements": {
  "CONFIG_ARCH_ROCKCHIP": ["y"],
  "CONFIG_EXT4_FS": ["y", "m"]
}
```

不同层对同一选项的要求取交集，交集为空时配置失败。

模块形式需要由板卡适配把模块及依赖纳入 initramfs。

初始状态使用 `draft` 或 `source_prepared`。

ARM32 的内核版本按芯片驱动支持选择，不要求与 RK3566 相同。

## 3. 创建板卡配置

从模板建立 `adapters/boards/<board-id>.json`。

板卡文件推荐放在 `adapters/boards/<board-id>/` 子目录。

至少准备 DTS、启动方式、真实接口和构建缺项。

DTS 必须登记到 `inputs`，并记录原始来源及 SHA256。

`stage_as` 将 DTS 复制到构建输入中的 `board-inputs/`。

```json
"inputs": [
  {
    "id": "board-dts",
    "path": "adapters/boards/my-board/dts/my-board.dts",
    "sha256": "填写文件的64位SHA256",
    "stage_as": "dts/my-board.dts"
  }
]
```

复制到 `board-inputs/` 后，还需要补丁或钩子把 DTS 接入内核源码及 DTB Makefile。

ARM32 DTS 的源码路径按具体内核树确定，可能包含厂商子目录。

`boot.dtb` 使用最终 DTB 安装路径，供板卡启动配置使用。

`boot.chain` 支持模板中的用户完整引导链，以及 OECT 的 `reviewed-components` 永久引导链。后者可把通过审核的永久组件放入公开 IMG，临时 MiniLoader 由用户准备。

永久组件只通过 `boot.permanent_components` 接入；普通 `inputs`、外设固件和 overlay 仍拒绝引导载荷。每项固定来源、原许可文本、SHA256、大小和偏移；U-Boot/GPL 组件登记完整对应源码。默认 Armbian 自动引导构建仍关闭，独立组装流程只使用审核清单。

## 4. 接入 Armbian 后端

当前后端固定为 `armbian-v25-08`。

在板卡配置填写 `armbian_board`，并登记真实构建文件：

```json
"armbian_board": "my-board",
"backend_files": {
  "armbian-v25-08": [
    {
      "source": "adapters/boards/my-board/armbian/my-board.csc",
      "destination": "config/boards/my-board.csc"
    },
    {
      "source": "adapters/boards/my-board/patches/0001-add-board-dts.patch",
      "destination": "kernel/my-family-6.12/0001-add-board-dts.patch"
    }
  ]
}
```

示例的 board 名称、补丁目录和内核版本需要替换。

后端文件写入独立输出的 `userpatches/`，不写入现有 `sources/armbian-build/`。

板卡定义负责正确的 `BOARDFAMILY`、内核架构、`BOOT_FDT_FILE` 和启动场景。框架在 family 配置后将 `BOOTCONFIG` 设为 `none` 并清除 branch 覆写，最终配置钩子将其与 `ATF_COMPILE=no` 固定为只读；rkbin 扩展由无下载/安装的本地扩展覆盖。

需要新 family 时，登记 `config/sources/families/<family>.conf` 和对应补丁。

板卡启动适配负责 U-Boot 交接方式、引导地址、分区边界、initramfs 和恢复入口。SATA 安装和日常维护保留 eMMC 引导及环境。OECT 完整 IMG 可按已审核布局从 LBA 0 刷入；`0xCCCCCCCC` 是 RKDevTool 的 LoaderToDDR 特殊值，不是 eMMC 偏移。单个 ext4 文件系统镜像不能充当完整整盘 IMG。

`userpatches/u-boot` 和 `userpatches/atf` 不再接受板卡输入。自定义 family 或钩子仍需审查，不能自行下载或写入早期引导链。

K3s 目标还需要接入 `runtime-requirements.json` 指定的 cgroup v2 启动参数。

生成的 Armbian 钩子在 family 配置后固定内核源码、commit、主次版本及 DTB 路径。

内核配置钩子设置驱动需求；镜像定制脚本检查安装后的内核配置。

这些检查不能替代 SATA、网卡、温控和启动链的实机验收。

完整输入准备后，生成的 `build.sh` 接受独立的 Armbian Git checkout：

```sh
# 在独立 Linux 构建目录中准备正确版本和 userpatches 链接。
bash /path/to/stage/build.sh /path/to/pinned-armbian-checkout
```

脚本核对 Armbian HEAD 与固定 commit，并要求其 `userpatches` 指向本次 stage。

缺项时脚本退出码为 `78`，不会进入编译。

## 5. 选择系统、存储和应用

优先复用现有 OS 和 workload 配置。

OS 配置包含本地安装时创建管理员、导入 SSH 公钥的 `access_policy`。生成的安装约定要求正常系统及存在的 Rescue 分别完成配置，见 [INSTALL-ACCESS.md](INSTALL-ACCESS.md)。

首个存储配置是 `emmc-external-ext4-64g`：eMMC boot/rescue，外置盘 root/data。

`single-media-ext4` 为单盘 boot/root/data，默认使用 SD，可覆写 `boot_media` 为 eMMC。

单盘配置默认 root 为 16 GiB，可覆写大小；恢复从外部救援系统执行。

单盘分区起点需要根据板卡原始引导区计算，stage 不假定 1 MiB 即可避开引导区。

两种现有配置均要求全 ext4。其他分区策略需要独立存储配置及对应安装/恢复实现。

外置接口必须出现在板卡 `storage_interfaces` 中，且相应驱动进入内核/initramfs。

`k3s-online` 支持 ARM64/ARM32 的配置接口、在线安装和四种节点角色。

业务容器镜像还需要对应 CPU 架构；K3s 程序支持该架构不等于业务镜像可运行。

`base-server` 不加入 K3s 代码，也不应用 K3s 的关闭 swap 策略。

## 6. 创建目标并检查

从模板建立 `targets/<target-id>.json`，选择已填写的配置。

```sh
python scripts/image-framework.py fetch-sources my-target
python scripts/image-framework.py validate my-target --verify-sources
python scripts/image-framework.py plan my-target
python scripts/image-framework.py stage my-target
python scripts/image-framework.py check-kernel my-target /path/to/actual-kernel.config
```

所有输入路径必须位于工作区，禁止路径越界。

Overlay 出现内容冲突时失败；`etc/image-framework/` 保留给框架生成配置。

需要符号链接或特殊文件权限时，使用板卡构建钩子，避免 Windows overlay 复制改变含义。

更改源码文件后同步更新输入 SHA256，并保留来源记录。

## 状态与验收

| 状态 | 必须具有的证据 |
| --- | --- |
| `draft` | 配置草案 |
| `source_prepared` | DTS、启动资料和固定源码来源已准备 |
| `configured` | 后端构建文件、安装和恢复输入已接入，构建缺项已完成 |
| `built` | 有实际构建产物、文件 SHA256 和构建日志 |
| `hardware_verified` | 有对应硬件版本的启动、存储、网络、恢复等验收记录 |

状态由适配维护者根据证据更新。框架不会因生成 stage 自动标记为 `built` 或 `hardware_verified`。

`blockers` 记录缺少的构建工作；`validation_todo` 记录构建后的验收工作。

构建成功后继续完成验收，再发布可刷写产物。

编译得到的 `.img` 及压缩包保存在本地 `build/`，预编译镜像只作为 Release 附件发布。板卡适配源码、DTS、补丁、固定上游来源和构建配置保留在对应源码版本中，供用户自行编译。

Release 镜像附带 SHA256，并标明板卡、架构、内核版本和源码 tag/commit。源码仓库及源码归档均不包含编译镜像。

固件整包默认关闭；需要的外设固件在板卡 `firmware` 清单中逐项登记来源、SHA256、许可文本及审核结果。

发布入口和对应源码/实机记录要求见 [RELEASE.md](RELEASE.md)。

## 当前未完成

OECT 尚缺完整板卡定义、固定内核与 DTB 集成、eMMC 启动布局、安装器及独立 Rescue。

尚未选择实际 ARM32 芯片和板卡，模板和测试夹具均不代表硬件适配完成。

## 上游接口参考

- [Armbian userpatches 与自定义构建配置](https://docs.armbian.com/build-framework/user-configurations/)
- [Armbian 新增板卡](https://docs.armbian.com/build-framework/adding-a-board/)
- [K3s 支持架构与运行要求](https://docs.k3s.io/installation/requirements)

上游文档会更新。生成钩子使用本地固定 v25.08 源码已检查的接口。
