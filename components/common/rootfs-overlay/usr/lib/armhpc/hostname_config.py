#!/usr/bin/python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Configure the normal-system hostname and retain it on subsequent boots."""
import argparse
from pathlib import Path
import re
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_io import atomic_write, configuration_lock, normal_system, safe_path, require_idle, require, approve, emit, entry

PLACEHOLDERS = {'armbian', 'armhpc', 'localhost', 'localhost.localdomain'}


def valid_hostname(value):
    return (isinstance(value, str) and 1 <= len(value) <= 64 and
            all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', label)
                for label in value.split('.')))


def boot_hostname(current, generated, rescue=False):
    if rescue or not valid_hostname(current) or current.lower() in PLACEHOLDERS:
        return generated
    return current


def update_hosts(content, hostname):
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if re.match(r'^\s*127\.0\.1\.1\s', line):
            addresses, separator, comment = line.partition('#')
            tokens = addresses.split()
            aliases = [alias for alias in tokens[2:] if alias != hostname]
            lines[index] = '127.0.1.1\t' + ' '.join([hostname, *aliases]) + (' #' + comment if separator else '')
            break
    else:
        lines.append('127.0.1.1\t' + hostname)
    return '\n'.join(lines) + '\n'


def show(root=Path('/')):
    path = safe_path(root, 'etc/hostname')
    return {'hostname': path.read_text().strip() if path.is_file() else None,
            'runtime_hostname': socket.gethostname() if Path(root) == Path('/') else None,
            'mode': 'rescue' if (Path(root) / 'etc/armhpc/rescue-environment').is_file() else 'system'}


def set_hostname(name, root=Path('/'), runtime=True):
    normal_system(root)
    require(valid_hostname(name) and name.lower() not in PLACEHOLDERS,
            'hostname_invalid', 'Use a DNS hostname of at most 64 characters; reserved default names are not allowed')
    with configuration_lock(root):
        require_idle(root)
        paths = [safe_path(root, name) for name in ('etc/hosts', 'etc/hostname')]
        previous = [(p.read_bytes(), p.stat().st_mode & 0o777) if p.is_file() else None for p in paths]
        old_runtime = socket.gethostname()
        try:
            atomic_write(paths[0], update_hosts(previous[0][0].decode() if previous[0] else '127.0.0.1 localhost\n', name))
            atomic_write(paths[1], name + '\n')
            if runtime and Path(root) == Path('/'):
                socket.sethostname(name.encode('ascii'))
        except Exception:
            for path, old in zip(paths, previous):
                if old:
                    atomic_write(path, old[0], old[1])
                else:
                    path.unlink(missing_ok=True)
            if runtime and Path(root) == Path('/'):
                socket.sethostname(old_runtime.encode('ascii'))
            raise
    return {**show(root), 'changed': True, 'persistent': True, 'reboot_required': False}


def main(argv=None):
    parser = argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG', 'armhpc hostname'), description=__doc__)
    parser.add_argument('action', nargs='?', default='show', choices=('show', 'set', 'configure'))
    parser.add_argument('name', nargs='?', help='Normal-system hostname for set.')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--yes', action='store_true')
    args = parser.parse_args(argv)
    require(args.name is None or args.action == 'set', 'arguments', 'A hostname is accepted only by set')
    if args.action == 'show':
        emit(show(), args.json)
        return 0
    normal_system(Path('/'))
    if args.action == 'configure':
        require(not args.json and sys.stdin.isatty(), 'interactive_required', 'Run the hostname wizard from an interactive terminal')
        current = show()['hostname'] or ''
        args.name = input(f'主机名 [{current}]: ').strip() or current
    require(args.name is not None, 'arguments', 'Provide a hostname: armhpc hostname set NAME')
    require(valid_hostname(args.name) and args.name.lower() not in PLACEHOLDERS, 'hostname_invalid', 'Invalid or reserved hostname')
    approve(args.yes, '保存并立即应用主机名 ' + args.name + '？')
    emit(set_hostname(args.name), args.json)
    return 0


if __name__ == '__main__':
    raise SystemExit(entry(main))
