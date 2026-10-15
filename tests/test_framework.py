# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Check composition, architecture separation, staging guards, and build gates."""

import contextlib
import ast
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from framework.cli import main
from framework.config import ARCHITECTURES, BOOT_CHAIN_POLICY, ConfigError, catalog, resolve, sha256, validate_source_only, workspace_path
from framework.staging import stage, write_json


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / 'build/tests'


class FrameworkTests(unittest.TestCase):
    def setUp(self):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='framework-', dir=TEST_ROOT)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(TEST_ROOT.resolve()))
        # Fixtures contain configuration and small sources, not the large cache.
        for directory in ('backends', 'adapters', 'profiles', 'targets', 'components', 'framework'):
            shutil.copytree(ROOT / directory, self.root / directory)
        for directory in ('backends', 'adapters/soc', 'adapters/boards', 'profiles/workloads'):
            for path in (self.root / directory).glob('*.json'):
                record = json.loads(path.read_text(encoding='utf-8'))
                for item in record.get('inputs', []):
                    source = self.root / item['path']
                    source.parent.mkdir(parents=True, exist_ok=True)
                    if not source.exists():
                        source.write_bytes(('isolated test input: ' + item['id']).encode())
                    item['sha256'] = sha256(source)
                write_json(path, record)

    def tearDown(self):
        # Check the absolute deletion target before TemporaryDirectory cleans up.
        self.assertTrue(self.root.is_relative_to(TEST_ROOT.resolve()))
        self.assertNotEqual(self.root, TEST_ROOT.resolve())
        self.temporary.cleanup()

    def edit(self, relative, change):
        path = self.root / relative
        record = json.loads(path.read_text(encoding='utf-8'))
        change(record)
        write_json(path, record)

    def arm32_fixture(self):
        soc = json.loads((self.root / 'adapters/soc/rockchip-rk3566.json').read_text(encoding='utf-8'))
        soc.update(id='test-arm32-soc', name='Test ARMv7 SoC', architectures=['armhf'],
                   backend_families={'armbian-v25-08': 'test-arm32-family'}, kernel_requirements={})
        write_json(self.root / 'adapters/soc/test-arm32-soc.json', soc)
        dts = self.root / 'adapters/boards/test-arm32-board/dts/test.dts'
        dts.parent.mkdir(parents=True)
        dts.write_text('/dts-v1/; / { compatible = "test,arm32-board"; };\n')
        board = json.loads((self.root / 'adapters/boards/wxy-oect-original.json').read_text(encoding='utf-8'))
        board.update(id='test-arm32-board', name='Test ARM32 board', soc='test-arm32-soc',
                     architectures=['armhf'], workload_overlays={})
        board['boot'].update(dtb='test.dtb', dts=dts.relative_to(self.root).as_posix(),
                             chain=dict(BOOT_CHAIN_POLICY), permanent_components=[])
        board['boot'].pop('flash_workflow', None)
        board['inputs'] = [{'id': 'board-dts', 'path': board['boot']['dts'], 'sha256': sha256(dts), 'stage_as': 'dts/test.dts'}]
        write_json(self.root / 'adapters/boards/test-arm32-board.json', board)
        target = json.loads((self.root / 'targets/wxy-oect-k3s.json').read_text(encoding='utf-8'))
        target.update(id='test-arm32-k3s', name='Test ARM32 target', architecture='armhf', board='test-arm32-board')
        target['overrides']['storage']['data'].update(label='ARM32_DATA', mountpoint='/srv/persistent')
        write_json(self.root / 'targets/test-arm32-k3s.json', target)
        return resolve(self.root, 'test-arm32-k3s')

    def test_real_oect_composition_keeps_storage_decisions(self):
        config = resolve(self.root, 'wxy-oect-k3s')
        self.assertEqual(config['architecture_info']['kernel_arch'], 'arm64')
        self.assertEqual(config['storage']['root']['size_bytes'], 64 * 1024**3)
        self.assertEqual(config['storage']['data']['label'], 'OECT_DATA')
        self.assertEqual(config['setup_policy']['configuration_dir'], '/data/node-config/k3s')
        self.assertFalse(config['build_ready'])
        self.assertGreater(len(config['blockers']), 0)

    def test_no_boot_or_runtime_installer_is_a_build_dependency(self):
        config = resolve(self.root, 'wxy-oect-k3s', verify_sources=True)
        self.assertEqual(config['board']['boot']['chain']['provider'], 'reviewed-components')
        self.assertFalse(config['board']['boot']['chain']['include_temporary_loader_in_image'])
        self.assertTrue(any('永久引导链尚未登记' in item['reason'] for item in config['blockers']))
        self.assertEqual(config['workload']['inputs'], [])
        self.assertIn('board-dts', [item['id'] for item in config['board']['inputs']])
        for item in config['board']['inputs']:
            self.assertNotIn(Path(item['path']).suffix, ('.bin', '.elf', '.img', '.itb', '.deb'))
            self.assertEqual(item['acquire']['type'], 'repository')
        self.assertEqual(config['setup_policy']['download_source_selection'], 'ssh_select')
        self.assertFalse(config['setup_policy']['include_installer_in_image'])

    def test_user_boot_policy_cannot_enable_build_packaging_or_write(self):
        self.edit('adapters/boards/wxy-oect-original.json', lambda value: value['boot'].update(chain=dict(BOOT_CHAIN_POLICY)))
        for field in ('build_in_framework', 'include_in_image', 'write_by_framework'):
            with self.subTest(field=field):
                self.edit('adapters/boards/wxy-oect-original.json', lambda v: v['boot']['chain'].update({field: True}))
                with self.assertRaisesRegex(ConfigError, '完整板级引导链'):
                    resolve(self.root, 'wxy-oect-k3s')
                self.edit('adapters/boards/wxy-oect-original.json', lambda v: v['boot']['chain'].update({field: False}))

    def test_boot_payload_cannot_be_an_input_even_without_stage_as(self):
        payload = self.root / 'local/loader.bin'
        payload.parent.mkdir()
        payload.write_bytes(b'isolated boot payload fixture')
        self.edit('adapters/boards/wxy-oect-original.json', lambda v: v['inputs'].append(
            {'id': 'loader', 'path': 'local/loader.bin', 'sha256': sha256(payload)}))
        with self.assertRaisesRegex(ConfigError, '引导链或上游 K3s 载荷'):
            resolve(self.root, 'wxy-oect-k3s')

    def test_boot_payload_cannot_enter_through_overlay(self):
        payload = self.root / 'components/common/rootfs-overlay/usr/local/sbin/loader.bin'
        payload.write_bytes(b'isolated boot payload fixture')
        with self.assertRaisesRegex(ConfigError, '引导链或上游 K3s 载荷'):
            stage(self.root, resolve(self.root, 'wxy-oect-base'))

    def test_compressed_payloads_cannot_bypass_source_validation(self):
        for name in ('payload.img.xz', 'payload.img.zst', 'payload.img.gz', 'payload.img.zip',
                     'payload.img.tar.gz', 'loader.bin.xz', 'firmware.itb.zst', 'k3s-arm64.gz', 'k3s-armhf.xz'):
            with self.subTest(name=name), self.assertRaisesRegex(ConfigError, '引导链或上游 K3s 载荷'):
                validate_source_only(Path(name))
        validate_source_only(Path('kernel-source.tar.xz'))

    def test_stage_as_cannot_rename_source_to_a_boot_payload(self):
        self.edit('adapters/boards/wxy-oect-original.json', lambda item: item['inputs'][0].update(stage_as='boot/loader.bin'))
        with self.assertRaisesRegex(ConfigError, '引导链或上游 K3s 载荷'):
            resolve(self.root, 'wxy-oect-k3s')

    def test_backend_destination_cannot_rename_source_to_a_boot_payload(self):
        def edit_board(item):
            item['backend_files'] = {'armbian-v25-08': [{
                'source': item['boot']['dts'], 'destination': 'bootenv/loader.bin'}]}
        self.edit('adapters/boards/wxy-oect-original.json', edit_board)
        with self.assertRaisesRegex(ConfigError, '引导链或上游 K3s 载荷'):
            resolve(self.root, 'wxy-oect-k3s')

    def test_administrator_policy_rejects_default_password_access(self):
        self.edit('profiles/os/debian-bookworm-minimal.json', lambda v: v['access_policy'].update(password_ssh_login=True))
        with self.assertRaisesRegex(ConfigError, '管理员和 SSH 公钥'):
            resolve(self.root, 'wxy-oect-base')

    def test_stage_emits_install_contract_for_normal_and_rescue_roots(self):
        output = stage(self.root, resolve(self.root, 'wxy-oect-base'))
        policy = json.loads((output / 'installation-policy.json').read_text(encoding='utf-8'))
        self.assertEqual(policy['provision_root_filesystems'], ['normal', 'rescue'])
        self.assertTrue(policy['boot_chain']['preserve_existing_regions'])
        self.assertTrue(policy['access_policy']['require_provisioning_before_ssh'])
        self.assertEqual(policy['administrator_helper'], '/usr/lib/armhpc/admin.py')
        self.assertEqual(policy['rescue_entrypoint'], '/usr/local/sbin/armhpc-rescue')
        self.assertTrue((output / 'rootfs-overlay/usr/lib/armhpc/admin.py').is_file())
        self.assertTrue((output / 'rootfs-overlay/usr/local/sbin/armhpc').is_file())

    def test_framework_maps_both_architectures(self):
        self.assertEqual(ARCHITECTURES['armhf']['kernel_arch'], 'arm')
        self.assertEqual(ARCHITECTURES['armhf']['k3s_asset'], 'k3s-armhf')
        self.assertEqual(ARCHITECTURES['arm64']['k3s_asset'], 'k3s-arm64')

    def test_registered_board_cannot_select_unsupported_architecture(self):
        self.edit('targets/wxy-oect-k3s.json', lambda item: item.update(architecture='armhf'))
        with self.assertRaisesRegex(ConfigError, '不支持 armhf'):
            resolve(self.root, 'wxy-oect-k3s')

    def test_arm32_target_stages_32_bit_settings_and_selected_paths(self):
        config = self.arm32_fixture()
        output = stage(self.root, config)
        policy = json.loads((output / 'rootfs-overlay/etc/image-framework/k3s.json').read_text(encoding='utf-8'))
        self.assertEqual(config['architecture_info']['bits'], 32)
        self.assertEqual(config['architecture_info']['dts_directory'], 'arch/arm/boot/dts')
        self.assertEqual(policy['architecture'], 'armhf')
        self.assertEqual(policy['installer_arch'], 'arm')
        self.assertEqual(policy['data_dir'], '/srv/persistent/k3s')
        self.assertEqual(policy['data_label'], 'ARM32_DATA')
        script = (output / 'userpatches/customize-image.sh').read_text()
        self.assertIn('= armhf', script)
        self.assertFalse((output / 'rootfs-overlay/usr/local/sbin/oect-setup').exists())

    def test_base_target_contains_no_k3s_component_or_swap_policy(self):
        config = resolve(self.root, 'wxy-oect-base')
        output = stage(self.root, config)
        self.assertEqual(config['components'], ['common'])
        self.assertIsNone(config['setup_policy'])
        self.assertNotIn('curl', config['packages'])
        self.assertFalse((output / 'rootfs-overlay/usr/local/sbin/armhpc-kube').exists())
        self.assertFalse((output / 'rootfs-overlay/usr/local/sbin/oect-setup').exists())
        self.assertFalse((output / 'rootfs-overlay/etc/image-framework/k3s.json').exists())
        runtime = json.loads((output / 'rootfs-overlay/etc/image-framework/runtime-requirements.json').read_text(encoding='utf-8'))
        self.assertEqual(runtime['swap_enabled'], 'os_default')
        self.assertEqual(runtime['bootargs_required'], [])

    def test_stage_preserves_partition_geometry_and_provenance(self):
        output = stage(self.root, resolve(self.root, 'wxy-oect-k3s'))
        layout = json.loads((output / 'storage-layout.json').read_text(encoding='utf-8'))
        partitions = layout['external']['partitions']
        self.assertEqual(partitions[0]['start_offset_bytes'], 1048576)
        self.assertEqual(partitions[1]['start_offset_bytes'], 1048576 + 64 * 1024**3)
        self.assertTrue(layout['recovery']['default_preserve_data_partition'])
        receipt = json.loads((output / 'source-receipt.json').read_text())
        self.assertTrue(all(item['verified'] for item in receipt['inputs']))
        manifest = json.loads((output / 'stage-manifest.json').read_text(encoding='utf-8'))
        self.assertFalse(manifest['image_built'])
        self.assertFalse(manifest['hardware_verified'])
        self.assertFalse((output / '.incomplete').exists())
        for relative, digest in manifest['files'].items():
            self.assertEqual(sha256(output / relative), digest)
        self.assertEqual(list(output.rglob('k3s-arm*')), [])

    def test_stage_never_overwrites_existing_output(self):
        config = resolve(self.root, 'wxy-oect-base')
        output = stage(self.root, config)
        sentinel = output / 'existing.txt'
        sentinel.write_text('keep')
        with self.assertRaisesRegex(ConfigError, '输出已存在'):
            stage(self.root, config)
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_output_must_stay_in_staging(self):
        for value in ('../outside', 'sources/armbian-build/userpatches', 'build/staging'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                stage(self.root, resolve(self.root, 'wxy-oect-base'), value)

    def test_modified_source_fails_before_staging_writes(self):
        config = resolve(self.root, 'wxy-oect-k3s')
        (self.root / config['inputs'][0]['path']).write_bytes(b'altered')
        with self.assertRaisesRegex(ConfigError, '输入校验失败'):
            stage(self.root, config)
        self.assertFalse((self.root / 'build/staging/wxy-oect-k3s').exists())

    def test_external_storage_must_be_supported_by_board(self):
        self.edit('targets/wxy-oect-k3s.json', lambda item: item['overrides']['storage'].update(external_interface='nvme'))
        with self.assertRaisesRegex(ConfigError, '外置存储接口'):
            resolve(self.root, 'wxy-oect-k3s')

    def test_duplicate_labels_and_non_ext4_profile_are_rejected(self):
        self.edit('targets/wxy-oect-k3s.json', lambda item: item['overrides']['storage']['root'].update(label='OECT_DATA'))
        with self.assertRaisesRegex(ConfigError, '不能重复'):
            resolve(self.root, 'wxy-oect-k3s')
        self.edit('targets/wxy-oect-k3s.json', lambda item: item['overrides']['storage']['root'].update(label='OECT_ROOT'))
        self.edit('profiles/storage/emmc-sata-squashfs-ab.json', lambda item: item['root'].update(filesystem='xfs'))
        with self.assertRaisesRegex(ConfigError, '全部使用 ext4'):
            resolve(self.root, 'wxy-oect-k3s')

    def test_profile_mountpoint_is_not_fixed_to_data(self):
        self.edit('targets/wxy-oect-k3s.json', lambda item: item['overrides']['storage']['data'].update(mountpoint='/srv/disk'))
        policy = resolve(self.root, 'wxy-oect-k3s')['setup_policy']
        self.assertEqual(policy['local_pv_dir'], '/srv/disk/pv')
        self.assertEqual(policy['node_identity_source'], '/srv/disk/node-config/k3s/node')

    def test_arm32_board_without_sata_uses_single_media_profile(self):
        self.arm32_fixture()
        self.edit('adapters/boards/test-arm32-board.json', lambda item: item.update(storage_interfaces=['emmc', 'usb']))
        def change_target(item):
            item['storage'] = 'single-media-ext4'
            item['overrides']['storage'] = {'boot_media': 'emmc', 'root': {'size_bytes': 4 * 1024**3}}
        self.edit('targets/test-arm32-k3s.json', change_target)
        config = resolve(self.root, 'test-arm32-k3s')
        output = stage(self.root, config)
        layout = json.loads((output / 'storage-layout.json').read_text(encoding='utf-8'))
        self.assertEqual(layout['strategy'], 'single-media')
        self.assertEqual(layout['boot_disk']['interface'], 'emmc')
        self.assertEqual([item['role'] for item in layout['boot_disk']['partitions']], ['boot', 'root', 'data'])
        self.assertNotIn('external', layout)
        self.assertEqual(config['setup_policy']['root_size_bytes'], 4 * 1024**3)

    def test_single_media_rejects_an_external_root_interface(self):
        self.edit('targets/wxy-oect-base.json', lambda item: item.update(storage='single-media-ext4', overrides={'storage': {'boot_media': 'emmc', 'external_interface': 'sata'}}))
        with self.assertRaisesRegex(ConfigError, '单盘布局不能指定'):
            resolve(self.root, 'wxy-oect-base')

    def test_input_paths_and_stage_paths_reject_traversal(self):
        for value in ('../secret', '/etc/passwd', 'C:/Windows/win.ini', 'folder\\file'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                workspace_path(self.root, value, exists=False)
        self.edit('adapters/boards/wxy-oect-original.json', lambda item: item['inputs'][0].update(stage_as='/outside.dts'))
        with self.assertRaisesRegex(ConfigError, '相对路径'):
            resolve(self.root, 'wxy-oect-k3s')

    def test_unknown_target_overrides_cannot_change_architecture(self):
        self.edit('targets/wxy-oect-k3s.json', lambda item: item['overrides'].update(architecture='armhf'))
        with self.assertRaisesRegex(ConfigError, '只允许覆写'):
            resolve(self.root, 'wxy-oect-k3s')

    def test_runtime_settings_cannot_override_architecture_mapping(self):
        self.edit('profiles/workloads/k3s-online.json', lambda item: item['settings'].update(runtime_machines=['x86_64']))
        with self.assertRaisesRegex(ConfigError, '未知字段'):
            resolve(self.root, 'wxy-oect-k3s')

    def test_incompatible_kernel_requirements_are_rejected(self):
        self.edit('adapters/soc/rockchip-rk3566.json', lambda item: item['kernel_requirements'].update(CONFIG_EXT4_FS=['m']))
        self.edit('profiles/storage/emmc-sata-squashfs-ab.json', lambda item: item['kernel_requirements'].update(CONFIG_EXT4_FS=['y']))
        with self.assertRaisesRegex(ConfigError, '内核要求冲突'):
            resolve(self.root, 'wxy-oect-k3s')

    def test_actual_kernel_config_missing_memory_controller_fails(self):
        config = resolve(self.root, 'wxy-oect-k3s')
        path = self.root / 'fixture.config'
        path.write_text(''.join(f'{name}={values[0]}\n' for name, values in config['kernel_requirements'].items()))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--root', str(self.root), 'check-kernel', 'wxy-oect-k3s', str(path)]), 0)
        path.write_text(path.read_text().replace('CONFIG_MEMCG=y\n', '# CONFIG_MEMCG is not set\n'))
        response = io.StringIO()
        with contextlib.redirect_stdout(response):
            self.assertEqual(main(['--root', str(self.root), 'check-kernel', 'wxy-oect-k3s', str(path)]), 1)
        self.assertIn('CONFIG_MEMCG', response.getvalue())

    def test_overlay_conflicts_are_not_silently_overwritten(self):
        overlay = 'adapters/boards/wxy-oect-original/rootfs-overlay'
        path = self.root / overlay / 'usr/lib/armhpc/kube_setup.py'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('unexpected override')
        self.edit('adapters/boards/wxy-oect-original.json',
                  lambda item: item.update(workload_overlays={'k3s': overlay}))
        with self.assertRaisesRegex(ConfigError, 'Overlay 内容冲突'):
            stage(self.root, resolve(self.root, 'wxy-oect-k3s'))
        self.assertTrue((self.root / 'build/staging/wxy-oect-k3s/.incomplete').exists())

    def test_generated_policy_namespace_is_reserved(self):
        path = self.root / 'components/common/rootfs-overlay/etc/image-framework/node.json'
        path.parent.mkdir(parents=True)
        path.write_text('{}')
        with self.assertRaisesRegex(ConfigError, '保留目录'):
            stage(self.root, resolve(self.root, 'wxy-oect-base'))

    def test_catalog_does_not_claim_arm32_hardware_support(self):
        listing = catalog(self.root)
        self.assertEqual([item['id'] for item in listing['board']], ['wxy-oect-original','wxy-oesp-v22'])
        self.assertEqual(listing['board'][0]['architectures'], ['arm64'])
        self.assertEqual(listing['board'][0]['status'], 'source_prepared')

    def test_backend_script_stops_before_a_build_when_adapter_is_incomplete(self):
        output = stage(self.root, resolve(self.root, 'wxy-oect-k3s'))
        bash = Path(r'C:\Program Files\Git\bin\bash.exe')
        executable = str(bash) if bash.exists() else shutil.which('bash')
        self.assertIsNotNone(executable)
        for path in (output / 'build.sh', output / 'userpatches/customize-image.sh',
                     output / 'userpatches/config-framework.conf', output / 'userpatches/extensions/image-framework.sh'):
            subprocess.run([executable, '-n', path.as_posix()], check=True)
        customize = (output / 'userpatches/customize-image.sh').read_text(encoding='utf-8')
        embedded_python = customize.split("python3 - <<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        ast.parse(embedded_python)
        # Verify that family defaults cannot silently select another kernel.
        extension = output / 'userpatches/extensions/image-framework.sh'
        config = resolve(self.root, 'wxy-oect-k3s')
        shell = '\n'.join([
            'source "$1"', 'BOARDFAMILY=rk35xx', 'KERNELBRANCH=branch:rolling', 'BOOTCONFIG=vendor_defconfig',
            'post_family_config__999_image_framework',
            'printf "%s\\n" "$KERNELSOURCE" "$KERNELBRANCH" "$KERNEL_MAJOR_MINOR" "$BOOT_FDT_FILE" "$BOOTCONFIG"',
        ])
        pinned = subprocess.run([executable, '--noprofile', '--norc', '-c', shell, '--', extension.as_posix()], capture_output=True, text=True)
        self.assertEqual(pinned.returncode, 0, pinned.stderr)
        self.assertEqual(pinned.stdout.splitlines(), [config['soc']['kernel']['source'],
                         'commit:' + config['soc']['kernel']['commit'], '6.12', config['board']['boot']['dtb'], 'none'])
        cannot_override = subprocess.run([executable, '--noprofile', '--norc', '-c',
            'source "$1"; BOARDFAMILY=rk35xx; BRANCH=current; post_family_config__999_image_framework; extension_prepare_config__999_image_framework; BOOTCONFIG=vendor_defconfig',
            '--', extension.as_posix()], capture_output=True, text=True)
        self.assertNotEqual(cannot_override.returncode, 0)
        result = subprocess.run([executable, '--noprofile', '--norc', '-c',
                                 'PATH="/usr/bin:/bin:$PATH"; export PATH; exec bash "$1"',
                                 '--', (output / 'build.sh').as_posix()], capture_output=True)
        self.assertEqual(result.returncode, 78, result.stderr.decode('utf-8', errors='replace'))
        self.assertTrue(result.stderr)


if __name__ == '__main__':
    unittest.main()
