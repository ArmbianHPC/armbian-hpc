# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Identify a network port through its green PHY LED and restore its rules."""
import argparse
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid

NET = Path('/sys/class/net')
LEDS = Path('/sys/class/leds')
POLICY = Path('/etc/armhpc/phy-led.json')
STATE = Path('/run/armhpc/net-identify')
MODE_FIELDS = ('link', 'link_10', 'link_100', 'link_1000', 'link_2500',
               'link_5000', 'link_10000', 'half_duplex', 'full_duplex',
               'rx', 'tx', 'rx_err', 'tx_err')


def read(path):
    return Path(path).read_text(encoding='ascii').strip()


def write(path, value):
    Path(path).write_text(str(value) + '\n', encoding='ascii')


def boot_id():
    return read('/proc/sys/kernel/random/boot_id')


def load_policy():
    return json.loads(POLICY.read_text(encoding='utf-8')) if POLICY.is_file() else {}


def interface_name(requested=None):
    name = requested or load_policy().get('interface')
    if not name:
        candidates = [p.name for p in NET.iterdir() if (p / 'device').is_dir()]
        if len(candidates) != 1:
            raise ValueError('Specify --interface when there is not exactly one physical port')
        name = candidates[0]
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', name) or name in ('.', '..') or not (NET / name / 'device').is_dir():
        raise ValueError('Invalid or unavailable physical network interface')
    supported = load_policy().get('supported_interfaces')
    if supported is not None and name not in supported:
        raise ValueError('This board supports identification only on: ' + ', '.join(supported))
    return name


def led_index(led):
    node = led / 'of_node'
    if node.exists() and (node / 'reg').is_file():
        data = (node / 'reg').read_bytes()
        return struct.unpack('>I', data)[0] if len(data) == 4 else None
    match = re.search(r':lan-(\d+)$', led.name)
    return int(match[1]) if match else None


def associated(led, interface):
    port = NET / interface
    phy = port / 'phydev'
    device = led / 'device'
    if device.exists() and device.resolve() in {p.resolve() for p in (phy, port / 'device') if p.exists()}:
        return True
    node = phy / 'of_node'
    if not node.exists():
        return False
    parent = led / 'device/of_node'
    child = led / 'of_node'
    return ((parent.exists() and parent.resolve() == node.resolve()) or
            (child.exists() and child.resolve().parent.parent == node.resolve()))


def select_led(interface):
    policy = load_policy()
    index = policy.get('identify_index') if policy.get('interface') == interface else None
    if index is not None and (type(index) is not int or not 0 <= index < 32):
        raise ValueError('Invalid board identification LED index')
    candidates = [p for p in LEDS.iterdir() if len(p.name.split(':')) >= 3 and
                  p.name.rsplit(':', 2)[-2] == 'green' and associated(p, interface) and
                  (index is None or led_index(p) == index)]
    if len(candidates) != 1:
        raise ValueError('A unique green LED associated with this network port is required')
    return candidates[0]


def current_speed(interface, settings):
    port = NET / interface
    if read(port / 'carrier') != '1':
        raise ValueError('Connect the network cable: identification requires an active link')
    key = 'link_' + str(int(read(port / 'speed')))
    if key not in settings:
        raise ValueError('The PHY LED does not expose the negotiated link speed')
    return key


def capture(interface):
    led = select_led(interface)
    if '[netdev]' not in read(led / 'trigger') or read(led / 'device_name') != interface or read(led / 'offloaded') != '1':
        raise ValueError('The green PHY LED must use the hardware-offloaded netdev trigger')
    settings = {name: int(read(led / name)) for name in MODE_FIELDS if (led / name).is_file()}
    if not settings or any(value not in (0, 1) for value in settings.values()):
        raise ValueError('Invalid network LED rules')
    current_speed(interface, settings)
    return {'led': led.name, 'led_path': str(led.resolve()), 'settings': settings}


