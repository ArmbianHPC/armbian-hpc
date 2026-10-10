# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Configure RTL8211F LEDs through PHYLIB/netdev hardware offload."""
import argparse
import errno
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import time

POLICY = Path('/etc/armhpc/phy-led.json')
STATUS = Path('/run/armhpc/phy-led-status.json')


def discover(led_root, phy_node):
    found = {}
    for led in Path(led_root).glob('*'):
        node = led/'of_node'
        if not node.exists():
            # LED device_set_node can run after device_add, so Linux 6.12 may
            # their parent PHY through device/of_node and a mandatory lan-N name.
            parent = led/'device/of_node'
            match = re.search(r':lan-([0-2])$', led.name)
            if parent.exists() and parent.resolve() == Path(phy_node).resolve() and match:
                index = int(match.group(1))
                if index in found:
                    raise ValueError('Duplicate PHY LED index')
                found[index] = led
            continue
        node = node.resolve()
        if node.parent.parent != Path(phy_node).resolve():
            continue
        raw = (node/'reg').read_bytes()
        if len(raw) != 4:
            continue
        index = struct.unpack('>I', raw)[0]
        if index in found:
            raise ValueError('Duplicate PHY LED index')
        found[index] = led
    return found


def settings(role):
    if role not in ('link', 'activity'):
        raise ValueError('Unknown PHY LED role')
    values = dict.fromkeys(('link', 'link_2500', 'link_5000', 'link_10000',
                           'half_duplex', 'full_duplex', 'rx_err', 'tx_err'), 0)
    values.update(dict.fromkeys(('link_10', 'link_100', 'link_1000'), int(role == 'link')))
    values.update(rx=int(role == 'activity'), tx=int(role == 'activity'))
    return values


def configure(led, interface, role):
    (led/'trigger').write_text('netdev\n')
    (led/'device_name').write_text(interface+'\n')
    desired = settings(role)
    for name, value in desired.items():
        path = led/name
        if not path.exists():
            if name in ('link_10', 'link_100', 'link_1000', 'rx', 'tx'):
                raise ValueError('Missing PHY netdev trigger setting: '+name)
            continue
        try:
            path.write_text(str(value)+'\n')
        except OSError as error:
            # RTL8211F requires RX and TX together. The first half of a paired
            # update can return EOPNOTSUPP after updating the trigger's mode.
            if error.errno != errno.EOPNOTSUPP:
                raise
    for name, value in desired.items():
        path = led/name
        if path.exists() and path.read_text().strip() != str(value):
            raise ValueError('PHY LED setting did not match: '+name)
    if (led/'offloaded').read_text().strip() != '1':
        raise ValueError('PHY LED hardware offload was not enabled')
    return {'led': led.name, 'role': role, 'offloaded': True,
            'settings': {k: v for k, v in desired.items() if (led/k).exists()}}


def main():
    parser = argparse.ArgumentParser(description='RTL8211F 网口灯：link 常亮，RX/TX 活动闪烁。')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--link-index', type=int, action='append', help='覆盖链路灯输出脚编号，可重复指定 0、1、2。')
    parser.add_argument('--activity-index', type=int, help='覆盖活动灯输出脚编号。')
    args = parser.parse_args()
    if args.status:
        print(STATUS.read_text() if STATUS.is_file() else '{}')
        return
    if os.geteuid() != 0:
        parser.error('需要 root 权限。')
    if not POLICY.is_file():
        return
    policy = json.loads(POLICY.read_text())
    interface = policy['interface']
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', interface):
        raise ValueError('Invalid network interface')
    links = args.link_index if args.link_index is not None else policy['link_indexes']
    activity = args.activity_index if args.activity_index is not None else policy['activity_index']
    if not links or len(set(links)) != len(links) or any(i not in (0, 1, 2) for i in [*links, activity]) or activity in links:
        raise ValueError('PHY LED indexes must be distinct and between 0 and 2')
    phy = Path('/sys/class/net')/interface/'phydev'
    report = {'configured': False, 'interface': interface, 'link_indexes': links, 'activity_index': activity}
    try:
        if not phy.is_dir():
            report['reason'] = 'No physical PHY on this interface'
            return
        phy_id = int((phy/'phy_id').read_text().strip(), 16)
        if phy_id != int(policy['phy_id'], 16):
            report['reason'] = 'Unsupported PHY id '+hex(phy_id)
            return
        subprocess.run(['modprobe', 'ledtrig-netdev'], check=True, stdout=subprocess.DEVNULL)
        nodes = discover(Path('/sys/class/leds'), phy/'of_node')
        needed = set(links+[activity])
        for attempt in range(10):
            if needed.issubset(nodes):
                break
            time.sleep(1)
            nodes = discover(Path('/sys/class/leds'), phy/'of_node')
        if not needed.issubset(nodes):
            raise ValueError('PHY LED classes were not identified on this PHY; check the DTB and sysfs parent mapping')
        report['outputs'] = [configure(nodes[index], interface, 'link') for index in links]
        report['outputs'].append(configure(nodes[activity], interface, 'activity'))
        report['configured'] = True
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report['reason'] = str(error)
    finally:
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
