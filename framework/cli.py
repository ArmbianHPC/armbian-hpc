# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Local entrypoint for framework inventory, plans, and staging."""

import argparse
import json
from pathlib import Path
import sys
import subprocess

from .config import ARCHITECTURES, ConfigError, catalog, resolve
from .staging import stage
from .sources import prepare_sources
from .release import source_audit, check_release, prepare_release, prepare_source_release


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description='ARM64 / ARM32 Linux 镜像配置框架；不操作物理磁盘。')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1], help='框架工作区根目录。')
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('list', help='列出已登记的配置和目标。')
    subparsers.add_parser('architectures', help='显示 ARM64 / ARM32 架构映射。')
    for name in ('validate', 'plan', 'stage'):
        command = subparsers.add_parser(name)
        command.add_argument('target', help='targets 下的目标标识。')
        if name != 'stage':
            command.add_argument('--verify-sources', action='store_true', help='校验本地输入文件 SHA256。')
        else:
            command.add_argument('--output', help='工作区 build/staging 内尚不存在的相对目录。')
    command = subparsers.add_parser('check-kernel', help='检查实际 .config；不修改内核。')
    command.add_argument('target')
    command.add_argument('config', type=Path)
    command = subparsers.add_parser('fetch-sources', help='按目标准备固定源码；不获取引导链或上游 K3s。')
    command.add_argument('target')
    command.add_argument('--verify', action='store_true', help='只校验现有文件，不下载或写入。')
    command = subparsers.add_parser('check-source', help='检查指定源码 commit，禁止镜像、缓存及身份文件进入归档。')
    command.add_argument('--ref', default='HEAD')
    command = subparsers.add_parser('check-release', help='检查实际镜像、对应源码及构建/实机记录；Linux 只读检查。')
    command.add_argument('specification', type=Path)
    command = subparsers.add_parser('prepare-release', help='检查通过后准备本地 Release 附件，不上传。')
    command.add_argument('specification', type=Path)
    command.add_argument('--output', required=True)
    command = subparsers.add_parser('prepare-source-release', help='导出已审计源码，不生成镜像。')
    command.add_argument('--ref', default='HEAD')
    command.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'list':
            emit(catalog(args.root))
            return 0
        if args.command == 'architectures':
            emit(ARCHITECTURES)
            return 0
        if args.command == 'check-source':
            result = source_audit(args.root, args.ref)
            emit(result)
            return 0 if result['passed'] else 1
        if args.command == 'prepare-source-release':
            emit(prepare_source_release(args.root, args.ref, args.output))
            return 0
        if args.command == 'check-release':
            result = check_release(args.root, args.specification)
            emit(result)
            return 0 if result['passed'] else 1
        if args.command == 'prepare-release':
            emit({'release_bundle': str(prepare_release(args.root, args.specification, args.output)), 'published': False})
            return 0
        resolved = resolve(args.root, args.target, verify_sources=getattr(args, 'verify_sources', False),
                           require_sources=args.command == 'stage')
        if args.command == 'plan':
            emit(resolved)
        elif args.command == 'validate':
            emit({'target': args.target, 'configuration_valid': True, 'architecture': resolved['architecture'],
                  'sources_verified': args.verify_sources, 'build_ready': resolved['build_ready'],
                  'missing_inputs': resolved['missing_inputs'],
                  'blockers': resolved['blockers']})
        elif args.command == 'stage':
            destination = stage(args.root, resolved, args.output)
            emit({'stage': str(destination), 'configuration_valid': True,
                  'sources_verified': True, 'build_ready': resolved['build_ready'],
                  'image_built': False, 'blockers': resolved['blockers']})
        elif args.command == 'fetch-sources':
            emit(prepare_sources(args.root, resolved, verify_only=args.verify))
        elif args.command == 'check-kernel':
            values = dict(line.strip().split('=', 1) for line in args.config.read_text().splitlines()
                          if line.startswith('CONFIG_') and '=' in line)
            missing = {name: {'actual': values.get(name, 'n'), 'allowed': allowed}
                       for name, allowed in resolved['kernel_requirements'].items()
                       if values.get(name) not in allowed}
            emit({'target': args.target, 'kernel_requirements_met': not missing, 'missing': missing})
            return 1 if missing else 0
        return 0
    except (ConfigError, OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        print(f'配置失败：{exc}', file=sys.stderr)
        return 2
