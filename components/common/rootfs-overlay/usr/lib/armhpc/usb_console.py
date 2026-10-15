# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Optional board-configured CDC ACM console; normal login authentication is retained."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

CONFIG = Path('/etc/armhpc/usb-console.json')
GADGETS = Path('/sys/kernel/config/usb_gadget')
UDCS = Path('/sys/class/udc')
NAME = 'armhpc-console'
ROLES = Path('/sys/class/usb_role')
ROLE_STATE = Path('/run/armhpc/usb-console-role.json')


def read(path):
    try:
        return Path(path).read_text().strip()
    except (FileNotFoundError, OSError):
        return None


def checked(config):
    if not isinstance(config, dict) or config.get('enabled') is not True:
        raise ValueError('This board has not enabled its USB console adapter')
    for key in ('vendor_id', 'product_id'):
        if not re.fullmatch(r'0x[0-9a-fA-F]{4}', config.get(key, '')):
            raise ValueError('Invalid USB descriptor ID: ' + key)
    if not re.fullmatch(r'[0-9a-f]{8}', config.get('controller_address', '')):
        raise ValueError('Invalid USB controller address')
    return config


def controller(config, root=UDCS):
    matches = [path.name for path in root.glob('*')
               if path.name.split('.', 1)[0] == config['controller_address']]
    if len(matches) != 1:
        raise ValueError('The board USB device controller is absent or ambiguous; check the matching peripheral DTB')
    return matches[0]


def serial_number():
    # The same eMMC-derived serial identifies both environments without exposing the CID.
    cids = [read(path) for path in Path('/sys/bus/mmc/devices').glob('*/cid')
            if read(path.parent / 'type') == 'MMC']
    if len(cids) == 1 and cids[0]:
        return hashlib.sha256(('armhpc-usb:' + cids[0]).encode()).hexdigest()[:24]
    machine = read('/etc/machine-id')
    if not machine:
        raise ValueError('USB serial identity is not initialized yet')
    return hashlib.sha256(('armhpc-usb:' + machine).encode()).hexdigest()[:24]


def role_path(config, root=ROLES):
    address=config.get('role_controller_address')
    if address is None:
        return None
    if not re.fullmatch(r'[0-9a-f]{8}',address):
        raise ValueError('Invalid USB role controller address')
    matches=[path/'role' for path in root.iterdir() if path.name.startswith(address+'.') and (path/'role').is_file()]
    if len(matches)!=1:
        raise ValueError('USB role controller is absent or ambiguous')
    return matches[0]


def acquire_device_role(config, root=ROLES, network=Path('/sys/class/net'), state=ROLE_STATE):
    role=role_path(config,root)
    if role is None:
        return
    port=config.get('otg_host_port')
    if port not in (1,2):
        raise ValueError('Unknown OTG host port')
    for interface in config.get('protected_usb_interfaces',[]):
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}',interface):
            raise ValueError('Invalid protected USB interface')
        device=network/interface/'device'
        if device.exists() and re.search(r'/\d+-'+str(port)+r'(?:/|:)',str(device.resolve())):
            raise ValueError('OTG port is occupied by the protected network interface '+interface)
    previous=read(role)
    if previous not in ('host','device','none'):
        raise ValueError('Unknown USB role state')
    state=Path(state)
    if not state.exists():
        state.parent.mkdir(parents=True,exist_ok=True)
        state.write_text(json.dumps({'role':role.parent.name,'previous':previous})+'\n')
        state.chmod(0o600)
    if previous!='device':
        role.write_text('device\n')
        if read(role)!='device':
            raise ValueError('USB device role did not take effect')


def restore_role(config, root=ROLES, state=ROLE_STATE):
    state=Path(state)
    if not state.is_file():
        return
    saved=json.loads(state.read_text())
    role=role_path(config,root)
    if role is None or saved.get('role')!=role.parent.name or saved.get('previous') not in ('host','device','none'):
        raise ValueError('USB role restoration identity differs')
    role.write_text(saved['previous']+'\n')
    state.unlink()


def configure(config, serial, gadgets=GADGETS, udcs=UDCS):
    checked(config)
    udc = controller(config, udcs)
    gadget = gadgets / NAME
    if gadget.is_symlink():
        raise ValueError('The owned USB gadget directory is a symlink')
    gadget.mkdir(exist_ok=True)
    bound = read(gadget / 'UDC')
    if bound:
        if bound != udc or read(gadget / 'strings/0x409/product') != 'ARMHPC Console':
            raise ValueError('An unexpected USB gadget already owns this configuration')
        return udc
    for attribute, value in {'idVendor': config['vendor_id'], 'idProduct': config['product_id'],
                             'bcdUSB': '0x0200', 'bcdDevice': '0x0101', 'bDeviceClass': '0x02',
                             'bDeviceSubClass': '0x02', 'bDeviceProtocol': '0x00'}.items():
        (gadget / attribute).write_text(value + '\n')
    strings = gadget / 'strings/0x409'
    strings.mkdir(parents=True, exist_ok=True)
    for attribute, value in {'serialnumber': serial, 'manufacturer': 'ARMHPC', 'product': 'ARMHPC Console'}.items():
        (strings / attribute).write_text(value + '\n')
    configuration = gadget / 'configs/c.1'
    (configuration / 'strings/0x409').mkdir(parents=True, exist_ok=True)
    (configuration / 'strings/0x409/configuration').write_text('Authenticated USB console\n')
    # The OECT remains powered by its original supply.
    (configuration / 'bmAttributes').write_text('0xC0\n')
    (configuration / 'MaxPower').write_text('2\n')
    function = gadget / 'functions/acm.usb0'
    function.mkdir(parents=True, exist_ok=True)
    link = configuration / 'acm.usb0'
    if link.is_symlink():
        if link.resolve() != function.resolve():
            raise ValueError('Unexpected ACM function link')
    elif link.exists():
        raise ValueError('The ACM function link is occupied')
    else:
        link.symlink_to(function, target_is_directory=True)
    # Never unbind somebody else's gadget to acquire the controller.
    for other in gadgets.glob('*'):
        if other != gadget and read(other / 'UDC') == udc:
            raise ValueError('The board USB controller is already used by another gadget')
    (gadget / 'UDC').write_text(udc + '\n')
    return udc


