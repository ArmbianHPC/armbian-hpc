# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Board-configured status colours, panel link indication and fatal faults."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

POLICY = Path('/etc/armhpc/led.json')
PREFERENCES = Path('/etc/armhpc/led-preferences.json')
FAULT = Path('/run/armhpc/led-error.json')
STATUS = Path('/run/armhpc/led-status.json')
LEDS = Path('/sys/class/leds')
NET = Path('/sys/class/net')


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


def colour(mode, heartbeat=True, policy=None):
    if mode not in ('rescue', 'normal', 'error'):
        raise ValueError('Unknown RGB mode')
    result = {'red': int(mode in ('rescue', 'error')),
              'green': int(mode in ('rescue', 'normal')), 'blue': 0}
    overrides = (policy or {}).get('mode_colours', {}).get(mode, {})
    if any(k not in result or type(v) is not int or v not in (0, 1) for k, v in overrides.items()):
        raise ValueError('Invalid board LED colour')
    result.update(overrides)
    result['heartbeat'] = bool(heartbeat and mode != 'error')
    return result


def write_channel(root, name, value, heartbeat=False):
    if not name:
        return 'unconfigured'
    if not isinstance(name, str) or name in ('.', '..') or '/' in name or '\\' in name:
        raise ValueError('Invalid board LED name')
    directory = Path(root)/name
    if not directory.is_dir():
        return 'unavailable'
    try:
        (directory/'trigger').write_text('none\n')
        (directory/'brightness').write_text(str(value)+'\n')
        if heartbeat:
            (directory/'trigger').write_text('heartbeat\n')
        return 'applied'
    except OSError as error:
        return str(error)


def needs_retry(hardware):
    return any(value not in ('applied', 'unconfigured') for value in hardware.values())


def apply(mode, heartbeat=True, *, root=Path('/sys/class/leds'), policy=None):
    policy = policy or read_json(POLICY, {})
    desired = colour(mode, heartbeat, policy)
    results = {}
    # Named status LEDs prevent changing green/yellow PHY LEDs on the same board.
    for name in ('red', 'green', 'blue'):
        led_name = policy.get('channels', {}).get(name)
        results[name] = write_channel(root, led_name, desired[name], name == 'blue' and desired['heartbeat'])
    return {'mode': mode, 'channels': desired, 'hardware': results}


def network_state(policy, *, root=None):
    root = NET if root is None else Path(root)
    config = policy['network']
    interfaces = config.get('interfaces', [])
    if not interfaces or any(not isinstance(n, str) or n in ('.', '..') or
                             not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', n) for n in interfaces):
        raise ValueError('Invalid panel network interfaces')
    carriers, warnings = {}, []
    for name in interfaces:
        try:
            value = (root/name/'carrier').read_text().strip()
            if value not in ('0', '1'):
                raise ValueError('Invalid carrier value')
            carriers[name] = value == '1'
        except (OSError, ValueError) as error:
            carriers[name] = None
            warnings.append(name+': '+str(error))
    linked = any(value is True for value in carriers.values())
    channels = config.get('channels', {})
    inverted = config.get('inverted_channels', [])
    if not channels or set(channels)-{'green', 'red'} or set(inverted)-set(channels):
        raise ValueError('Invalid panel network LED channels')
    if len(set(channels.values())) != len(channels) or set(channels.values()) & set(policy.get('channels', {}).values()):
        raise ValueError('Panel LED channels must be distinct')
    values = {name: int(linked if name == 'green' else not linked) ^ int(name in inverted) for name in channels}
    return {'linked': linked, 'interfaces': carriers, 'warnings': warnings,
            'outputs': {name: {'led': led, 'brightness': values[name]} for name, led in channels.items()}}


def update_network(policy, previous):
    current = network_state(policy)
    hardware = previous.get('hardware', {})
    if current['outputs'] != previous.get('outputs') or not hardware or needs_retry(hardware):
        hardware = {name: write_channel(LEDS, item['led'], item['brightness'])
                    for name, item in current['outputs'].items()}
    return {**current, 'hardware': hardware}


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
    desired = colour(mode, heartbeat, policy)
    # Avoid restarting the heartbeat timer on every monitor tick.
    retry_hardware = needs_retry(previous.get('hardware', {}))
    state = previous if previous.get('mode') == mode and previous.get('channels') == desired and not retry_hardware else apply(mode, heartbeat, policy=policy)
    state = {k: v for k, v in state.items() if k != 'network'}
    if policy.get('network'):
        state['network'] = update_network(policy, previous.get('network', {}))
    save(STATUS, {**state, 'reason': reason})


def main():
    parser = argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG'), description='按板卡配置显示系统状态、网络链路和错误灯态。')
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
