# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import hashlib
import importlib.util
from pathlib import Path

import pytest


def load():
    path = Path(__file__).resolve().parents[1] / 'tools/build/refresh-fixed-boot-roots.py'
    spec = importlib.util.spec_from_file_location('fixed_boot_refresh', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(tmp_path):
    module = load()
    manifest = {'kernel_release': '6.12.111-meson', 'files': {}}
    for role, name in (('kernel', 'Image'), ('initramfs', 'uInitrd'), ('dtb', module.DTBS['wxy-oesp-v22'])):
        path = tmp_path / 'boot' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(role.encode())
        manifest['files'][role] = {'sha256': module.sha(path)}
    for role in module.ROLES:
        path = tmp_path / role / 'lib/modules' / manifest['kernel_release'] / 'armhpc-kernel-build-id'
        path.parent.mkdir(parents=True)
        path.write_text(manifest['files']['kernel']['sha256'])
    return module, manifest


def test_fixed_boot_accepts_complete_pair(tmp_path):
    module, manifest = fixture(tmp_path)
    assert module.validate_pair(tmp_path, manifest, 'wxy-oesp-v22') == manifest


@pytest.mark.parametrize('role', ['kernel', 'dtb', 'initramfs'])
def test_rejects_changed_fixed_boot_bytes(tmp_path, role):
    module, manifest = fixture(tmp_path)
    manifest['files'][role]['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='pairing differs'):
        module.validate_pair(tmp_path, manifest, 'wxy-oesp-v22')


def test_rejects_missing_matching_modules(tmp_path):
    module, manifest = fixture(tmp_path)
    (tmp_path / 'public/rescue/lib/modules' / manifest['kernel_release'] / 'armhpc-kernel-build-id').write_text('other')
    with pytest.raises(ValueError, match='modules'):
        module.validate_pair(tmp_path, manifest, 'wxy-oesp-v22')