def validate_led(interface, snapshot):
    led = select_led(interface)
    if led.name != snapshot['led'] or str(led.resolve()) != snapshot['led_path']:
        raise ValueError('The saved identification LED belongs to a different device')
    return led


def set_rules(led, settings):
    # Clear mutually exclusive link rules before restoring their saved values.
    for name, value in [(n, 0) for n in settings] + [(n, v) for n, v in settings.items() if v]:
        try:
            write(led / name, value)
        except OSError as error:
            # RX and TX must be updated together on some PHYs. Verify the final pair.
            if error.errno != errno.EOPNOTSUPP:
                raise
    if any(read(led / name) != str(value) for name, value in settings.items()) or read(led / 'offloaded') != '1':
        raise ValueError('The PHY rejected the requested hardware LED rules')


def restore(interface, snapshot):
    led = validate_led(interface, snapshot)
    if '[netdev]' not in read(led / 'trigger'):
        write(led / 'trigger', 'netdev')
    if read(led / 'device_name') != interface:
        write(led / 'device_name', interface)
    set_rules(led, snapshot['settings'])


def save(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def state_file(interface):
    return STATE / (interface + '.json')


def load_state(interface):
    path = state_file(interface)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'stage': 'idle', 'interface': interface}


def initialize_state():
    if STATE.is_symlink():
        raise ValueError('Identification state directory must not be a symlink')
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)


@contextmanager
def locked(path, blocking=True):
    import fcntl
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def busy(interface):
    import fcntl
    path = STATE / (interface + '.owner.lock')
    if not path.exists():
        return False
    with path.open('r') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def status(interface):
    data = load_state(interface)
    data['active'] = busy(interface)
    if data['stage'] in ('queued', 'running', 'restoring') and not data['active']:
        data['stage'] = 'interrupted'
    if data.get('boot_id') and data['boot_id'] != boot_id():
        data.update(stage='expired', active=False)
    return {k: v for k, v in data.items() if k != 'snapshot'}


def repair_previous(interface):
    data = load_state(interface)
    if data.get('snapshot') and not data.get('restored') and data.get('boot_id') == boot_id():
        restore(interface, data['snapshot'])
        data.update(stage='stopped', restored=True)
        save(state_file(interface), data)


def start(interface, seconds, period_ms):
    if not 1 <= seconds <= 600 or not 200 <= period_ms <= 5000:
        raise ValueError('Seconds must be 1–600; blink period must be 200–5000 ms')
    initialize_state()
    with locked(STATE / (interface + '.control.lock')):
        if busy(interface):
            raise ValueError('This port is already flashing; use --stop first')
        repair_previous(interface)
        snapshot = capture(interface)
        task = uuid.uuid4().hex
        unit = 'armhpc-identify-' + hashlib.sha256(interface.encode()).hexdigest()[:12] + '-' + task[:12]
        data = {'protocol': 1, 'task_id': task, 'interface': interface, 'led': snapshot['led'],
                'stage': 'queued', 'seconds': seconds, 'period_ms': period_ms, 'boot_id': boot_id(),
                'snapshot': snapshot, 'restored': False, 'unit': unit}
        save(state_file(interface), data)
        command = ['systemd-run', '--quiet', '--collect', '--unit=' + unit,
                   '--property=Type=exec', '--property=Restart=no', '--property=NoNewPrivileges=yes',
                   '--property=RuntimeMaxSec=' + str(seconds + 10), '--property=TimeoutStopSec=5',
                   sys.executable, str(Path(__file__).resolve()), '--interface', interface, '--worker', task]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            repair_previous(interface)
            raise ValueError('Could not start the identification service') from None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = load_state(interface)
            if current['stage'] in ('running', 'failed', 'restore-failed') or current.get('restored'):
                if current['stage'] in ('failed', 'restore-failed'):
                    raise ValueError(current.get('error', 'Identification failed'))
                return status(interface)
            time.sleep(0.05)
        raise ValueError('Identification service did not confirm startup; check --status or use --stop')


