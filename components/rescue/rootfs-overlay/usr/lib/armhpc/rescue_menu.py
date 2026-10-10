#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Interactive Rescue menu; disk writes retain the installer's confirmation."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

def select_sata_disk():
    result = subprocess.run(['lsblk', '--json', '--tree', '--bytes', '--output',
                             'NAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,MOUNTPOINTS'],
                            text=True, capture_output=True, check=True)
    candidates = [item for item in json.loads(result.stdout)['blockdevices']
                  if item.get('type') == 'disk' and item.get('tran') == 'sata']
    if not candidates:
        print('没有发现 SATA 整盘，请检查硬盘连接。')
        return None
    print('可选择的 SATA 磁盘（执行前仍会检查占用并确认序列号及容量）：')
    for number, item in enumerate(candidates, 1):
        print(f"  {number}  {item['path']}  {int(item['size'])/1024**3:.2f} GiB  "
              f"{(item.get('model') or '').strip()}  序列号 {item.get('serial') or '不可读取'}")
    answer = input('输入磁盘编号或完整路径（空白取消）: ').strip()
    if not answer:
        return None
    if answer.isdecimal() and 1 <= int(answer) <= len(candidates):
        chosen = candidates[int(answer)-1]['path']
    elif answer in {item['path'] for item in candidates}:
        chosen = answer
    else:
        print('无效选择：'+repr(answer)+'。请使用上面列出的编号或路径。')
        return None
    print('已选择：'+chosen)
    return chosen

def main():
    parser = argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG'), description='ARMHPC Rescue 交互配置引导。')
    parser.add_argument('--auto', action='store_true', help='首次交互式 SSH 登录时显示。')
    args = parser.parse_args()
    if not Path('/etc/armhpc/rescue-environment').is_file():
        parser.error('此命令只在 Rescue 中使用。')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    if os.geteuid() != 0:
        os.execv('/usr/bin/sudo', ['sudo', '--', '/usr/local/sbin/armhpc-rescue', 'configuration', *sys.argv[1:]])
    seen = Path('/root/.armhpc-rescue-wizard-seen')
    if args.auto and seen.exists():
        return
    seen.write_text('Interactive menu displayed\n')
    while True:
        print('\nARMHPC Rescue — ' + Path('/etc/hostname').read_text().strip())
        print('  1 查看网络、SSH、磁盘及工厂包状态')
        print('  2 安装 SATA：64 GiB 系统分区，其余为数据分区')
        print('  3 恢复 SATA 系统分区，保留数据分区')
        print('  4 重启：有有效 SATA 系统时自动进入正常系统')
        print('  0 进入命令行')
        choice = input('选择 [0]: ').strip() or '0'
        if choice == '0':
            return
        if choice == '1':
            subprocess.run(['armhpc-rescue', 'status'])
        elif choice in ('2', '3'):
            subprocess.run(['lsblk', '-o', 'NAME,SIZE,MODEL,SERIAL,TRAN,FSTYPE,LABEL,MOUNTPOINTS'])
            disk = select_sata_disk()
            if disk:
                action = 'install' if choice == '2' else 'restore'
                subprocess.run(['armhpc-rescue', action, '--disk', disk, '--execute'])
        elif choice == '4':
            subprocess.run(['armhpc-rescue', 'reboot'], check=True)
            return

if __name__ == '__main__':
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print('\n已返回命令行。')
