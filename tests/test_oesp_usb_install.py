# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCRIPT = Path(__file__).parents[1] / 'tools/oesp-usb-install.py'
spec = importlib.util.spec_from_file_location('oesp_usb_install', SCRIPT)
usb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usb)


class Device:
    def __init__(self):
        self.areas = {0: bytearray(1024), 1: bytearray(512), 2: bytearray(512)}
        self.area = 0
        self.writes = []

    def identity(self):
        return {'board': usb.BOARD, 'cid_sha256': 'a' * 64, 'device_bytes': 1024, 'boot_bytes': 512}

    def select(self, area):
        self.area = area

    def read(self, offset, length):
        return bytes(self.areas[self.area][offset:offset + length])

    def write(self, offset, data):
        assert self.area == 0
        self.writes.append((offset, len(data)))
        self.areas[0][offset:offset + len(data)] = data


def resources(tmp_path, device):
    backup = tmp_path / 'backup'
    usb.backup(device, backup)
    key = Ed25519PrivateKey.generate()
    private = tmp_path / 'private.pem'
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    trust = tmp_path / 'trust'
    trust.mkdir()
    (trust / 'signer.pub').write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    image = tmp_path / 'image.img'
    image.write_bytes(b'x' * 512)
    from armhpc_ota.metadata import sign
    manifest = tmp_path / 'image.json'
    manifest.write_bytes(sign({'kind': 'image', 'board': usb.BOARD, 'architecture': 'arm64', 'layout': usb.LAYOUT,
                              'image': {'bytes': 512, 'sha256': usb.digest(image)}}, private, 'signer'))
    return image, manifest, trust, backup


def test_backup_never_writes_device_and_can_continue(tmp_path):
    device = Device()
    usb.backup(device, tmp_path)
    usb.backup(device, tmp_path)
    assert device.writes == []
    assert json.loads((tmp_path / 'backup.json').read_text())['complete']


def test_plan_and_signed_write_preserve_boot_areas(tmp_path):
    device = Device()
    args = resources(tmp_path, device)
    assert usb.install(device, *args)['execute'] is False
    assert device.writes == []
    assert usb.install(device, *args, execute=True, yes=True)['complete']
    assert device.writes == [(0, 512)]
    assert device.areas[1] == bytearray(512) and device.areas[2] == bytearray(512)


def test_repeated_install_checks_written_prefix_without_rewrite(tmp_path):
    device = Device()
    args = resources(tmp_path, device)
    usb.install(device, *args, execute=True, yes=True)
    usb.install(device, *args, execute=True, yes=True)
    assert len(device.writes) == 1


def test_original_backup_restore_preserves_hardware_boot_areas(tmp_path):
    device = Device()
    args = resources(tmp_path, device)
    usb.install(device, *args, execute=True, yes=True)
    usb.restore(device, args[-1], execute=True, yes=True)
    assert device.areas[0] == bytearray(1024)
    assert device.areas[1] == bytearray(512)
    device.areas[1][0] = 1
    with pytest.raises(ValueError, match='Hardware boot area changed'):
        usb.restore(device, args[-1], execute=True, yes=True)


def test_damaged_image_backup_and_missing_confirmation_are_refused(tmp_path):
    device = Device()
    image, manifest, trust, backup = resources(tmp_path, device)
    with pytest.raises(ValueError, match='requires both'):
        usb.install(device, image, manifest, trust, backup, execute=True)
    image.write_bytes(b'y' * 512)
    with pytest.raises(ValueError, match='Image hash'):
        usb.install(device, image, manifest, trust, backup, execute=True, yes=True)
    image.write_bytes(b'x' * 512)
    (backup / 'boot0.img').write_bytes(b'y' * 512)
    with pytest.raises(ValueError, match='Backup hash'):
        usb.install(device, image, manifest, trust, backup, execute=True, yes=True)
    assert device.writes == []


def test_changed_emmc_is_refused(tmp_path):
    device = Device()
    args = resources(tmp_path, device)
    with patch.object(device, 'identity', return_value={**device.identity(), 'cid_sha256': 'b' * 64}):
        with pytest.raises(ValueError, match='matching backup'):
            usb.install(device, *args, execute=True, yes=True)
    assert device.writes == []


def test_register_uses_decimal_offset_and_requires_success():
    class Adapter:
        def __init__(self): self.commands = []
        def bulkCmd(self, command, **kw): self.commands.append(command)
        def bulkCmdStat(self, **kw): return b'success\0'
        def readSimpleMemory(self, *args): return b'\0' * 4
    adapter = Adapter()
    usb.Transport(adapter).register('extcsd', 212)
    assert adapter.commands[0] == 'mmc reg read extcsd 212 armhpc_reg'
    with patch.object(adapter, 'bulkCmdStat', return_value=b'failed'):
        with pytest.raises(ValueError, match='rejected'):
            usb.Transport(adapter).select(0)


def test_final_chunk_has_exact_block_count():
    class Adapter:
        def __init__(self): self.commands = []
        def bulkCmd(self, command, **kw): self.commands.append(command)
        def bulkCmdStat(self, **kw): return b'success'
        def writeLargeMemory(self, *args): pass
    adapter = Adapter()
    usb.Transport(adapter).write(4096, b'x' * 512)
    assert adapter.commands[-1] == 'mmc write 20000000 8 1'
