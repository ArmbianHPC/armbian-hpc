#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Dispatch the workload selected when this image was composed."""

import argparse
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG'), description='打开正常系统选择的应用配置向导。')
    parser.add_argument('--auto', action='store_true', help='首次交互式 SSH 登录触发。')
    parser.add_argument('--preview', action='store_true', help='只预览配置。')
    parser.add_argument('--policy', type=Path, help='预览时使用指定策略文件。')
    parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    node = json.loads(Path('/etc/image-framework/node.json').read_text())
    if node['workload_component'] == 'none':
        if '--auto' not in sys.argv:
            print('此镜像未选择应用配置引导。')
        return
    if node['workload_component'] != 'k3s':
        raise RuntimeError('此应用没有注册配置入口。')
    os.execv('/usr/local/sbin/armhpc-kube', ['armhpc-kube', 'configuration', *sys.argv[1:]])


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'节点配置入口不可用：{exc}', file=sys.stderr)
        sys.exit(1)
