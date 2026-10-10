#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Check persistent mounts before K3s starts."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main(argv=()):
    argparse.ArgumentParser(prog="armhpc-kube check", description="检查 K3s 所需数据挂载、绑定目录和安装状态。").parse_args(argv)
    policy = json.loads(Path('/etc/image-framework/k3s.json').read_text())
    mountpoint = policy['data_mountpoint']
    mount = subprocess.check_output(
        ['findmnt', '-rn', '-M', mountpoint, '-o', 'TARGET,FSTYPE,LABEL'], text=True
    ).split()
    if mount != [mountpoint, policy['data_filesystem'], policy['data_label']]:
        raise RuntimeError(f'{mountpoint} 必须是标签正确的独立 {policy["data_filesystem"]} 数据分区。')
    for source, target in [
        (policy['kubelet_source'], policy['kubelet_target']),
        (policy['node_identity_source'], policy['node_identity_target']),
    ]:
        if not subprocess.run(['mountpoint', '-q', target]).returncode == 0:
            raise RuntimeError(f'{target} 尚未绑定数据分区。')
        if not os.path.samefile(source, target):
            raise RuntimeError(f'{target} 与预期数据目录不一致。')
    state = json.loads(Path(policy['state_file']).read_text())
    if state.get('status') not in ('starting', 'installed'):
        raise RuntimeError('安装或恢复尚未完成。请通过 SSH 运行 armhpc configuration。')


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'K3s 启动检查失败：{exc}', file=sys.stderr)
        sys.exit(1)
