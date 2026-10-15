# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest


def load():
    path = Path(__file__).resolve().parents[1] / 'tools/build/prepare-firmware-package.py'
    spec = importlib.util.spec_from_file_location('firmware_source_package', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package(tmp_path, name, content):
    source = tmp_path / 'kernel.tar.xz'
    with tarfile.open(source, 'w:xz') as archive:
        member = tarfile.TarInfo(name)
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    manifest = tmp_path / 'sources.json'
    manifest.write_text(json.dumps({'sources': [{'kind': 'kernel', 'path': str(source),
        'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}]}))
    return manifest


def test_only_exact_public_fixture_bytes_are_allowed(tmp_path, monkeypatch):
    module = load()
    name = 'tools/testing/selftests/sgx/sign_key.pem'
    content = b'public fixture'
    monkeypatch.setattr(module, 'PUBLIC_KERNEL_FIXTURES', {name: hashlib.sha256(content).hexdigest()})
    manifest = package(tmp_path, 'linux-6.12.111/' + name, content)
    assert module.source_inputs(manifest)
    manifest = package(tmp_path, 'linux-6.12.111/' + name, b'different private material')
    with pytest.raises(ValueError, match='Unrecognised key'):
        module.source_inputs(manifest)


def test_private_key_or_parent_path_is_rejected(tmp_path):
    module = load()
    for name in ('device-private.pem', '../escape'):
        manifest = package(tmp_path, name, b'input')
        with pytest.raises(ValueError):
            module.source_inputs(manifest)
