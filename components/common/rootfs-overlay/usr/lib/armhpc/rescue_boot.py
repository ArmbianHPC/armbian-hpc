# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import argparse
import os
import subprocess
from firstboot import request_rescue

def main():
    parser=argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG'), description='请求下一次进入 Rescue；默认不重启。')
    parser.add_argument('--cancel',action='store_true',help='取消尚未执行的 Rescue 请求。')
    parser.add_argument('--reboot',action='store_true',help='设置请求后立即重启。')
    args=parser.parse_args()
    if os.geteuid()!=0:parser.error('请使用 sudo 或 root 运行。')
    request_rescue(args.cancel)
    print('已取消 Rescue 启动请求。' if args.cancel else '下一次启动将进入 Rescue。')
    if args.reboot:subprocess.run(['systemctl','reboot'],check=True)

if __name__=='__main__':main()
