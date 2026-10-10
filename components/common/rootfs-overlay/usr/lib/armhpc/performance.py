# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Use performance governors within hardware-provided frequency tables."""
import json
import os
from pathlib import Path
import subprocess


def apply_cpu(policy):
    maximum = (policy/'cpuinfo_max_freq').read_text().strip()
    minimum = (policy/'cpuinfo_min_freq').read_text().strip()
    available = (policy/'scaling_available_governors').read_text().split()
    if 'performance' not in available:
        return {'policy': str(policy), 'applied': False, 'reason': 'performance governor unavailable'}
    if not maximum.isdecimal() or not minimum.isdecimal():
        raise ValueError('CPU hardware frequency bounds are invalid')
    (policy/'scaling_min_freq').write_text(minimum+'\n')
    (policy/'scaling_max_freq').write_text(maximum+'\n')
    (policy/'scaling_governor').write_text('performance\n')
    return {'policy': str(policy), 'applied': True, 'hardware_max_khz': int(maximum),
            'requested_max_khz': int((policy/'scaling_max_freq').read_text()), 'governor': 'performance'}


def apply_devfreq(device):
    available = (device/'available_governors').read_text().split()
    if 'performance' not in available:
        return {'device': str(device), 'applied': False, 'reason': 'performance governor unavailable'}
    # Zero removes a userspace devfreq ceiling; driver OPP and cooling QoS remain.
    for name in ('min_freq', 'max_freq'):
        path = device/name
        if path.is_file():
            path.write_text('0\n')
    (device/'governor').write_text('performance\n')
    return {'device': str(device), 'applied': True, 'governor': 'performance', 'userspace_limits': 'cleared'}


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Performance setup requires root')
    subprocess.run(['modprobe', 'governor_performance'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results = {'overclock': False, 'voltage_table_modified': False, 'thermal_protection': 'kernel defaults retained',
               'cpu': [], 'devfreq': []}
    for directory, operation, key in (
        (Path('/sys/devices/system/cpu/cpufreq'), apply_cpu, 'cpu'),
        (Path('/sys/class/devfreq'), apply_devfreq, 'devfreq')):
        for policy in sorted(directory.glob('policy*' if key == 'cpu' else '*')):
            try:
                results[key].append(operation(policy))
            except (OSError, ValueError) as error:
                results[key].append({'device': str(policy), 'applied': False, 'reason': str(error)})
    destination = Path('/run/armhpc/performance.json')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(results, indent=2)+'\n')
    print(json.dumps(results, indent=2), flush=True)


if __name__ == '__main__':
    main()
