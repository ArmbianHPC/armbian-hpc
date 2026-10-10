# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Source preparation failures and publication guards, with isolated small fixtures."""

import gzip
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import unittest
from unittest import mock
import zlib

from framework.cli import main
from framework.config import ConfigError, resolve, sha256
from framework.image_audit import audit_rootfs
from framework.release import check_disk_image, prepare_release, publication_asset, source_audit
from framework.sources import git_command, make_git_archive, prepare_sources
from framework.staging import stage, write_json


class SourceReleaseTests(unittest.TestCase):
    def setUp(self):
        from test_framework import FrameworkTests
        self.fixture = FrameworkTests()
        self.fixture.setUp()
        self.root = self.fixture.root
        self.edit = self.fixture.edit

    def tearDown(self):
        self.fixture.tearDown()

    def missing_kernel(self):
        resolved = resolve(self.root, 'wxy-oect-base')
        item = next(item for item in resolved['inputs'] if item['id'] == 'kernel-source')
        content = (self.root / item['path']).read_bytes()
        (self.root / item['path']).unlink()
        return resolve(self.root, 'wxy-oect-base', require_sources=False), item, content

    def test_fresh_checkout_plan_reports_missing_cache_and_stage_refuses_it(self):
        resolved, item, content = self.missing_kernel()
        self.assertIn(item['path'], resolved['missing_inputs'])
        with self.assertRaises(ConfigError):
            stage(self.root, resolved)
        self.assertFalse((self.root / 'build/staging').exists())

    def test_source_verify_does_not_download_or_create_a_receipt(self):
        resolved, item, content = self.missing_kernel()
        with mock.patch('framework.sources.download') as download, self.assertRaisesRegex(ConfigError, '缺少本地输入'):
            prepare_sources(self.root, resolved, verify_only=True)
        download.assert_not_called()
        self.assertFalse((self.root / 'build/source-receipts').exists())

    def test_git_archive_uses_the_pinned_object_without_changing_checkout(self):
        directory = self.root / 'sources/test-upstream'
        directory.mkdir(parents=True)
        (directory / 'source.txt').write_bytes(b'fixed source fixture\n')
        git_command(directory, 'init', stdout=subprocess.PIPE)
        git_command(directory, 'add', '.', stdout=subprocess.PIPE)
        git_command(directory, '-c', 'user.name=Test Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-m', 'Fixed fixture', stdout=subprocess.PIPE)
        commit = git_command(directory, 'rev-parse', 'HEAD', stdout=subprocess.PIPE).stdout.decode().strip()
        (directory / 'source.txt').write_bytes(b'local uncommitted changes\n')
        destination = self.root / 'build/fixed.tar'
        destination.parent.mkdir(exist_ok=True)
        acquisition = {'type': 'git-archive', 'url': 'https://example.invalid/no-network',
                       'git_dir': 'sources/test-upstream', 'commit': commit, 'prefix': 'fixture/'}
        make_git_archive(self.root, acquisition, destination)
        import tarfile
        with tarfile.open(destination) as archive:
            self.assertEqual(archive.extractfile('fixture/source.txt').read(), b'fixed source fixture\n')
        self.assertEqual((directory / 'source.txt').read_bytes(), b'local uncommitted changes\n')
        self.assertEqual(git_command(directory, 'rev-parse', 'HEAD', stdout=subprocess.PIPE).stdout.decode().strip(), commit)

    def test_source_fetch_reuses_only_a_matching_historical_cache(self):
        resolved, item, content = self.missing_kernel()
        old = self.root / item['reuse_paths'][0]
        old.parent.mkdir(parents=True)
        old.write_bytes(content)
        with mock.patch('framework.sources.download') as download:
            receipt = prepare_sources(self.root, resolved)
        download.assert_not_called()
        self.assertEqual((self.root / item['path']).read_bytes(), content)
        self.assertTrue(any(entry['method'].startswith('reused:') for entry in receipt['inputs']))
        self.assertEqual(old.read_bytes(), content)

    def test_bad_download_never_becomes_an_active_input(self):
        resolved, item, content = self.missing_kernel()
        def bad_download(url, path):
            path.write_bytes(b'corrupt transfer')
        with mock.patch('framework.sources.download', side_effect=bad_download), self.assertRaisesRegex(ConfigError, 'SHA256'):
            prepare_sources(self.root, resolved)
        self.assertFalse((self.root / item['path']).exists())
        self.assertEqual(list((self.root / item['path']).parent.glob('*.part')), [])

    def test_target_selects_its_own_https_source_instead_of_an_oect_default(self):
        self.fixture.arm32_fixture()
        self.edit('adapters/soc/test-arm32-soc.json', lambda value: value['inputs'][0]['acquire'].update(url='https://example.invalid/armv7-source.tar.xz'))
        resolved = resolve(self.root, 'test-arm32-k3s')
        item = next(item for item in resolved['inputs'] if item['id'] == 'kernel-source')
        content = (self.root / item['path']).read_bytes()
        (self.root / item['path']).unlink()
        urls = []
        def fake_download(url, path):
            urls.append(url)
            path.write_bytes(content)
        with mock.patch('framework.sources.download', side_effect=fake_download):
            receipt = prepare_sources(self.root, resolve(self.root, 'test-arm32-k3s', require_sources=False))
        self.assertEqual(urls, ['https://example.invalid/armv7-source.tar.xz'])
        self.assertEqual(receipt['architecture'], 'armhf')

    def test_default_firmware_bundle_cannot_be_reenabled_by_profile(self):
        self.edit('profiles/os/debian-bookworm-minimal.json', lambda value: value['firmware_policy'].update(install_armbian_bundle=True))
        with self.assertRaisesRegex(ConfigError, '固件策略'):
            resolve(self.root, 'wxy-oect-base')

    def test_adding_a_firmware_package_does_not_bypass_board_review(self):
        self.edit('profiles/os/debian-bookworm-minimal.json', lambda value: value['packages'].append('firmware-realtek'))
        with self.assertRaisesRegex(ConfigError, '逐文件审核'):
            resolve(self.root, 'wxy-oect-base')

    def firmware_fixture(self):
        path = self.root / 'cache/firmware/test/ethernet.bin'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'test-only peripheral fixture')
        license_path = self.root / 'adapters/boards/wxy-oect-original/licenses/test.txt'
        license_path.parent.mkdir()
        license_path.write_text('Test fixture permission, not a real firmware license.')
        item = {'id': 'test-ethernet', 'path': path.relative_to(self.root).as_posix(), 'sha256': sha256(path),
                'destination': '/lib/firmware/test/ethernet.bin', 'license': 'test-fixture',
                'license_file': license_path.relative_to(self.root).as_posix(), 'reviewed': True, 'redistributable': True,
                'acquire': {'type': 'https', 'url': 'https://example.invalid/ethernet.bin'}}
        self.edit('adapters/boards/wxy-oect-original.json', lambda value: value.update(firmware=[item]))
        return item

    def test_only_reviewed_pinned_peripheral_firmware_can_be_staged(self):
        item = self.firmware_fixture()
        output = stage(self.root, resolve(self.root, 'wxy-oect-base'))
        self.assertEqual(sha256(output / 'rootfs-overlay' / ('usr/' + item['destination'].lstrip('/'))), item['sha256'])
        self.edit('adapters/boards/wxy-oect-original.json', lambda value: value['firmware'][0].update(reviewed=False))
        with self.assertRaisesRegex(ConfigError, '逐项审核'):
            resolve(self.root, 'wxy-oect-base')

    def test_approved_peripheral_list_still_rejects_a_boot_loader(self):
        self.firmware_fixture()
        self.edit('adapters/boards/wxy-oect-original.json', lambda value: value['firmware'][0].update(destination='/lib/firmware/rk3568_ddr.bin'))
        with self.assertRaisesRegex(ConfigError, '早期引导链'):
            resolve(self.root, 'wxy-oect-base')

    def rootfs_fixture(self):
        root = self.root / 'build/rootfs'
        status = root / 'var/lib/dpkg/status'
        status.parent.mkdir(parents=True)
        status.write_text('Package: base-files\nVersion: 1\nSource: base-source (2)\nStatus: install ok installed\n\n')
        return root

    def test_image_audit_detects_upstream_k3s_and_unexpected_firmware(self):
        root = self.rootfs_fixture()
        self.assertTrue(audit_rootfs(root, {'files': []})['passed'])
        for relative in ('usr/local/bin/k3s', 'usr/lib/firmware/network.bin'):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')
        result = audit_rootfs(root, {'files': []})
        self.assertFalse(result['passed'])
        self.assertTrue(any('K3s' in value for value in result['errors']))
        self.assertTrue(any('外设固件' in value for value in result['errors']))
        self.assertEqual(result['packages'][0]['source_version'], '2')

    def test_image_audit_detects_a_firmware_package_without_firmware_files(self):
        root = self.rootfs_fixture()
        with (root / 'var/lib/dpkg/status').open('a') as stream:
            stream.write('Package: armbian-firmware\nVersion: 1\nStatus: install ok installed\n\n')
        self.assertFalse(audit_rootfs(root, {'files': []})['passed'])

    def test_runtime_indexes_are_accepted_with_their_formats_and_packages(self):
        root = self.rootfs_fixture()
        with (root / 'var/lib/dpkg/status').open('a') as stream:
            for name in ('linux-image-test', 'kmod', 'udev'):
                stream.write(f'Package: {name}\nVersion: 1\nStatus: install ok installed\n\n')
        index = root / 'usr/lib/modules/6.12.111-test/modules.dep.bin'
        index.parent.mkdir(parents=True)
        index.write_bytes(struct.pack('>III', 0xb007f457, 0x00020001, 0))
        hwdb = root / 'usr/lib/udev/hwdb.bin'
        hwdb.parent.mkdir(parents=True)
        hwdb.write_bytes(b'KSLPHHRH' + struct.pack('<QQ', 252, 80) + bytes(56))
        self.assertTrue(audit_rootfs(root, {'files': []})['passed'])
        index.write_bytes(b'not a module index')
        self.assertFalse(audit_rootfs(root, {'files': []})['passed'])

    def test_runtime_data_exception_does_not_accept_renamed_boot_payloads(self):
        root = self.rootfs_fixture()
        with (root / 'var/lib/dpkg/status').open('a') as stream:
            for name in ('linux-image-test', 'kmod', 'udev'):
                stream.write(f'Package: {name}\nVersion: 1\nStatus: install ok installed\n\n')
        for relative in ('usr/lib/modules/6.12.111-test/modules.dep.bin',
                         'usr/lib/udev/hwdb.bin',
                         'usr/lib/modules/6.12.111-test/kernel/drivers/nvmem/nvmem_u-boot-env.ko'):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'boot payload fixture')
        result = audit_rootfs(root, {'files': []})
        self.assertEqual(len(result['errors']), 3)

    def git_fixture(self):
        for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md', '.gitignore', 'README.md'):
            source = Path(__file__).resolve().parents[1] / name
            (self.root / name).write_bytes(source.read_bytes())
        git_command(self.root, 'init', stdout=subprocess.PIPE)
        git_command(self.root, 'add', '.', stdout=subprocess.PIPE)
        git_command(self.root, '-c', 'user.name=Test Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'Isolated source fixture', stdout=subprocess.PIPE)

    def test_release_source_commit_cannot_omit_new_framework_files(self):
        self.git_fixture()
        self.assertTrue(source_audit(self.root)['passed'])
        (self.root / 'framework/new-required-module.py').write_text('# uncommitted source\n')
        result = source_audit(self.root)
        self.assertFalse(result['passed'])
        self.assertTrue(any('new-required-module.py' in value for value in result['errors']))

    def test_release_source_rejects_force_added_compressed_disk_images(self):
        self.git_fixture()
        (self.root / 'prohibited.img.xz').write_bytes(b'fixture, never a real image')
        git_command(self.root, 'add', '-f', 'prohibited.img.xz', stdout=subprocess.PIPE)
        git_command(self.root, '-c', 'user.name=Test Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'Bad source fixture', stdout=subprocess.PIPE)
        result = source_audit(self.root)
        self.assertFalse(result['passed'])
        self.assertTrue(any('prohibited.img.xz' in value for value in result['errors']))

    def disk_fixture(self):
        content = bytearray(512 * 512)
        content[450] = 0xee
        content[510:512] = b'\x55\xaa'
        entries = bytearray(128 * 128)
        entries[:16] = bytes(range(1, 17))
        entries[16:32] = bytes(range(17, 33))
        struct.pack_into('<QQ', entries, 32, 128, 383)
        content[2*512:34*512] = entries
        content[479*512:511*512] = entries
        for current, other, table in ((1, 511, 2), (511, 1, 479)):
            block = bytearray(512)
            block[:8] = b'EFI PART'
            struct.pack_into('<III', block, 8, 0x10000, 92, 0)
            struct.pack_into('<QQQQ', block, 24, current, other, 34, 478)
            struct.pack_into('<QIII', block, 72, table, 128, 128, zlib.crc32(entries))
            struct.pack_into('<I', block, 16, zlib.crc32(block[:92]))
            content[current*512:(current+1)*512] = block
        content[128*512+1080:128*512+1082] = b'\x53\xef'
        path = self.root / 'build/test.img'
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
        return path

    def test_gpt_gate_detects_a_raw_loader_outside_filesystem_partitions(self):
        path = self.disk_fixture()
        self.assertTrue(check_disk_image(path)['raw_gaps_zero'])
        with path.open('r+b') as stream:
            stream.seek(32768)
            stream.write(b'raw early boot fixture')
        with self.assertRaisesRegex(ConfigError, '分区外存在载荷'):
            check_disk_image(path)

    def permanent_boot_fixture(self):
        payload = self.root / 'cache/boot-chain/test-board/uboot.bin'
        payload.parent.mkdir(parents=True)
        payload.write_bytes(b'reviewed permanent boot fixture')
        license_path = self.root / 'adapters/boards/wxy-oect-original/licenses/uboot-license.txt'
        license_path.parent.mkdir(parents=True)
        license_path.write_text('Isolated GPL fixture license text\n')
        source_path = self.root / 'cache/sources/uboot/test-source.tar'
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(b'isolated corresponding source fixture')
        item = {
            'id': 'uboot-test', 'role': 'uboot', 'path': payload.relative_to(self.root).as_posix(),
            'sha256': sha256(payload), 'size_bytes': payload.stat().st_size, 'offset_bytes': 32768,
            'source_url': 'https://example.invalid/pinned-uboot', 'source_commit': 'a' * 40,
            'license': 'GPL-2.0-only', 'license_file': license_path.relative_to(self.root).as_posix(),
            'license_sha256': sha256(license_path), 'reviewed': True, 'redistributable': True,
            'corresponding_sources': [{'source': 'uboot-test', 'version': 'a' * 40,
                                      'path': source_path.relative_to(self.root).as_posix(), 'sha256': sha256(source_path)}],
        }
        self.edit('adapters/boards/wxy-oect-original.json', lambda value: value['boot'].update(permanent_components=[item]))
        return payload, item

    def test_permanent_components_have_a_separate_receipt_and_never_enter_rootfs(self):
        payload, item = self.permanent_boot_fixture()
        resolved = resolve(self.root, 'wxy-oect-base', verify_sources=True)
        output = stage(self.root, resolved)
        record = json.loads((output / 'boot-chain.json').read_text())
        self.assertEqual(record['components'][0]['sha256'], item['sha256'])
        self.assertFalse(record['temporary_loader_included'])
        self.assertFalse(record['assembly_implemented'])
        self.assertEqual(list(output.rglob('uboot.bin')), [])
        self.assertTrue(any('单个 IMG' in value['reason'] for value in resolved['blockers']))

    def test_unreviewed_permanent_component_is_rejected(self):
        self.permanent_boot_fixture()
        self.edit('adapters/boards/wxy-oect-original.json', lambda value: value['boot']['permanent_components'][0].update(reviewed=False))
        with self.assertRaisesRegex(ConfigError, '逐项核查'):
            resolve(self.root, 'wxy-oect-base')

    def test_gpl_permanent_component_requires_corresponding_sources(self):
        self.permanent_boot_fixture()
        self.edit('adapters/boards/wxy-oect-original.json', lambda value: value['boot']['permanent_components'][0].update(corresponding_sources=[]))
        with self.assertRaisesRegex(ConfigError, '完整对应源码'):
            resolve(self.root, 'wxy-oect-base')

    def test_temporary_miniloader_cannot_be_declared_as_a_permanent_component(self):
        self.permanent_boot_fixture()
        self.edit('adapters/boards/wxy-oect-original.json', lambda value: value['boot']['permanent_components'][0].update(id='miniloader-all'))
        with self.assertRaisesRegex(ConfigError, 'MiniLoader'):
            resolve(self.root, 'wxy-oect-base')

    def reviewed_disk_fixture(self):
        payload, item = self.permanent_boot_fixture()
        component = resolve(self.root, 'wxy-oect-base', verify_sources=True)['permanent_boot'][0]
        path = self.disk_fixture()
        with path.open('r+b') as stream:
            stream.seek(item['offset_bytes'])
            stream.write(payload.read_bytes())
        return path, component

    def test_gpt_gate_accepts_only_the_matching_reviewed_boot_payload(self):
        path, component = self.reviewed_disk_fixture()
        result = check_disk_image(path, [component])
        self.assertTrue(result['unlisted_raw_gaps_zero'])
        self.assertEqual(result['permanent_boot'][0]['sha256'], component['sha256'])
        with path.open('r+b') as stream:
            stream.seek(component['offset_bytes'])
            stream.write(b'X')
        with self.assertRaisesRegex(ConfigError, '永久引导组件 SHA256'):
            check_disk_image(path, [component])

    def test_reviewed_payload_does_not_allow_other_raw_gap_payloads(self):
        path, component = self.reviewed_disk_fixture()
        with path.open('r+b') as stream:
            stream.seek(49152)
            stream.write(b'unlisted boot fixture')
        with self.assertRaisesRegex(ConfigError, '分区外存在载荷'):
            check_disk_image(path, [component])

    def test_reviewed_payload_cannot_overlap_gpt_metadata(self):
        path, component = self.reviewed_disk_fixture()
        component['offset_bytes'] = 1024
        with self.assertRaisesRegex(ConfigError, '重叠'):
            check_disk_image(path, [component])

    def test_ext4_gate_detects_payload_in_reserved_boot_area(self):
        path = self.root / 'build/fs.img'
        path.parent.mkdir(exist_ok=True)
        content = bytearray(4096)
        content[1080:1082] = b'\x53\xef'
        path.write_bytes(content)
        self.assertEqual(check_disk_image(path)['type'], 'ext4-filesystem')
        content[:4] = b'BOOT'
        path.write_bytes(content)
        with self.assertRaisesRegex(ConfigError, '保留引导区'):
            check_disk_image(path)

    def test_publication_compression_must_match_the_audited_image(self):
        image = self.disk_fixture()
        asset = image.with_suffix('.img.gz')
        with gzip.open(asset, 'wb') as stream:
            stream.write(image.read_bytes())
        publication_asset(image, asset)
        with gzip.open(asset, 'wb') as stream:
            stream.write(b'another image')
        with self.assertRaisesRegex(ConfigError, '不一致'):
            publication_asset(image, asset)

    def test_failed_release_check_never_creates_a_publishable_bundle(self):
        with mock.patch('framework.release.check_release', return_value={'passed': False, 'errors': ['unbuilt']}):
            with self.assertRaisesRegex(ConfigError, 'unbuilt'):
                prepare_release(self.root, 'not-read.json', 'build/releases/test')
        self.assertFalse((self.root / 'build/releases/test').exists())


if __name__ == '__main__':
    unittest.main()
