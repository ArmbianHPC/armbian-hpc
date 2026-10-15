# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import hashlib
import struct
import pytest
from framework.amlogic_image import inspect


def fixture(tmp_path):
    board = {'boot_start': 8, 'boot_sectors': 8, 'rescue_a_start': 16, 'rescue_a_sectors': 8,
             'rescue_b_start': 24, 'shared_start': 32, 'protected_prefix_bytes': 4096}
    raw = bytearray(40 * 512)
    struct.pack_into('<I', raw, 440, 0x12345678)
    for i, (start, count, kind) in enumerate([(8, 8, 0x0c), (16, 8, 0x83), (24, 8, 0x83), (32, 8, 0x83)]):
        raw[450 + 16 * i] = kind
        struct.pack_into('<II', raw, 454 + 16 * i, start, count)
    raw[510:512] = b'\x55\xaa'
    raw[8 * 512 + 82:8 * 512 + 90] = b'FAT32   '
    raw[8 * 512 + 510:8 * 512 + 512] = b'\x55\xaa'
    raw[16 * 512:16 * 512 + 4] = raw[24 * 512:24 * 512 + 4] = b'hsqs'
    raw[32 * 512 + 1080:32 * 512 + 1082] = b'\x53\xef'
    path = tmp_path / 'image.img'
    path.write_bytes(raw)
    return path, board


def test_mbr_roles_and_superblocks(tmp_path):
    path, board = fixture(tmp_path)
    report = inspect(path, board, [])
    assert report['partitions'][2]['partuuid'] == '12345678-03'
    assert report['raw_gaps_zero'] is True


@pytest.mark.parametrize('offset,value', [(440, b'\0' * 4), (450, b'\x83'), (2048, b'x'), (8 * 512 + 82, b'EXT4')])
def test_bad_signature_geometry_gap_or_boot_filesystem_are_refused(tmp_path, offset, value):
    path, board = fixture(tmp_path)
    with path.open('r+b') as stream:
        stream.seek(offset)
        stream.write(value)
    with pytest.raises(ValueError):
        inspect(path, board, [])


def test_component_mismatch_or_overlap_is_refused(tmp_path):
    path, board = fixture(tmp_path)
    component = {'offset_bytes': 512, 'size_bytes': 512, 'sha256': hashlib.sha256(bytes(512)).hexdigest()}
    inspect(path, board, [component])
    with pytest.raises(ValueError):
        inspect(path, board, [component, component])
