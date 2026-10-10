#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Save bounded boot/network snapshots without overriding the RGB state manager."""
import json
import os
from pathlib import Path
import re
import subprocess
import time

DESTINATION = Path('/var/log/armhpc')
NET = Path('/sys/class/net')
LIMIT = 48 * 1024


def read(path, maximum=2048):
    try:
        with Path(path).open('rb') as stream:
            return stream.read(maximum).decode('utf-8', errors='replace').replace('\0', '').strip()
    except OSError:
        return None


def command(*arguments):
    try:
        result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=8, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C.UTF-8'})
        return {'exit_code': result.returncode,
                'output': result.stdout[-LIMIT:].decode('utf-8', errors='replace')}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'error': type(error).__name__}


def interfaces():
    rows = []
    for directory in sorted(NET.glob('*')):
        if directory.name == 'lo':
            continue
        row = {'name': directory.name}
        for attribute in ('address', 'operstate', 'carrier', 'speed', 'type'):
            row[attribute] = read(directory / attribute)
        row['hardware_device'] = (directory / 'device').exists()
        for attribute in ('device/driver', 'phydev/driver'):
            path = directory / attribute
            row[attribute] = path.resolve().name if path.is_symlink() else None
        row['phy_id'] = read(directory / 'phydev/phy_id')
        rows.append(row)
    return rows


def snapshot():
    rows = interfaces()
    # A hardware Ethernet interface confirms driver registration, not physical link.
    ethernet = [row for row in rows if row['hardware_device'] and row['type'] == '1']
    state = 'no-hardware-ethernet' if not ethernet else (
        'carrier-present' if any(row['carrier'] == '1' for row in ethernet) else 'no-carrier')
    # Only collect networking/kernel status. Do not inspect users, keys or K3s config.
    boot_arguments = read('/proc/cmdline') or ''
    return {'uptime': read('/proc/uptime'), 'kernel': os.uname().release,
            'model': read('/proc/device-tree/model'),
            'boot_selection': [part for part in boot_arguments.split()
                               if part.startswith(('root=', 'rootfstype=', 'armhpc.mode=', 'console='))],
            'root_mount': command('findmnt', '-rn', '--mountpoint', '/', '-o', 'SOURCE,FSTYPE,OPTIONS'),
            'network_state': state, 'interfaces': rows,
            'ethernet_details': {row['name']: {
                'link': command('ethtool', row['name']),
                'driver': command('ethtool', '-i', row['name'])
            } for row in ethernet},
            'ip_link': command('ip', '-br', 'link'), 'ip_address': command('ip', '-br', 'address'),
            'kernel_log': command('journalctl', '-k', '-b', '-n', '350', '--no-pager', '-o', 'short-monotonic'),
            'network_log': command('journalctl', '-b', '-u', 'systemd-networkd', '-n', '120', '--no-pager', '-o', 'short-monotonic')}


def save(destination, boot_id, records):
    if destination.is_symlink():
        raise RuntimeError('Diagnostic directory cannot be a symlink.')
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    if not re.fullmatch(r'[0-9a-f-]{36}', boot_id):
        raise RuntimeError('Invalid boot identifier.')
    data = json.dumps({'schema_version': 1, 'boot_id': boot_id,
                       'hardware_validation': 'runtime observations only',
                       'snapshots': records}, ensure_ascii=False, indent=2) + '\n'
    if len(data.encode()) > 512 * 1024:
        raise RuntimeError('Diagnostic snapshot exceeds its size limit.')
    for name in ('boot-' + boot_id + '.json', 'boot-diagnostics-latest.json'):
        temporary = destination / (name + '.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination / name)
    history = sorted((path for path in destination.glob('boot-*.json')
                      if path.name != 'boot-diagnostics-latest.json'),
                     key=lambda path: path.stat().st_mtime, reverse=True)
    for old in history[4:]:
        old.unlink()


def main():
    boot_id = read('/proc/sys/kernel/random/boot_id')
    records = [snapshot()]
    save(DESTINATION, boot_id, records)
    time.sleep(30)
    records.append(snapshot())
    save(DESTINATION, boot_id, records)
    print('ARMHPC boot diagnostics: ' + records[-1]['network_state'] + '; ' + str(DESTINATION / 'boot-diagnostics-latest.json'))


if __name__ == '__main__':
    main()
