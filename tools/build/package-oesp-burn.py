#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Export a signed OESP raw image as a USB Burning Tool 2.2.4 container."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from framework.amlogic_boot import validate_vendor_environment

REFERENCE_SHA256 = '1052743ccde21765d42afa022bbe987c9571e8e19de4a937e3145d6d9e7811e2'
REFERENCE_URL = 'https://github.com/ophub/kernel/releases/download/tools/wxy-oes-plus-ubuntu-s922x-usbboot.tar.xz'
PACKER_SHA256 = 'e76475dd305d90a1f6fee18db637c68f3084d920e9691fd17f44fde1822f6766'
PACKER_URL = 'https://raw.githubusercontent.com/hardkernel/buildroot/master/package/aml_img_packer_new/src/aml_image_v2_packer_new'
FIP_SHA256 = '2e28a6d05bcda412195d4991beba403cc6fe6859d7770ebead91a34990c48fde'
MINIMAL_RESERVED_SHA256 = 'e8946f1e4fafb0ef39b3452a560630a8384aa9e5bd243b950a325f59cce5d717'
SOURCE_ITEMS = {
    ('USB', 'DDR'): ('DDR.USB', '62eea16c67b0d16ba8310845a0edd3b2f71b1ef5019c577c2a5b94372f03cd7c'),
    ('USB', 'DDR_ENC'): ('DDR_ENC.USB', FIP_SHA256),
    ('USB', 'UBOOT'): ('UBOOT.USB', '62eea16c67b0d16ba8310845a0edd3b2f71b1ef5019c577c2a5b94372f03cd7c'),
    ('USB', 'UBOOT_ENC'): ('UBOOT_ENC.USB', FIP_SHA256),
    ('dtb', 'meson1'): ('meson1.dtb', 'fa94b34c3e678eb5ee5c9496dfa2ce5523f5ef962646ebd951a8b9f79e4b2ccf'),
    ('dtb', 'meson1_ENC'): ('meson1_ENC.dtb', 'e9945c1207900a608a942d88dea6d15a9bb84b0e05ea258a53e9d1d1fd560eb9'),
    ('conf', 'platform'): ('platform.conf', '1981261f893f6cf6c6c47ef5741b4d979ba926b126f238ca21e5ecd4c5467033'),
}
HEAD = struct.Struct('<IIIQII36s')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def region_digest(path, offset, size):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as stream:
        stream.seek(offset)
        while size:
            data = stream.read(min(size, 4 * 1024**2))
            require(bool(data), 'Truncated image item')
            hasher.update(data)
            size -= len(data)
    return hasher.hexdigest()


def native_items(path):
    size = Path(path).stat().st_size
    with Path(path).open('rb') as stream:
        head = stream.read(HEAD.size)
        require(len(head) == HEAD.size, 'Truncated native header')
        crc, version, magic, declared, alignment, count, reserve = HEAD.unpack(head)
        require(magic == 0x27b51956 and version in (1, 2), 'Unsupported native container')
        require(declared == size and 1 <= count <= 1024, 'Native image size or item count differs')
        names = 32 if version == 1 else 256
        record = struct.Struct(f'<IIQQQ{names}s{names}sIHH24s')
        end = HEAD.size + record.size * count
        require(end <= size, 'Truncated item table')
        rows = {}
        for _ in range(count):
            row = stream.read(record.size)
            require(len(row) == record.size, 'Truncated native item')
            item_id, kind, cursor, offset, length, main, sub, verify, backup, backup_id, reserve = record.unpack(row)
            role = (main.split(b'\0', 1)[0].decode('ascii'), sub.split(b'\0', 1)[0].decode('ascii'))
            require(role not in rows and end <= offset <= size and 0 < length <= size - offset,
                    'Duplicate or out-of-range native item')
            rows[role] = {'offset': offset, 'size': length, 'file_type': kind}
    return rows


def prepare_loader(reference, output):
    require(digest(reference) == REFERENCE_SHA256, 'Reference OESP package hash differs')
    rows = native_items(reference)
    files = []
    with Path(reference).open('rb') as stream:
        for role, (name, expected) in SOURCE_ITEMS.items():
            require(role in rows and rows[role]['file_type'] == 0, 'Reference loader item is missing')
            row = rows[role]
            require(row['size'] <= 4 * 1024**2, 'Loader item is too large')
            stream.seek(row['offset'])
            data = stream.read(row['size'])
            require(hashlib.sha256(data).hexdigest() == expected, 'Loader item hash differs: ' + name)
            with (output / name).open('xb') as destination:
                destination.write(data)
            files.append({'file': name, 'role': ':'.join(role), 'bytes': len(data), 'sha256': expected})
    lines = ['[LIST_NORMAL]']
    for role, (name, expected) in SOURCE_ITEMS.items():
        lines.append(f'file="{name}" main_type="{role[0]}" sub_type="{role[1]}" file_type="normal"')
    lines.extend(['file="armhpc-full.img" main_type="PARTITION" sub_type="1" file_type="normal"', '[LIST_VERIFY]', ''])
    (output / 'image.cfg').write_text('\n'.join(lines), encoding='ascii')
    return files


