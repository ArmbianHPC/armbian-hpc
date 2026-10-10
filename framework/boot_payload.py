# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Validate a raw initramfs for a loader that does not strip legacy headers."""
import hashlib
from pathlib import Path
import re

FORMATS = (
    (b'\x28\xb5\x2f\xfd', 'zstd', 'CONFIG_RD_ZSTD'),
    (b'\xfd7zXZ\x00', 'xz', 'CONFIG_RD_XZ'),
    (b'\x1f\x8b', 'gzip', 'CONFIG_RD_GZIP'),
    (b'\x02\x21\x4c\x18', 'lz4', 'CONFIG_RD_LZ4'),
    (b'070701', 'cpio', None),
    (b'070702', 'cpio', None),
)


def raw_initramfs_format(header):
    if header.startswith(b'\x27\x05\x19\x56'):
        raise ValueError('Legacy uImage ramdisk headers are unsupported by this loader; supply a raw initramfs.')
    for magic, name, requirement in FORMATS:
        if header.startswith(magic):
            return name, requirement
    raise ValueError('Unrecognized raw initramfs format.')


def verify_raw_initramfs(initramfs, kernel_config):
    initramfs, kernel_config = Path(initramfs), Path(kernel_config)
    with initramfs.open('rb') as stream:
        name, requirement = raw_initramfs_format(stream.read(8))
        stream.seek(0)
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    config = kernel_config.read_text()
    for option in ('CONFIG_BLK_DEV_INITRD', requirement):
        if option and not re.search(r'^'+re.escape(option)+r'=y$', config, re.MULTILINE):
            raise ValueError('The kernel requires built-in '+option+' for this initramfs.')
    return {'format': 'raw', 'compression': name, 'size_bytes': initramfs.stat().st_size, 'sha256': digest}