def stop(interface):
    initialize_state()
    with locked(STATE / (interface + '.control.lock')):
        data = load_state(interface)
        if busy(interface):
            unit = data.get('unit', '')
            if not re.fullmatch(r'armhpc-identify-[0-9a-f]{12}-[0-9a-f]{12}', unit):
                raise ValueError('Invalid identification service identity')
            subprocess.run(['systemctl', 'stop', unit], check=True, capture_output=True, timeout=8)
        if busy(interface):
            raise ValueError('Identification is still running; inspect the service')
        repair_previous(interface)
        return status(interface)


def flash(interface, task, stop_event):
    with locked(STATE / (interface + '.owner.lock'), blocking=False):
        data = load_state(interface)
        if data.get('task_id') != task or data.get('boot_id') != boot_id() or data['stage'] != 'queued':
            raise ValueError('Identification request is no longer current')
        snapshot = data['snapshot']
        led = validate_led(interface, snapshot)
        zeros = dict.fromkeys(snapshot['settings'], 0)
        started = time.monotonic()
        deadline = started + data['seconds']
        half_period = data['period_ms'] / 2000
        reason = None
        try:
            while not stop_event.is_set() and time.monotonic() < deadline:
                on = dict(zeros)
                on[current_speed(interface, zeros)] = 1
                set_rules(led, on)
                if data['stage'] == 'queued':
                    data.update(stage='running', started_monotonic=started, pid=os.getpid())
                    save(state_file(interface), data)
                if stop_event.wait(min(half_period, max(0, deadline - time.monotonic()))):
                    break
                set_rules(led, zeros)
                stop_event.wait(min(half_period, max(0, deadline - time.monotonic())))
        except (OSError, ValueError) as error:
            reason = str(error)
        finally:
            data.update(stage='restoring')
            try:
                restore(interface, snapshot)
                data.update(stage='failed' if reason else 'stopped' if stop_event.is_set() else 'completed', restored=True)
            except (OSError, ValueError) as error:
                data.update(stage='restore-failed', restored=False)
                reason = (reason + '; ' if reason else '') + 'Restore failed: ' + str(error)
            if reason:
                data['error'] = reason
            data['elapsed_seconds'] = round(time.monotonic() - started, 3)
            save(state_file(interface), data)
        return data


def main(argv=None):
    parser = argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG'), description='规律闪烁网口绿灯以定位设备，结束后恢复原灯态；需要网口链路。')
    parser.add_argument('--interface', help='网口名称；默认使用板卡配置或唯一物理网口。')
    parser.add_argument('--seconds', type=int, default=30, help='持续时间，1–600 秒，默认 30。')
    parser.add_argument('--period-ms', type=int, default=1000, help='亮灭周期，200–5000 毫秒，默认 1000。')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--status', action='store_true', help='查询定位灯任务状态。')
    actions.add_argument('--stop', action='store_true', help='停止闪烁并恢复原来的链路／活动灯规则。')
    actions.add_argument('--worker', help=argparse.SUPPRESS)
    parser.add_argument('--json', action='store_true', help='输出 protocol 1 JSON。')
    args = parser.parse_args(argv)
    try:
        interface = interface_name(args.interface)
        if not args.status and os.geteuid() != 0:
            raise ValueError('需要 root 权限。')
        if args.worker:
            stopped = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stopped.set())
            signal.signal(signal.SIGINT, lambda *_: stopped.set())
            data = flash(interface, args.worker, stopped)
            return 0 if data.get('restored') and not data.get('error') else 2
        data = status(interface) if args.status else stop(interface) if args.stop else start(interface, args.seconds, args.period_ms)
        print(json.dumps({'protocol': 1, 'ok': True, 'data': data} if args.json else data,
                         ensure_ascii=False, indent=None if args.json else 2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if args.json:
            print(json.dumps({'protocol': 1, 'ok': False, 'error': {'code': 'identify', 'message': str(error)}}, ensure_ascii=False))
        else:
            print('网口定位失败：' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
