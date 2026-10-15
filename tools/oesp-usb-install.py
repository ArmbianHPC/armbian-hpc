#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Back up and install an OESP raw image through the Amlogic USB transport."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import time

BOARD = 'wxy-oesp-v22'
LAYOUT = 'oesp-mbr-squashfs-ab-v1'
PYAMLBOOT_COMMIT = 'd7806acc4f0a9a9d89b4e32a5c9a0ae03f7d11bf'
LOADER_SHA256 = '2e28a6d05bcda412195d4991beba403cc6fe6859d7770ebead91a34990c48fde'
LOADER_FILES = {'DDR.USB': LOADER_SHA256,
    'image.cfg': 'a019beaedc769161472332d8747d4f52f398d3c4493a6b2bd75367b5b1f57b83',
    'platform.conf': '1981261f893f6cf6c6c47ef5741b4d979ba926b126f238ca21e5ecd4c5467033',
    '_aml_dtb.PARTITION': 'e9945c1207900a608a942d88dea6d15a9bb84b0e05ea258a53e9d1d1fd560eb9'}
BUFFER_ADDRESS = 0x20000000
CHUNK_BYTES = 4 * 1024**2


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != 'nt':
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class Transport:
    """Only generated MMC and RAM commands are accepted by this adapter."""
    def __init__(self, device):
        self.device = device

    def command(self, command, timeout=30000):
        require(len(command.encode('ascii')) < 127, 'USB command exceeds protocol limit')
        self.device.bulkCmd(command, read_status=False, timeout=timeout)
        deadline = time.monotonic() + timeout / 1000
        while True:
            result = bytes(self.device.bulkCmdStat(timeout=timeout)).rstrip(b'\0')
            if result == b'success':
                return
            require(result.startswith(b'Continue:34'),
                    'Vendor U-Boot rejected a required MMC/RAM command; no automatic fallback is allowed')
            require(time.monotonic() < deadline, 'USB command timed out; result must be checked before continuing')
            time.sleep(0.1)

    def select(self, area):
        require(area in (0, 1, 2), 'Invalid eMMC area')
        self.command(f'mmc dev 1 {area:x}')

    def register(self, name, offset):
        require((name == 'cid' and 0 <= offset < 4) or
                (name == 'extcsd' and offset in (212, 213, 214, 215, 226)), 'Invalid register')
        self.command(f'mmc reg read {name} {offset:d} armhpc_reg')
        self.command(f'mw.l {BUFFER_ADDRESS:x} ${{armhpc_reg}} 1')
        return struct.unpack('<I', bytes(self.device.readSimpleMemory(BUFFER_ADDRESS, 4)))[0]

    def identity(self):
        self.select(0)
        cid = b''.join(self.register('cid', n).to_bytes(4, 'big') for n in range(4))
        sectors = sum(self.register('extcsd', 212 + n) << (8 * n) for n in range(4))
        boot_bytes = self.register('extcsd', 226) * 128 * 1024
        require(cid[0] == 0x15 and 7 * 1024**3 <= sectors * 512 <= 9 * 1024**3,
                'Expected Samsung 8 GB eMMC was not identified')
        require(0 < boot_bytes <= 16 * 1024**2, 'Invalid hardware boot area capacity')
        return {'board': BOARD, 'cid_sha256': hashlib.sha256(cid).hexdigest(),
                'device_bytes': sectors * 512, 'boot_bytes': boot_bytes}

    def read(self, offset, length):
        require(offset >= 0 and offset % 512 == 0 and 0 < length <= CHUNK_BYTES and length % 512 == 0,
                'Invalid eMMC read range')
        self.command(f'mmc read {BUFFER_ADDRESS:x} {offset // 512:x} {length // 512:x}')
        # Convert each pyusb array explicitly; older pyamlboot releases concatenate
        # array responses to bytes in their large-read helper.
        import usb.util
        interface = self.device.dev.get_active_configuration()[(0, 0)]
        endpoint = usb.util.find_descriptor(interface, custom_match=lambda ep:
            usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN)
        require(endpoint is not None, 'USB bulk input endpoint is missing')
        block = 16384 if length % 16384 == 0 else 512
        self.device.dev.ctrl_transfer(bmRequestType=0x40, bRequest=0x12,
            wValue=block, wIndex=length // block,
            data_or_wLength=struct.pack('<IIII', BUFFER_ADDRESS, length, 0, 0))
        result = bytearray()
        while len(result) < length:
            item = bytes(endpoint.read(block, 5000))
            require(len(item) == block, 'Short USB bulk read')
            result.extend(item)
        data = bytes(result)
        require(len(data) == length, 'Short USB read')
        return data

    def write(self, offset, data):
        require(offset >= 0 and offset % 512 == 0 and 0 < len(data) <= CHUNK_BYTES and len(data) % 512 == 0,
                'Invalid eMMC write range')
        # Smaller transfers also cover the final partial chunk without extra sectors.
        for index in range(0, len(data), 1024**2):
            part = data[index:index + 1024**2]
            self.device.writeLargeMemory(BUFFER_ADDRESS + index, part, 512)
        self.command(f'mmc write {BUFFER_ADDRESS:x} {offset // 512:x} {len(data) // 512:x}')


