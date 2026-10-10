# Windows 与 WSL 构建

在 WSL 的 Linux 文件系统内放置工作区，源码通过固定 commit 检出；输出导出到 Windows 目录。准备 Python 3.11+、Git、交叉 GCC、e2fsprogs、SquashFS 工具、zstd、rsync、gdisk 和 U-Boot tools。

辅助程序使用 OTA 仓库 DEPENDENCIES.lock.json 中固定的交叉编译器，并检查 ELF 架构及 GLIBC 不超过 2.36。Windows 软件使用固定 .NET SDK、NuGet 锁文件和带 SHA256 的 Python 运行时。

按 [BUILD.md](BUILD.md) 指定输入根目录、内核配套清单、输出目录、版本、序号和时间戳。构建缓存、签名密钥和设备配置置于两个源码仓库之外。