def validate_raw(image, manifest):
    for key, expected in {'kind': 'image', 'protocol': 1, 'board': 'wxy-oesp-v22', 'architecture': 'arm64',
                          'layout': 'oesp-mbr-squashfs-ab-v1', 'anchor_abi': 'armhpc-anchor-amlogic-mbr-v1'}.items():
        require(manifest.get(key) == expected, 'Signed image identity differs: ' + key)
    expected = manifest['image']
    size = Path(image).stat().st_size
    require(size == expected['bytes'] and size % 512 == 0 and digest(image) == expected['sha256'],
            'Signed raw image size or SHA256 differs')
    with Path(image).open('rb') as stream:
        mbr = stream.read(512)
        require(mbr[-2:] == b'\x55\xaa', 'MBR signature is missing')
        geometry = []
        for i in range(4):
            entry = mbr[446 + i * 16:462 + i * 16]
            start, sectors = struct.unpack_from('<II', entry, 8)
            require(sectors > 0 and (start + sectors) * 512 <= size, 'MBR partition lies outside image')
            geometry.append((entry[4], start, sectors))
        require(geometry[:3] == [(0x0c, 204800, 1048576), (0x83, 1253376, 1048576),
                                 (0x83, 2301952, 1048576)], 'BOOT or Rescue A/B geometry differs')
        require(geometry[3][0] == 0x83 and geometry[3][1] == 3350528, 'Shared partition geometry differs')
        stream.seek(512)
        require(hashlib.sha256(stream.read(2097152)).hexdigest() == FIP_SHA256, 'Permanent FIP differs')
        stream.seek(36 * 1024**2)
        require(hashlib.sha256(stream.read(64 * 1024**2)).hexdigest() == MINIMAL_RESERVED_SHA256,
                'Minimal EPT must exclude the vendor Android partition DTB')
        environments = []
        for offset in (4194304, 4259840):
            stream.seek(offset)
            env = stream.read(65536)
            validate_vendor_environment(env)
            environments.append(env)
        require(environments[0] == environments[1], 'Vendor environment copies differ')
    return expected


def verify_export(container, raw):
    rows = native_items(container)
    require(set(rows) == set(SOURCE_ITEMS) | {('PARTITION', '1')}, 'Export contains unexpected write or key items')
    for role, (_, expected) in SOURCE_ITEMS.items():
        row = rows[role]
        require(row['file_type'] == 0 and region_digest(container, row['offset'], row['size']) == expected,
                'Export loader hash differs: ' + ':'.join(role))
    payload = rows[('PARTITION', '1')]
    require(payload['file_type'] == 0 and payload['size'] == raw['bytes'] and
            region_digest(container, payload['offset'], payload['size']) == raw['sha256'],
            'Embedded whole-device image differs')
    return {':'.join(role): row for role, row in rows.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('image', 'manifest', 'trust', 'ota-source', 'reference-image', 'packer', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--name', required=True)
    args = parser.parse_args()
    require(Path(args.name).name == args.name and args.name.endswith('.img'), 'Use an IMG basename')
    require(not args.output.exists(), 'Use a new export directory; existing packages are immutable')
    require(digest(args.packer) == PACKER_SHA256, 'Native packer hash differs')
    sys.path.insert(0, str(args.ota_source / 'src'))
    from armhpc_ota.metadata import verify
    manifest = verify(args.manifest.read_bytes(), args.trust)
    raw = validate_raw(args.image, manifest)
    args.output.mkdir(parents=True)
    files = prepare_loader(args.reference_image, args.output)
    os.symlink(args.image.resolve(), args.output / 'armhpc-full.img')
    container = args.output / args.name
    with (args.output / 'pack.log').open('w', encoding='utf-8') as log:
        subprocess.run([str(args.packer.resolve()), '-r', str(args.output / 'image.cfg'), str(args.output), str(container)],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
        subprocess.run([str(args.packer.resolve()), '-c', str(container)], stdout=log, stderr=subprocess.STDOUT, check=True)
    items = verify_export(container, raw)
    print('Native container and embedded signed raw image verified; compressing', flush=True)
    subprocess.run(['xz', '-T4', '-6', '-k', str(container)], check=True)
    compressed = container.with_suffix(container.suffix + '.xz')
    subprocess.run(['xz', '-t', str(compressed)], check=True)
    result = {'image': compressed.name, 'bytes': compressed.stat().st_size, 'sha256': digest(compressed),
              'raw_burn_image_bytes': container.stat().st_size, 'raw_burn_sha256': digest(container),
              'board': manifest['board'], 'system_release': manifest['release'], 'embedded_raw_image': raw,
              'format': 'Amlogic USB Burning Tool v2 native container', 'usb_tool_version': '2.2.4',
              'reference_image': {'sha256': REFERENCE_SHA256, 'url': REFERENCE_URL},
              'packer': {'sha256': PACKER_SHA256, 'url': PACKER_URL}, 'loader_inputs': files, 'items': items,
              'permanent_boot': {'strategy': 'OESP user-area FIP at sector 1 with generated autoscr environment',
                                 'fip_offset_bytes': 512, 'environment_offsets': [4194304, 4259840],
                                 'boot0_boot1_written': False, 'save_setting_requested': False},
              'checks': {'signature': True, 'raw_sha256': True, 'native_crc': True, 'embedded_raw_sha256': True,
                         'separate_plain_and_encrypted_roles': True, 'vendor_environment_crc': True,
                         'vendor_autoscr_entry': True, 'environment_copies_match': True, 'xz_integrity': True},
              'hardware_verified': False, 'usb_burning_tool_import_checked': False, 'device_flash_checked': False}
    (args.output / 'BURN-EXPORT.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    (args.output / (compressed.name + '.sha256')).write_text(result['sha256'] + '  ' + compressed.name + '\n', encoding='ascii')
    print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    main()
