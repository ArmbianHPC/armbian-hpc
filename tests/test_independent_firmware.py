# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location('independent_firmware_builder', ROOT / 'tools/build/build-firmware-release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(tmp_path, ota_version, framework_version):
    project = tmp_path / 'armbian-hpc'
    ota = tmp_path / 'armhpc-ota'
    for repo, version in ((project, framework_version), (ota, ota_version)):
        repo.mkdir()
        (repo / 'VERSION').write_text(version)
        for name in ('COPYRIGHT', 'LICENSE'):
            (repo / name).write_text('source fixture')
    (project / 'COMPATIBILITY.json').write_bytes((ROOT / 'COMPATIBILITY.json').read_bytes())
    if ota_version != '1.2.1':
        (ota / 'COMPATIBILITY.json').write_text(json.dumps({'schema_version': 1, 'component': 'armhpc-ota',
            'source_interface': 1, 'ota_protocol': 1, 'minimum_companion_version': '1.2.1'}))
    provenance = tmp_path / 'provenance.json'
    provenance.write_text('{}')
    cfg = {'ota_source': str(ota), 'output': str(tmp_path / 'output'), 'work': str(tmp_path / 'work'),
           'input_roots': 'unused', 'boot_manifest': 'unused', 'watchdog_binary': 'unused',
           'board': 'wxy-oect-original', 'release': 'framework-' + framework_version, 'epoch': '0',
           'provenance': str(provenance)}
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(cfg))
    return project, ota, config, cfg


@pytest.mark.parametrize('ota_version,framework_version', [('1.2.10', '1.2.1'), ('1.2.1', '1.2.2')])
def test_firmware_build_uses_framework_version_and_records_separate_agent(tmp_path, monkeypatch, ota_version, framework_version):
    builder = load()
    project, ota, config, cfg = fixture(tmp_path, ota_version, framework_version)
    monkeypatch.setattr(builder, '__file__', str(project / 'tools/build/build-firmware-release.py'))
    monkeypatch.setattr(builder.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(builder.subprocess, 'check_output',
        lambda args, **kwargs: '' if kwargs.get('text') is None else ('a' if Path(args[2]) == project else 'b') * 40)

    class PackagingReached(Exception):
        pass

    def run(args, **kwargs):
        script = Path(args[1]).name
        if script == 'compare-squashfs.py':
            raise PackagingReached()
        if script == 'refresh-fixed-boot-roots.py':
            work = Path(cfg['work'])
            for role in ('normal', 'rescue', 'public/system', 'public/rescue'):
                (work / role).mkdir(parents=True)
            (work / 'boot-manifest.json').write_text('{}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(builder.subprocess, 'run', run)
    with pytest.raises(PackagingReached):
        builder.build(config)
    for role in ('normal', 'rescue', 'public/system', 'public/rescue'):
        info = json.loads((Path(cfg['work']) / role / 'usr/share/doc/armhpc/BUILD-SOURCES.json').read_text())
        assert info['version'] == framework_version
        assert info['source_versions'] == {'armbian-hpc': framework_version, 'armhpc-ota': ota_version}
        assert info['compatibility']['minimum_agent'] == '1.2.1'
        assert info['source_commits'] == {'armbian-hpc': 'a' * 40, 'armhpc-ota': 'b' * 40}


def test_firmware_build_rejects_missing_required_agent_before_creating_output(tmp_path, monkeypatch):
    builder = load()
    project, ota, config, cfg = fixture(tmp_path, '1.2.0', '1.2.2')
    monkeypatch.setattr(builder, '__file__', str(project / 'tools/build/build-firmware-release.py'))
    monkeypatch.setattr(builder.os, 'geteuid', lambda: 0)
    with pytest.raises(ValueError, match='requires armhpc-ota >= 1.2.1'):
        builder.build(config)
    assert not Path(cfg['output']).exists()
