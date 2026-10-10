# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import argparse
import os
import subprocess

def main(action,argv=None):
    parser=argparse.ArgumentParser(prog='armhpc-rescue '+action,description='进入本地终端或正常重启。')
    parser.parse_args(argv)
    if action=='reboot':
        if os.geteuid()!=0:parser.error('请使用 sudo 或 root 运行。')
        subprocess.run(['systemctl','reboot'],check=True);return 0
    print('\nARMHPC Rescue: armhpc-rescue --help; armhpc-rescue status',flush=True)
    os.execv('/bin/bash',['bash','-l'])
