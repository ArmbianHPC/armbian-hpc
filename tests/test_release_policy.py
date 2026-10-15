# SPDX-License-Identifier: GPL-2.0-only
import hashlib
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools/release-policy.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inputs(tmp_path):
    module = load('framework_release')
    rows = []
    for model in ('wxy-oect', 'wxy-oesp-v22'):
        for name in module.names('1.2.0', model, 'bookworm', '6.12.111', 'squashfs', '20261015'):
            path = tmp_path / name
            path.write_bytes(name.encode())
            rows.append({'file': name, 'size': path.stat().st_size, 'sha256': module.sha256(path),
                         'redistribution': {'reviewed': True, 'evidence': ['fixture']}})
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'tag': 'v1.2.0', 'packages': rows}))
    notes = tmp_path / 'v1.2.0.md'
    notes.write_text('Changes')
    notes.with_name('v1.2.0.zh-CN.md').write_text('Changes')
    assets = [{'id': i, 'name': r['file'], 'size': r['size'], 'digest': 'sha256:' + r['sha256']} for i, r in enumerate(rows)]
    return module, rows, manifest, notes, assets


def test_complete_manifest_retains_both_models_and_uses_draft_id(tmp_path):
    module, rows, manifest, notes, assets = inputs(tmp_path)
    with patch.object(module, 'release', return_value={'id': 42, 'tag_name': 'v1.2.0', 'assets': assets}) as query:
        result = module.update('owner/repo', 'v1.2.0', tmp_path, manifest=manifest, notes=notes, release_id=42)
    assert result['remove_assets'] == [] and len(result['packages']) == 4
    query.assert_called_once_with('owner/repo', 'v1.2.0', 42)


def test_per_model_update_is_rejected_before_any_remote_call(tmp_path):
    module = load('single_model_release')
    with patch.object(module, 'release') as query, pytest.raises(ValueError, match='every model'):
        module.update('owner/repo', 'v1.2.0', tmp_path, model='wxy-oect', kernel='6.12.111', build_date='20261015')
    query.assert_not_called()


def test_manifest_requires_pairs_and_reviewed_image_sources(tmp_path):
    module, rows, manifest, notes, assets = inputs(tmp_path)
    manifest.write_text(json.dumps({'tag': 'v1.2.0', 'packages': rows[:-1]}))
    with pytest.raises(ValueError, match='both'):
        module.manifest_files(manifest, 'v1.2.0')
    rows[0]['redistribution']['reviewed'] = False
    manifest.write_text(json.dumps({'tag': 'v1.2.0', 'packages': rows}))
    with pytest.raises(ValueError, match='redistribution'):
        module.manifest_files(manifest, 'v1.2.0')


def test_hash_change_never_mutates_a_release(tmp_path):
    module, rows, manifest, notes, assets = inputs(tmp_path)
    (tmp_path / rows[0]['file']).write_bytes(b'different')
    with patch.object(module.subprocess, 'run') as mutate, pytest.raises(ValueError, match='Local package'):
        module.update('owner/repo', 'v1.2.0', tmp_path, manifest=manifest, notes=notes, apply=True)
    mutate.assert_not_called()


def test_incomplete_upload_retains_old_assets(tmp_path):
    module, rows, manifest, notes, assets = inputs(tmp_path)
    original = {'id': 42, 'tag_name': 'v1.2.0', 'assets': [
        {'id': 99, 'name': 'old-file'}], 'upload_url': 'https://uploads.github.com/repos/owner/repo/releases/42/assets{?name,label}'}
    incomplete = {**original, 'assets': assets[:-1]}
    with patch.object(module, 'release', side_effect=[original, incomplete]), patch.object(module.subprocess, 'run') as mutate:
        with pytest.raises(ValueError, match='old assets retained'):
            module.update('owner/repo', 'v1.2.0', tmp_path, manifest=manifest, notes=notes, apply=True, release_id=42)
    assert not any('DELETE' in call.args[0] or 'PATCH' in call.args[0] for call in mutate.call_args_list)


def test_metadata_and_publish_updates_preserve_version_tag(tmp_path):
    module = load('draft_tag_regression')
    asset = tmp_path / 'package.tar.xz'
    asset.write_bytes(b'payload')
    notes = tmp_path / 'v1.2.0.md'
    notes.write_text('Changes')
    notes.with_name('v1.2.0.zh-CN.md').write_text('Changes')
    current = {'id': 42, 'name': 'v1.2.0', 'tag_name': 'v1.2.0', 'draft': True,
        'prerelease': False, 'upload_url': 'https://uploads.github.com/repos/owner/repo/releases/42/assets{?name,label}',
        'html_url': 'https://github.com/owner/repo/releases/tag/v1.2.0',
        'assets': [{'name': asset.name, 'size': asset.stat().st_size, 'digest': 'sha256:' + module.sha256(asset)}]}
    public = {**current, 'draft': False}
    with patch.object(module, 'release', side_effect=[current, current, current, public]), patch.object(module.subprocess, 'run') as mutate:
        result = module.publish_packages('owner/repo', 'v1.2.0', [asset], notes,
            apply=True, release_id=42, publish=True)
    updates = [json.loads(call.kwargs['input']) for call in mutate.call_args_list if 'PATCH' in call.args[0]]
    assert len(updates) == 2 and all(row['tag_name'] == 'v1.2.0' for row in updates)
    assert updates[0]['draft'] is True and updates[1]['draft'] is False
    assert result['draft'] is False
