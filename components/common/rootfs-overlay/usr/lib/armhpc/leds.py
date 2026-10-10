# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Board-configured RGB mode colours, optional blue heartbeat, and fatal faults."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

POLICY = Path('/etc/armhpc/led.json')
PREFERENCES = Path('/etc/armhpc/led-preferences.json')
FAULT = Path('/run/armhpc/led-error.json')
STATUS = Path('/run/armhpc/led-status.json')


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.'+path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def colour(mode, heartbeat=True):
    if mode not in ('rescue', 'normal', 'error'):
        raise ValueError('Unknown RGB mode')
    return {'red': int(mode in ('rescue', 'error')),
            'green': int(mode in ('rescue', 'normal')),
            'blue': 0, 'heartbeat': bool(heartbeat and mode != 'error')}


def apply(mode, heartbeat=True, *, root=Path('/sys/class/leds'), policy=None):
    policy = policy or read_json(POLICY, {})
    desired = colour(mode, heartbeat)
    results = {}
    # Named status LEDs prevent changing green/yellow PHY LEDs on the same board.
    for name in ('red', 'green', 'blue'):
        led_name = policy.get('channels', {}).get(name)
        if not led_name or '/' in led_name:
            results[name] = 'unconfigured'
            continue
        directory = Path(root)/led_name
        if not directory.is_dir():
            results[name] = 'unavailable'
            continue
        try:
            (directory/'trigger').write_text('none\n')
            (directory/'brightness').write_text(str(desired[name])+'\n')
            if name == 'blue' and desired['heartbeat']:
                # Kernel heartbeat flashes only blue, over the base R/G colour.
                (directory/'trigger').write_text('heartbeat\n')
            results[name] = 'applied'
        except OSError as error:
            results[name] = str(error)
    return {'mode': mode, 'channels': desired, 'hardware': results}


def mark_error(reason):
    if not POLICY.is_file():
        return
    save(FAULT, {'reason': str(reason)[:512]})
    save(STATUS, {**apply('error'), 'reason': str(reason)[:512]})


def base_mode():
    return 'rescue' if Path('/etc/armhpc/rescue-environment').is_file() else 'normal'


def failed_units(names):
    if not names:
        return []
    result = subprocess.run(['systemctl', 'show', '--property=Id,ActiveState', *names],
                            text=True, capture_output=True, timeout=5)
    failed = []
    for block in result.stdout.strip().split('\n\n'):
        fields = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        if fields.get('ActiveState') == 'failed':
            failed.append(fields.get('Id', 'unknown'))
    return failed


def update(policy, *, check_storage=False):
    fault = read_json(FAULT, {})
    failures = failed_units(policy.get('critical_units', []))
    reason = fault.get('reason') or ('Required service failed: '+', '.join(failures) if failures else None)
    mode = base_mode()
    if check_storage and mode == 'normal':
        mounted = subprocess.run(['findmnt', '-rn', '--mountpoint', '/data', '-o', 'FSTYPE'],
                                 text=True, capture_output=True, timeout=5)
        if mounted.stdout.strip() != 'ext4':
            reason = reason or 'Normal system DATA partition is not mounted as ext4'
    if reason:
        mode = 'error'
    heartbeat = read_json(PREFERENCES, {}).get('heartbeat', policy.get('heartbeat', True))
    previous = read_json(STATUS, {})
    desired = colour(mode, heartbeat)
    # Avoid restarting the heartbeat timer on every monitor tick.
    retry_hardware = bool(previous.get('hardware')) and any(value != 'applied' for value in previous['hardware'].values())
    state = previous if previous.get('mode') == mode and previous.get('channels') == desired and not retry_hardware else apply(mode, heartbeat, policy=policy)
    save(STATUS, {**state, 'reason': reason})


def main():
    parser = argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG'), description='RGB：Rescue 黄、正常系统绿、严重错误红，可叠加蓝色心跳。')
    parser.add_argument('--status', action='store_true', help='查看逻辑灯态与硬件通道状态。')
    parser.add_argument('--heartbeat', choices=('on', 'off'), help='持久设置蓝色心跳叠加。')
    parser.add_argument('--error', metavar='REASON', help='记录严重错误并显示纯红色。')
    parser.add_argument('--clear-error', action='store_true', help='清除手动／写盘错误记录，重新评估系统状态。')
    parser.add_argument('--daemon', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.status:
        print(json.dumps(read_json(STATUS, {}), ensure_ascii=False, indent=2))
        return
    if os.geteuid() != 0:
        parser.error('需要 root 权限。')
    policy = read_json(POLICY, {})
    if not policy:
        return
    if args.error:
        mark_error(args.error)
        return
    if args.clear_error:
        FAULT.unlink(missing_ok=True)
    if args.heartbeat:
        save(PREFERENCES, {'heartbeat': args.heartbeat == 'on'})
        STATUS.unlink(missing_ok=True)
    if not args.daemon:
        update(policy)
        return
    running = True
    def stop(signum, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    started = time.monotonic()
    while running:
        try:
            update(policy, check_storage=time.monotonic()-started >= 45)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print('ARMHPC LED monitor: '+str(error), flush=True)
        time.sleep(5)


if __name__ == '__main__':
    main()
