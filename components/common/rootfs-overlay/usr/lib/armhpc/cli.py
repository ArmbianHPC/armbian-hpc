# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Public command groups dispatch to private modules without shell evaluation."""
import argparse
from pathlib import Path
import runpy
import sys

LIBRARY=Path(__file__).resolve().parent
RESCUE_MARKER=Path('/etc/armhpc/rescue-environment')
NORMAL={
    'watchdog':('watchdog.py','查询看门狗状态和诊断报告。'),
    'usb-console':('usb_console.py','查看、启停 OTG 虚拟串口，读取当前或上次 Linux 启动日志。'),
    'ota':('ota.py','查询、预览和执行双系统签名 OTA；普通系统更新先进入 Rescue。'),
    'configuration':('configuration.py','打开正常系统的应用配置向导。'),
    'firstrun':('admin.py','为离线 ext4 根文件系统创建管理员并导入 SSH 公钥。'),
    'led':('leds.py','查看 RGB 灯态、设置蓝色心跳或清除错误提示。'),
    'rescue':('rescue_boot.py','请求下一次进入 Rescue；--reboot 立即重启，--cancel 取消。'),
}
KUBE={
    'configuration':('kube_setup.py','联网安装、创建或加入 K3s 集群，修复或重试配置。'),
    'check':('kube_check.py','检查 K3s 数据挂载、绑定目录和安装状态。'),
}
RESCUE={
    'watchdog':('watchdog.py','准备、执行和报告受控复位诊断；并发仅 1。'),
    'usb-console':('usb_console.py','查看、启停 OTG 虚拟串口，读取当前或上次 Linux 启动日志。'),
    'ota':('ota.py','在 Rescue 内管理系统更新、备用救援槽与失败恢复。'),
    'configuration':('rescue_menu.py','打开交互式救援菜单。'),
    'status':('rescue.py','核对网络、磁盘、工厂归档及 BOOT 配套。'),
    'install':('rescue.py','安装 SATA：64 GiB ext4 系统分区和剩余数据分区。'),
    'restore':('rescue.py','恢复 SATA 系统分区，保留 DATA 和选定机器身份。'),
    'provision':('rescue.py','从本地交互终端配置 Rescue 管理员和 SSH 公钥。'),
    'firstrun':('admin.py','为离线 ext4 根文件系统配置管理员和 SSH 公钥。'),
    'led':('leds.py','查看或管理 Rescue RGB 灯态及蓝色心跳。'),
    'boot':('rescue_boot.py','请求下一次仍进入 Rescue，或取消待执行请求。'),
    'reboot':('console.py','正常重启；有有效 SATA 系统时自动进入正常系统。'),
    'console':('console.py','进入本地登录式 Bash 终端。'),
}

def dispatch(prog, commands, argv=None, *, rescue=False):
    extra='\nK3s 管理：armhpc-kube configuration / armhpc-kube check\n' if prog=='armhpc' and (LIBRARY/'kube_setup.py').is_file() else ''
    parser=argparse.ArgumentParser(prog=prog,description='ARMHPC 系统管理。',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='子命令：\n'+''.join(f'  {name:16s} {item[1]}\n' for name,item in commands.items())+
               extra+'\n查看具体参数：'+prog+' <子命令> --help')
    parser.add_argument('command',nargs='?',choices=tuple(commands),help='选择管理操作。')
    args=list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h','--help'):
        parser.parse_args(args);parser.print_help();return 0
    chosen=parser.parse_args(args[:1]).command
    if RESCUE_MARKER.is_file()!=rescue:
        parser.error('请在 Rescue 中使用 armhpc-rescue。' if RESCUE_MARKER.is_file() else '此命令仅在 Rescue 中使用。')
    module=LIBRARY/commands[chosen][0]
    if not module.is_file():parser.error('当前镜像未包含此功能：'+chosen)
    original=sys.argv
    sys.argv=[prog+' '+chosen,*args[1:]]
    try:
        if commands[chosen][0]=='rescue.py':
            return runpy.run_path(str(module))['entry'](chosen,args[1:])
        if commands[chosen][0]=='console.py':
            return runpy.run_path(str(module))['main'](chosen,args[1:])
        runpy.run_path(str(module),run_name='__main__',init_globals={'ARMHPC_PROG':prog+' '+chosen})
        return 0
    finally:sys.argv=original

def normal_main(argv=None):return dispatch('armhpc',NORMAL,argv)
def kube_main(argv=None):return dispatch('armhpc-kube',KUBE,argv)
def rescue_main(argv=None):return dispatch('armhpc-rescue',RESCUE,argv,rescue=True)
