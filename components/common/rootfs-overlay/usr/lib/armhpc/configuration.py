#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Dispatch the workload selected when this image was composed."""

import argparse
import json
import os
from pathlib import Path
import sys
import subprocess


def network_login_confirmation():
    if Path('/etc/armhpc/rescue-environment').is_file():
        return False
    if os.geteuid() == 0:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from network_config import Network, login_prompt
        return login_prompt(Network()) == 10
    result = subprocess.run(['sudo', '-n', '--', '/usr/bin/python3',
                             str(Path(__file__).with_name('network_config.py')), '_login'],
                            stderr=subprocess.PIPE, text=True, timeout=610)
    if result.returncode not in (0, 10):
        print('网络确认提示需要管理员权限；如有待确认配置，请运行 sudo armhpc network confirm。', file=sys.stderr)
    return result.returncode == 10


def main():
    parser = argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG'), description='打开正常系统选择的应用配置向导。')
    parser.add_argument('--auto', action='store_true', help='首次交互式 SSH 登录触发。')
    parser.add_argument('--preview', action='store_true', help='只预览配置。')
    parser.add_argument('--policy', type=Path, help='预览时使用指定策略文件。')
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    if args.auto and network_login_confirmation():
        return
    if args.auto and os.geteuid() == 0:
        policy_path=Path('/etc/image-framework/k3s.json')
        if policy_path.is_file():
            from kube_runtime import installed, mark_joined
            policy=json.loads(policy_path.read_text())
            if Path(policy['state_file']).is_file() and installed(policy)['ready']:
                previous=json.loads(Path(policy['state_file']).read_text())
                if previous.get('plan'):
                    mark_joined(policy, previous['plan'])
        from auth_config import remind
        remind()
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
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'节点配置入口不可用：{exc}', file=sys.stderr)
        sys.exit(1)