def backup(transport, directory):
    identity = transport.identity()
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    require(not directory.is_symlink(), 'Backup directory cannot be a link')
    state_path = directory / 'backup.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        'protocol': 1, 'kind': 'oesp-usb-backup', **identity, 'files': [], 'complete': False}
    require(all(state.get(key) == value for key, value in identity.items()), 'Backup belongs to another eMMC')
    remaining = identity['device_bytes'] + 2 * identity['boot_bytes']
    remaining -= sum(row['bytes'] for row in state['files'])
    require(shutil.disk_usage(directory).free >= remaining + 64 * 1024**2, 'Insufficient computer backup space')
    for area, name, total in ((0, 'user.img', identity['device_bytes']),
                              (1, 'boot0.img', identity['boot_bytes']), (2, 'boot1.img', identity['boot_bytes'])):
        transport.select(area)
        destination = directory / name
        old = next((row for row in state['files'] if row['file'] == name), None)
        if old:
            require(destination.is_file() and destination.stat().st_size == total and digest(destination) == old['sha256'],
                    'Existing backup failed verification')
            continue
        partial = destination.with_suffix('.img.partial')
        require(not destination.exists() and not partial.is_symlink(), 'Unregistered backup must be reviewed')
        offset = partial.stat().st_size if partial.exists() else 0
        require(offset <= total and offset % CHUNK_BYTES == 0, 'Partial backup has an invalid offset')
        # Check every retained chunk against the same physical area before resuming.
        if offset:
            with partial.open('rb') as stream:
                for start in range(0, offset, CHUNK_BYTES):
                    require(stream.read(CHUNK_BYTES) == transport.read(start, CHUNK_BYTES), 'Partial backup differs from device')
        with partial.open('ab') as stream:
            while offset < total:
                data = transport.read(offset, min(CHUNK_BYTES, total - offset))
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                offset += len(data)
                print(json.dumps({'operation': 'backup', 'area': name, 'bytes': offset, 'total': total}), flush=True)
        os.replace(partial, destination)
        if os.name != 'nt':
            destination.chmod(0o600)
        state['files'].append({'file': name, 'bytes': total, 'sha256': digest(destination)})
        save(state_path, state)
    transport.select(0)
    state['complete'] = True
    save(state_path, state)
    return {'complete': True, 'backup': str(state_path), 'device_bytes': identity['device_bytes']}