def status(config_path=CONFIG, gadgets=GADGETS, udcs=UDCS,
           tty=Path("/dev/ttyGS0"), pstore=Path("/sys/fs/pstore")):
    config = json.loads(config_path.read_text()) if config_path.is_file() else {'enabled': False}
    bound = read(gadgets / NAME / 'UDC')
    controllers = [{'name': path.name, 'state': read(path / 'state'),
                    'current_speed': read(path / 'current_speed')}
                   for path in udcs.glob('*')]
    active = next((item for item in controllers if item['name'] == bound), None)
    enumerated = bool(active and active['state'] == 'configured')
    tty_present = tty.exists()
    if not config.get('enabled'):
        diagnosis = 'disabled'
    elif not bound:
        diagnosis = 'gadget_not_bound'
    elif not tty_present:
        diagnosis = 'acm_tty_missing'
    elif not enumerated:
        diagnosis = 'waiting_for_host_enumeration; ttyGS0 alone does not prove a working COM port'
    else:
        diagnosis = 'host_configured; use the existing administrator login'
    return {'enabled': config.get('enabled') is True, 'gadget': NAME,
            'bound_controller': bound, 'tty': '/dev/ttyGS0', 'tty_present': tty_present,
            'enumerated': enumerated, 'diagnosis': diagnosis,
            'physical_port': config.get('physical_port'), 'controllers': controllers,
            'pstore_files': [path.name for path in pstore.glob('*')],
            'log_scope': 'Linux journal from this boot; DDR/SPL/U-Boot are not captured by the Linux gadget'}


def start():
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise ValueError('USB console setup requires Linux root')
    config = checked(json.loads(CONFIG.read_text()))
    subprocess.run(['modprobe', 'libcomposite'], check=True)
    if subprocess.run(['mountpoint', '-q', '/sys/kernel/config']).returncode != 0:
        subprocess.run(['mount', '-t', 'configfs', 'none', '/sys/kernel/config'], check=True)
    deadline = time.monotonic() + 25
    while True:
        try:
            controller(config)
            break
        except ValueError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)
    udc=controller(config)
    for gadget in GADGETS.iterdir():
        if gadget.name!=NAME and read(gadget/'UDC')==udc:
            raise ValueError('The board USB controller is already used by another gadget')
    acquire_device_role(config)
    try:
        configure(config, serial_number())
    except Exception:
        restore_role(config)
        raise
    if not Path('/dev/ttyGS0').exists():
        subprocess.run(['udevadm', 'settle', '--timeout=5'], check=True)
    if not Path('/dev/ttyGS0').exists():
        raise ValueError('ACM bound but ttyGS0 is missing')
    subprocess.run(['systemctl','start','--no-block','serial-getty@ttyGS0.service'],check=True)
    return status()


def stop(gadgets=GADGETS):
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise ValueError('USB console setup requires Linux root')
    gadget = gadgets / NAME
    if gadget.is_symlink():
        raise ValueError('The owned USB gadget directory is a symlink')
    if (gadget / 'UDC').exists():
        if read(gadget / 'strings/0x409/product') != 'ARMHPC Console':
            raise ValueError('Unexpected gadget ownership')
        (gadget / 'UDC').write_text('\n')
    config=json.loads(CONFIG.read_text()) if CONFIG.is_file() else {}
    restore_role(config)
    return {'bound_controller': None, 'gadget': NAME}


def main(argv=None):
    parser = argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG', 'armhpc usb-console'), description=__doc__)
    parser.add_argument('action', nargs='?', default='status', choices=('status', 'start', 'stop', 'logs'))
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--previous', action='store_true', help='Show the previous persisted Linux boot journal')
    args = parser.parse_args(argv)
    if args.previous and args.action != 'logs':
        parser.error('--previous requires logs')
    try:
        if args.action == 'logs':
            if args.json:
                parser.error('logs streams journal text; use status --json for structured diagnostics')
            return subprocess.run(['journalctl', '-k', '-b', '-1' if args.previous else '0',
                                   '--no-pager', '-o', 'short-monotonic']).returncode
        result = {'status': status, 'start': start, 'stop': stop}[args.action]()
        print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print('ARMHPC USB console: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
