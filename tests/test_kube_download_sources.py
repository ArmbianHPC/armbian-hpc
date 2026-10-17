# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import copy
import hashlib
import importlib.util
import io
import ssl
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'kube_download_sources', ROOT / 'components/k3s/rootfs-overlay/usr/lib/armhpc/kube_setup.py')
kube = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kube)


def source(china=False):
    return {'kind': 'mirror' if china else 'official', 'registry_mirrors': {},
            'installer_url': kube.RANCHER_CN_INSTALLER if china else 'https://example.invalid/install.sh',
            'artifact_url': kube.RANCHER_CN_ARTIFACTS if china else 'https://example.invalid/releases'}


class Response(io.BytesIO):
    def __init__(self, value, url='https://example.invalid/install.sh'):
        super().__init__(value)
        self.url = url

    def geturl(self):
        return self.url


class KubeDownloadSourceTests(unittest.TestCase):
    def test_editing_a_failed_empty_install_keeps_saved_cluster_defaults(self):
        policy = {'display_name': 'node', 'default_version': 'v1.36.5+k3s1',
                  'installer_url': source()['installer_url'], 'artifact_url': source()['artifact_url']}
        saved = {'role': 'server-init', 'node_name': 'chosen-name', 'version': 'v1.36.4+k3s1',
                 'server': '', 'download_source': source(),
                 'server_options': {'cluster-cidr': '10.52.0.0/16', 'service-cidr': '10.53.0.0/16',
                                    'cluster-dns': '10.53.0.10', 'cluster-domain': 'cluster.internal',
                                    'flannel-backend': 'vxlan', 'secrets-encryption': True,
                                    'disable': 'traefik', 'tls-san': 'api.example.invalid'}}
        previous = copy.deepcopy(saved)
        def ask(prompt, default=None, validator=None):
            return '3' if prompt == '下载来源' else str(default)
        def yes(prompt, default=False):
            return default
        with patch.object(kube, 'ask', side_effect=ask), patch.object(kube, 'yes', side_effect=yes), redirect_stdout(io.StringIO()):
            plan, token = kube.collect_plan(policy, saved)
        self.assertEqual(previous, saved)
        self.assertEqual('server-init', plan['role'])
        self.assertEqual('chosen-name', plan['node_name'])
        self.assertEqual('v1.36.4+k3s1', plan['version'])
        self.assertEqual(saved['server_options'], plan['server_options'])
        self.assertEqual(source(True), plan['download_source'])
        self.assertIsNone(token)

    def test_china_source_uses_its_pin_without_mutating_upstream_policy(self):
        policy = {'installer_url': 'https://example.invalid/install.sh', 'installer_sha256': 'a' * 64}
        previous = dict(policy)
        selected = kube.installer_settings(policy, source(True))
        self.assertEqual(kube.RANCHER_CN_INSTALLER_SHA256, selected['installer_sha256'])
        self.assertEqual(kube.RANCHER_CN_INSTALLER, selected['installer_url'])
        self.assertEqual(previous, policy)

    def test_custom_source_keeps_upstream_pin(self):
        policy = {'installer_sha256': 'a' * 64}
        selected = source()
        selected.update(kind='mirror', installer_url='https://mirror.invalid/install.sh')
        self.assertEqual('a' * 64, kube.installer_settings(policy, selected)['installer_sha256'])

    def test_native_script_requires_the_matching_artifact_origin(self):
        value = source(True)
        value['artifact_url'] = 'https://other.invalid/releases'
        with self.assertRaises(kube.SetupError):
            kube.validate_download_source(value)

    def test_native_script_cannot_be_mislabeled_as_the_upstream_source(self):
        value = source(True)
        value['kind'] = 'official'
        with self.assertRaises(kube.SetupError):
            kube.validate_download_source(value)

    def test_lookalike_host_does_not_receive_native_environment_or_pin(self):
        value = source(True)
        value['installer_url'] = 'https://rancher-mirror.rancher.cn.other.invalid/k3s/k3s-install.sh'
        self.assertNotIn('INSTALL_K3S_MIRROR', kube.download_environment(value))
        self.assertEqual('a' * 64, kube.installer_settings({'installer_sha256': 'a' * 64}, value)['installer_sha256'])

    def test_china_environment_enables_the_native_version_path_translation(self):
        self.assertEqual({'INSTALL_K3S_ARTIFACT_URL': kube.RANCHER_CN_ARTIFACTS,
                          'INSTALL_K3S_MIRROR': 'cn',
                          'INSTALL_K3S_MIRROR_URL': 'https://rancher-mirror.rancher.cn'},
                         kube.download_environment(source(True)))

    def test_upstream_environment_has_no_native_mirror_flags(self):
        self.assertEqual({'INSTALL_K3S_ARTIFACT_URL': source()['artifact_url']},
                         kube.download_environment(source()))

    def test_menu_three_uses_canonical_urls_and_keeps_the_existing_record_shape(self):
        policy = {'installer_url': source()['installer_url'], 'artifact_url': source()['artifact_url']}
        with patch.object(kube, 'ask', return_value='3') as ask, patch.object(kube, 'yes', return_value=False), redirect_stdout(io.StringIO()):
            value = kube.collect_download_source(policy)
        self.assertEqual(source(True), value)
        self.assertEqual(1, ask.call_count)
        self.assertEqual('1', ask.call_args.args[1])

    def test_a_saved_china_source_selects_menu_three_again(self):
        policy = {'installer_url': source()['installer_url'], 'artifact_url': source()['artifact_url']}
        with patch.object(kube, 'ask', return_value='3') as ask, patch.object(kube, 'yes', return_value=False), redirect_stdout(io.StringIO()):
            kube.collect_download_source(policy, source(True))
        self.assertEqual('3', ask.call_args.args[1])

    def test_legacy_custom_menu_two_and_registry_mapping_remain_available(self):
        policy = {'installer_url': source()['installer_url'], 'artifact_url': source()['artifact_url']}
        answers = ['2', 'https://custom.invalid/install.sh', 'https://custom.invalid/releases/', 'docker.io', 'https://registry.invalid']
        with patch.object(kube, 'ask', side_effect=answers), patch.object(kube, 'yes', side_effect=[True,False,False]), redirect_stdout(io.StringIO()):
            value = kube.collect_download_source(policy)
        self.assertEqual('https://custom.invalid/releases', value['artifact_url'])
        self.assertEqual({'docker.io': ['https://registry.invalid']}, value['registry_mirrors'])

    def test_install_keeps_the_selected_etcd_configuration_and_exact_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = {'configuration_dir': str(root / 'config'), 'state_file': str(root / 'state.json'),
                      'completion_marker': str(root / 'complete'), 'installer_arch': 'arm64',
                      'installer_sha256': 'a' * 64, 'data_dir': '/data/k3s'}
            plan = {'role': 'server-init', 'node_name': 'node-one', 'version': 'v1.36.5+k3s1',
                    'server': '', 'server_options': {'secrets-encryption': True, 'cluster-cidr': '10.42.0.0/16'},
                    'download_source': source(True)}
            original = copy.deepcopy(plan)
            state = {'plan': plan, 'status': 'failed'}
            actual_temp = tempfile.TemporaryDirectory
            captured = []
            def run(arguments, **kwargs):
                if arguments[0] == 'sh':
                    captured.append(kwargs['env'])
                return ''
            def download(selected, destination):
                self.assertEqual(kube.RANCHER_CN_INSTALLER_SHA256, selected['installer_sha256'])
                Path(destination).write_text('#!/bin/sh\n')
            with patch.object(kube, 'prepare_storage', return_value='k3s'), patch.object(kube, 'run', side_effect=run), \
                 patch.object(kube, 'wait_for_service'), patch.object(kube, 'download_installer', side_effect=download), \
                 patch.object(kube,'management_status',return_value={'ready':False}), \
                 patch.object(kube,'prepare_recovery_resources',return_value={'version':plan['version']}), \
                 patch.object(kube,'capture_installation'), \
                 patch.object(kube.tempfile, 'TemporaryDirectory', side_effect=lambda **kwargs: actual_temp(dir=root)), redirect_stdout(io.StringIO()):
                kube.install(policy, state, None)
            self.assertEqual(original, state['plan'])
            self.assertEqual('installed', state['status'])
            self.assertEqual('v1.36.5+k3s1', captured[0]['INSTALL_K3S_VERSION'])
            self.assertEqual('server', captured[0]['INSTALL_K3S_EXEC'])
            self.assertEqual('true', captured[0]['INSTALL_K3S_SKIP_START'])
            self.assertEqual('true',captured[0]['INSTALL_K3S_SKIP_DOWNLOAD'])
            self.assertEqual(plan['version'],state['artifacts']['version'])
            self.assertEqual('cn', captured[0]['INSTALL_K3S_MIRROR'])
            self.assertEqual('https://rancher-mirror.rancher.cn', captured[0]['INSTALL_K3S_MIRROR_URL'])
            self.assertEqual(policy['configuration_dir'] + '/config.yaml', captured[0]['K3S_CONFIG_FILE'])


class InstallerDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.destination = self.directory / 'install.sh'
        self.content = b'#!/bin/sh\necho verified\n'
        self.policy = {'installer_url': 'https://example.invalid/install.sh',
                       'installer_sha256': hashlib.sha256(self.content).hexdigest()}

    def download(self, side_effect):
        with patch.object(kube.urllib.request, 'urlopen', side_effect=side_effect) as network, \
             patch.object(kube.time, 'sleep') as sleep, redirect_stdout(io.StringIO()):
            kube.download_installer(self.policy, self.destination)
        return network, sleep

    def test_timeout_then_success_retries_and_only_commits_verified_bytes(self):
        network, sleep = self.download([TimeoutError('read timed out'), Response(self.content)])
        self.assertEqual(2, network.call_count)
        sleep.assert_called_once_with(1)
        self.assertEqual(self.content, self.destination.read_bytes())
        self.assertEqual([], list(self.directory.glob('.install-*')))

    def test_partial_timeout_is_removed_before_a_fresh_attempt(self):
        class Broken(Response):
            def read(self, size=-1):
                if self.tell() > 0:
                    raise TimeoutError('interrupted body')
                return super().read(4)
        self.download([Broken(self.content), Response(self.content)])
        self.assertEqual(self.content, self.destination.read_bytes())
        self.assertEqual([], list(self.directory.glob('.install-*')))

    def test_all_timeouts_are_bounded_and_report_the_failed_origin(self):
        with patch.object(kube.urllib.request, 'urlopen', side_effect=TimeoutError('timeout')) as network, \
             patch.object(kube.time, 'sleep') as sleep, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(kube.SetupError, 'example.invalid/install.sh'):
                kube.download_installer(self.policy, self.destination)
        self.assertEqual(3, network.call_count)
        self.assertEqual([1, 3], [item.args[0] for item in sleep.call_args_list])
        self.assertFalse(self.destination.exists())

    def test_checksum_failure_does_not_retry_or_overwrite_the_previous_file(self):
        self.destination.write_bytes(b'previous')
        with patch.object(kube.urllib.request, 'urlopen', return_value=Response(b'wrong')) as network, \
             patch.object(kube.time, 'sleep') as sleep, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(kube.SetupError, 'SHA256'):
                kube.download_installer(self.policy, self.destination)
        self.assertEqual(1, network.call_count)
        sleep.assert_not_called()
        self.assertEqual(b'previous', self.destination.read_bytes())
        self.assertEqual([], list(self.directory.glob('.install-*')))

    def test_oversize_script_is_refused_without_committing_a_destination(self):
        with self.assertRaisesRegex(kube.SetupError, '超过预期大小'):
            self.download([Response(b'x' * (1024 * 1024 + 1))])
        self.assertFalse(self.destination.exists())

    def test_http_404_is_not_retried(self):
        error = urllib.error.HTTPError(self.policy['installer_url'], 404, 'not found', {}, None)
        with patch.object(kube.urllib.request, 'urlopen', side_effect=error) as network, redirect_stdout(io.StringIO()):
            with self.assertRaises(kube.SetupError):
                kube.download_installer(self.policy, self.destination)
        self.assertEqual(1, network.call_count)

    def test_http_503_is_retried(self):
        error = urllib.error.HTTPError(self.policy['installer_url'], 503, 'unavailable', {}, None)
        network, _ = self.download([error, Response(self.content)])
        self.assertEqual(2, network.call_count)

    def test_certificate_errors_are_refused_without_retry(self):
        error = urllib.error.URLError(ssl.SSLCertVerificationError('certificate invalid'))
        with patch.object(kube.urllib.request, 'urlopen', side_effect=error) as network, redirect_stdout(io.StringIO()):
            with self.assertRaises(kube.SetupError):
                kube.download_installer(self.policy, self.destination)
        self.assertEqual(1, network.call_count)

    def test_https_to_http_redirect_is_refused(self):
        with self.assertRaises(ValueError):
            self.download([Response(self.content, 'http://example.invalid/install.sh')])
        self.assertFalse(self.destination.exists())

    def test_local_file_permission_failure_is_not_retried(self):
        with patch.object(kube.urllib.request, 'urlopen', return_value=Response(self.content)) as network, \
             patch.object(kube.tempfile, 'NamedTemporaryFile', side_effect=PermissionError('not writable')), redirect_stdout(io.StringIO()):
            with self.assertRaises(kube.SetupError):
                kube.download_installer(self.policy, self.destination)
        self.assertEqual(1, network.call_count)


if __name__ == '__main__':
    unittest.main()