def verify_backup(directory, identity):
    directory = Path(directory)
    state = json.loads((directory / 'backup.json').read_text())
    require(state.get('kind') == 'oesp-usb-backup' and state.get('complete') is True and
            all(state.get(key) == value for key, value in identity.items()), 'Complete matching backup is required')
    expected = {'user.img': identity['device_bytes'], 'boot0.img': identity['boot_bytes'], 'boot1.img': identity['boot_bytes']}
    require(len(state['files']) == 3 and {row['file'] for row in state['files']} == set(expected), 'Incomplete backup inventory')
    for row in state['files']:
        path = directory / row['file']
        require(not path.is_symlink() and path.is_file() and row['bytes'] == expected[row['file']] and
                path.stat().st_size == row['bytes'] and digest(path) == row['sha256'], 'Backup hash or capacity differs')


def write_image(transport, image, expected, directory, identity, execute, yes, state_name):
    image = Path(image)
    require(not image.is_symlink() and image.is_file(), 'Choose a regular uncompressed image')
    with image.open('rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        require(size == expected['bytes'] and 0 < size <= identity['device_bytes'] and size % 512 == 0 and
                hashlib.file_digest(stream, 'sha256').hexdigest() == expected['sha256'], 'Image hash or capacity differs')
        result = {'execute': execute, 'image_bytes': size, 'backup_verified': True, 'board': BOARD}
        if not execute:
            return result
        require(yes, 'Writing requires both --execute and --yes')
        require(transport.identity() == identity, 'eMMC identity changed before writing')
        state_path = Path(directory) / state_name
        state = {'protocol': 1, 'kind': 'oesp-usb-install', **identity,
                 'image_sha256': expected['sha256'], 'image_bytes': size, 'written_bytes': 0, 'stage': 'writing'}
        if state_path.exists():
            old = json.loads(state_path.read_text())
            require(all(old.get(key) == state[key] for key in (*identity, 'image_sha256', 'image_bytes')),
                    'Existing installation belongs to another device or image')
            state['written_bytes'] = old['written_bytes']
        offset = state['written_bytes']
        require(0 <= offset <= size and (offset == size or offset % CHUNK_BYTES == 0), 'Invalid installation checkpoint')
        stream.seek(0)
        for start in range(0, offset, CHUNK_BYTES):
            require(stream.read(min(CHUNK_BYTES, size - start)) == transport.read(start, min(CHUNK_BYTES, size - start)),
                    'Written prefix differs; installation requires review')
        save(state_path, state)
        while offset < size:
            data = stream.read(min(CHUNK_BYTES, size - offset))
            require(data, 'Image was truncated')
            transport.write(offset, data)
            require(transport.read(offset, len(data)) == data, 'USB eMMC readback differs')
            offset += len(data)
            state['written_bytes'] = offset
            save(state_path, state)
            print(json.dumps({'operation': 'install', 'bytes': offset, 'total': size}), flush=True)
        h = hashlib.sha256()
        for start in range(0, size, CHUNK_BYTES):
            h.update(transport.read(start, min(CHUNK_BYTES, size - start)))
        require(h.hexdigest() == expected['sha256'], 'Full image readback differs')
        state['stage'] = 'succeeded'
        save(state_path, state)
        # The verified image already contains its environment and EPT. Do not saveenv.
        return {**result, 'complete': True, 'cold_start_required': True}


def install(transport, image, manifest, trust, directory, execute=False, yes=False):
    from armhpc_ota.metadata import verify
    signed = verify(Path(manifest).read_bytes(), Path(trust))
    require(signed.get('kind') == 'image' and signed.get('board') == BOARD and
            signed.get('architecture') == 'arm64' and signed.get('layout') == LAYOUT, 'Wrong signed image profile')
    identity = transport.identity()
    verify_backup(directory, identity)
    return write_image(transport, image, signed['image'], directory, identity, execute, yes, 'install.json')


