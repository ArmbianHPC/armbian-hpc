# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Reviewable permanent boot inputs, separate from source and temporary loaders."""

from copy import deepcopy
from pathlib import PurePosixPath
import re

from .config import BOOT_CHAIN_POLICY, https_url, require, sha256, validate_source_only, workspace_path


REVIEWED_PERMANENT_CHAIN_POLICY = {
    'provider': 'reviewed-components', 'scope': 'permanent-board-chain',
    'build_in_framework': False, 'include_in_image': True,
    'write_by_framework': True, 'preserve_existing_regions': True,
    'temporary_loader_provider': 'user', 'include_temporary_loader_in_image': False,
    'include_payloads_in_repository': False,
}
PERMANENT_ROLES = ('idbloader', 'uboot', 'trust', 'boot-environment')


def validate_boot_chain(root, board, *, verify_sources, require_sources):
    policy = board['boot'].get('chain')
    require(policy == BOOT_CHAIN_POLICY or policy == REVIEWED_PERMANENT_CHAIN_POLICY,
            '完整板级引导链策略必须为用户提供，或逐项审核的永久组件；MiniLoader 不进入镜像或源码。')
    entries = board['boot'].get('permanent_components', [])
    require(isinstance(entries, list), '永久引导组件清单必须为列表。')
    if policy == BOOT_CHAIN_POLICY:
        require(not entries, '用户引导链策略不能登记永久引导载荷。')
        return [], []
    if not entries:
        return [], ['永久引导链尚未登记通过许可审核的组件、对应源码、SHA256 和写入范围。']
    result = []
    names = set()
    regions = []
    fields = {'id', 'role', 'path', 'sha256', 'size_bytes', 'offset_bytes',
              'source_url', 'source_commit', 'license', 'license_file', 'license_sha256',
              'reviewed', 'redistributable', 'corresponding_sources'}
    for entry in entries:
        item = deepcopy(entry)
        require(set(item) == fields, '永久引导组件字段无效。')
        require(re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', item['id']) and item['id'] not in names,
                '永久引导组件 id 无效或重复。')
        names.add(item['id'])
        require(item['role'] in PERMANENT_ROLES and 'miniloader' not in (item['id'] + item['path']).lower(),
                'MiniLoader 是用户准备的临时刷机程序，不能登记为永久引导组件。')
        require(item['path'].startswith('cache/boot-chain/'), '永久引导二进制必须位于被忽略的 cache/boot-chain。')
        require(item['reviewed'] is True and item['redistributable'] is True,
                '永久引导组件必须逐项核查来源、许可证和再分发条件。')
        require(isinstance(item['license'], str) and item['license'], '永久引导组件缺少许可证。')
        https_url(item['source_url'])
        require(item['source_commit'] is None or re.fullmatch(r'[0-9a-f]{40}', item['source_commit']),
                '永久引导组件源码提交必须固定完整 commit。')
        for field in ('sha256', 'license_sha256'):
            require(isinstance(item[field], str) and re.fullmatch(r'[0-9a-f]{64}', item[field]),
                    '永久引导组件及许可文本必须固定 SHA256。')
        for field in ('size_bytes', 'offset_bytes'):
            require(type(item[field]) is int and item[field] > 0, '永久引导组件大小和偏移必须为正整数。')
        require(item['offset_bytes'] % 512 == 0, '永久引导组件偏移必须按 512 字节对齐。')
        license_path = workspace_path(root, item['license_file'])
        require(item['license_file'].startswith('adapters/boards/') and license_path.is_file()
                and sha256(license_path) == item['license_sha256'], '永久引导组件许可文本缺失或 SHA256 不匹配。')
        validate_source_only(PurePosixPath(item['license_file']))
        require(isinstance(item['corresponding_sources'], list), '永久引导对应源码必须为列表。')
        if item['role'] == 'uboot' or 'GPL' in item['license'].upper():
            require(item['corresponding_sources'], 'U-Boot/GPL 永久引导组件必须登记完整对应源码。')
        for source in item['corresponding_sources']:
            require(set(source) == {'source', 'version', 'path', 'sha256'}
                    and source['source'] and source['version'], '永久引导对应源码记录无效。')
            require(source['path'].startswith('cache/sources/')
                    and re.fullmatch(r'[0-9a-f]{64}', source['sha256']), '永久引导对应源码需要缓存路径和 SHA256。')
            validate_source_only(PurePosixPath(source['path']))
            source_path = workspace_path(root, source['path'], exists=require_sources or verify_sources)
            require(not source_path.exists() or source_path.is_file(), '永久引导对应源码必须为文件。')
            source['present'] = source_path.is_file()
            source['verified'] = verify_sources
            if verify_sources:
                require(sha256(source_path) == source['sha256'], '永久引导对应源码 SHA256 不匹配。')
        path = workspace_path(root, item['path'], exists=require_sources or verify_sources)
        require(not path.exists() or path.is_file(), '永久引导组件必须为文件。')
        item.update(present=path.is_file(), verified=verify_sources)
        if path.is_file():
            require(path.stat().st_size == item['size_bytes'], '永久引导组件大小不匹配。')
        if verify_sources:
            require(sha256(path) == item['sha256'], '永久引导组件 SHA256 不匹配。')
        regions.append((item['offset_bytes'], item['offset_bytes'] + item['size_bytes']))
        result.append(item)
    end = 0
    for start, stop in sorted(regions):
        require(start >= end, '永久引导组件写入范围重叠。')
        end = stop
    blockers = ['永久引导组件的 IMG 组装尚未实现。']
    if any(not item['present'] or any(not source['present'] for source in item['corresponding_sources'])
           for item in result):
        blockers.append('缺少永久引导组件本地缓存或对应源码。')
    return result, blockers


def reject_failed_candidate(compatibility, components):
    """Reject the exact board-chain pairs with recorded physical boot failures."""
    require(compatibility.get('schema_version') == 1, '引导兼容性记录版本无效。')
    failures = compatibility.get('rejected_candidates')
    require(isinstance(failures, list), '引导失败记录必须为列表。')
    require(set(components) == {'idbloader', 'uboot'} and all(
        isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)
        for value in components.values()), '本地引导候选必须提供 IDB/U-Boot SHA256。')
    for failure in failures:
        failed = failure.get('components')
        require(isinstance(failed, dict) and set(failed) == {'idbloader', 'uboot'}
                and all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)
                        for value in failed.values()), '已报告失败的引导组件记录无效。')
        require(isinstance(failure.get('reason'), str) and failure['reason'],
                '引导失败记录缺少具体原因。')
        require(failed != components, failure['reason'])