def restore(transport, directory, execute=False, yes=False):
    identity = transport.identity()
    verify_backup(directory, identity)
    state = json.loads((Path(directory) / 'backup.json').read_text())
    # Normal installation does not modify hardware boot partitions. Require that
    # they still match before restoring the original user area.
    for area, name in ((1, 'boot0.img'), (2, 'boot1.img')):
        transport.select(area)
        h = hashlib.sha256()
        for offset in range(0, identity['boot_bytes'], CHUNK_BYTES):
            h.update(transport.read(offset, min(CHUNK_BYTES, identity['boot_bytes'] - offset)))
        row = next(item for item in state['files'] if item['file'] == name)
        require(h.hexdigest() == row['sha256'], 'Hardware boot area changed; restore requires separate review')
    transport.select(0)
    expected = next(item for item in state['files'] if item['file'] == 'user.img')
    return write_image(transport, Path(directory) / 'user.img', expected, directory, identity, execute, yes, 'restore.json')


def connect(loader_directory=None):
    import usb.core
    import usb.backend.libusb1
    if os.name != 'nt':
        backend = usb.backend.libusb1.get_backend()
    else:
        try:
            import libusb_package
        except ImportError:
            backend = usb.backend.libusb1.get_backend()
        else:
            backend = usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)
    require(backend is not None, 'libusb backend is unavailable')
    from pyamlboot.pyamlboot import AmlogicSoC
    from pyamlboot.socid import SocId
    devices = list(usb.core.find(find_all=True, idVendor=0x1b8e, idProduct=0xc003, backend=backend))
    require(len(devices) == 1, 'Connect exactly one OESP in Amlogic USB download mode')
    device = AmlogicSoC(timeout=10, usb_backend=backend)
    soc = SocId(device.identify())
    require(not soc.need_password or soc.password_ok, 'USB download requires a vendor password input; no bypass is attempted')
    if soc.stage_minor != SocId.STAGE_MINOR_TPL:
        require(loader_directory, 'BootROM mode requires the pinned OESP loader directory')
        from pyamlboot.amlimage import AmlImagePack
        from pyamlboot.optimus import SharedData, get_burn_steps, do_burn
        from enum import Enum
        from types import SimpleNamespace
        loader = Path(loader_directory)
        for name,expected in LOADER_FILES.items():
            require(not (loader/name).is_symlink() and digest(loader/name)==expected,'OESP loader input hash differs: '+name)
        with (loader / 'image.cfg').open('rb') as config:
            package = AmlImagePack(config, True)
            # Construct the upstream loading sequence, then retain only RAM initialization.
            args = SimpleNamespace(password=None, no_erase_bootloader=True,
                                   wipe=Enum('Wipe', {'no': 0}).no, reset=False)
            steps = get_burn_steps(args, SharedData(), package)
            allowed = {'BurnStepCheckPassword', 'BurnStepBoardIsSecure', 'BurnStepDownloadSPL', 'BurnStepDownloadUboot'}
            selected = [step for step in steps if type(step).__name__ in allowed]
            require(len(selected) == 4, 'Unexpected pyamlboot loader API; use the pinned commit')
            do_burn(selected)
        device = AmlogicSoC(timeout=10, usb_backend=backend)
        require(SocId(device.identify()).stage_minor == SocId.STAGE_MINOR_TPL, 'Vendor U-Boot did not enumerate')
    return Transport(device)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'backup', 'install', 'restore'))
    parser.add_argument('--board', choices=(BOARD,), required=True)
    parser.add_argument('--loader-directory', type=Path)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--image', type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--trust', type=Path)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--yes', action='store_true')
    args = parser.parse_args(argv)
    if args.action != 'status':
        require(args.directory, 'Provide a private backup directory on this computer')
    if args.action == 'install':
        require(args.image and args.manifest and args.trust, 'Provide --image, --manifest and trusted --trust directory')
    transport = connect(args.loader_directory)
    if args.action == 'status':
        result = transport.identity()
    elif args.action == 'backup':
        result = backup(transport, args.directory)
    elif args.action == 'restore':
        result = restore(transport, args.directory, args.execute, args.yes)
    else:
        result = install(transport, args.image, args.manifest, args.trust, args.directory, args.execute, args.yes)
    print(json.dumps({'protocol': 1, 'ok': True, 'data': result}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'protocol': 1, 'ok': False, 'error': {'code': 'usb_install', 'message': str(error)}}), flush=True)
        sys.exit(2)
