# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Exercise completion in Bash, including actual Readline Tab key presses."""
import importlib.util
import os
from pathlib import Path
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / 'components/common/rootfs-overlay/usr/lib/armhpc'
SCRIPT = LIBRARY / 'completion.bash'
QUERY = '''source "$1"
shift
COMP_WORDS=("$@")
COMP_CWORD=$((${#COMP_WORDS[@]}-1))
_armhpc_complete
if ((${#COMPREPLY[@]})); then printf '%s\\0' "${COMPREPLY[@]}"; fi
'''


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('bash'), 'Requires Bash on Linux')
class BashCompletionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='armhpc-completion-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = dict(os.environ, PATH=str(self.root / 'empty-path'), LC_ALL='C', TERM='xterm')
        self.env['_ARMHPC_COMPLETION_STATE_ROOT'] = str(self.root / 'state')
        self.env['_ARMHPC_COMPLETION_NET_ROOT'] = str(self.root / 'net')

    def complete(self, *words):
        result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', QUERY, 'completion', str(SCRIPT), *words],
                                env=self.env, check=True, capture_output=True, timeout=3)
        self.assertEqual(result.stderr, b'')
        return [value.decode() for value in result.stdout.split(b'\0') if value]

    def test_top_commands_match_current_public_dispatchers(self):
        spec = importlib.util.spec_from_file_location('completion_cli', LIBRARY / 'cli.py')
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        for program, table in (('armhpc', cli.NORMAL), ('armhpc-rescue', cli.RESCUE), ('armhpc-kube', cli.KUBE)):
            with self.subTest(program=program):
                self.assertEqual(set(self.complete(program, '')), set(table))
        self.assertEqual(self.complete('armhpc', 'id'), ['identify'])
        self.assertEqual(self.complete('armhpc-kube', 'ch'), ['check'])

    def test_subcommands_are_contextual_and_private_operations_are_absent(self):
        self.assertEqual(self.complete('armhpc', 'ota', 'app'), ['apply'])
        self.assertEqual(self.complete('armhpc-rescue', 'ota', 'tools', 'up'), ['update'])
        self.assertEqual(set(self.complete('armhpc', 'ota', 'source', '')), {'show', 'set', 'test'})
        self.assertEqual(set(self.complete('armhpc-rescue', 'ota', 'boot', '')), {'plan', 'enter', 'apply', 'status', 'rollback', 'restore', 'resume-system'})
        self.assertEqual(self.complete('armhpc-rescue','ota','boot','restore','--exec'),['--execute'])
        self.assertIn('--request-id', self.complete('armhpc-rescue','ota','boot','resume-system','--'))
        self.assertIn('storage', self.complete('armhpc', 'ota', ''))
        self.assertEqual(set(self.complete('armhpc-rescue', 'ota', 'storage', '')), {'status', 'export', 'acknowledge', 'plan', 'cleanup', 'stage'})
        self.assertNotIn('_service', self.complete('armhpc', 'ota', ''))
        self.assertNotIn('prepare', self.complete('armhpc', 'watchdog', ''))
        self.assertIn('prepare', self.complete('armhpc-rescue', 'watchdog', ''))

    def test_hostname_and_network_actions_and_parameters(self):
        self.assertEqual(self.complete('armhpc','hostname','co'),['configure'])
        self.assertEqual(set(self.complete('armhpc','network','')),{'show','set','configure','confirm','rollback'})
        self.assertNotIn('_service',self.complete('armhpc','network',''))
        self.assertIn('--address',self.complete('armhpc','network','set','--'))
        self.assertNotIn('--address',self.complete('armhpc','network','confirm','--'))
        self.assertIn('--transaction',self.complete('armhpc','network','confirm','--'))
        self.assertEqual(self.complete('armhpc','network','set','--rollback-after','9'),['90'])
        self.assertNotIn('hostname',self.complete('armhpc-rescue',''))

    def test_public_options_and_common_flags_before_actions(self):
        self.assertEqual(self.complete('armhpc', 'identify', '--inter'), ['--interface'])
        self.assertIn('--target', self.complete('armhpc-rescue', 'ota', '--json', 'apply', '--'))
        self.assertIn('--bundle', self.complete('armhpc-rescue', 'ota', 'tools', '--json', 'update', '--'))
        options = self.complete('armhpc', 'identify', '--')
        self.assertNotIn('--worker', options)
        for private in ('--simulate-root', '--config', '--daemon'):
            self.assertNotIn(private, self.complete('armhpc', 'ota', 'apply', '--'))

    def test_values_support_separate_inline_and_bash_wordbreak_equals(self):
        for words, expected in (
            (('armhpc', 'ota', 'apply', '--target', 's'), ['system']),
            (('armhpc', 'ota', 'apply', '--target=sy'), ['--target=system']),
            (('armhpc', 'ota', 'apply', '--target', '=', 'sy'), ['system']),
            (('armhpc', 'led', '--heartbeat', 'o'), ['on', 'off']),
            (('armhpc-rescue', 'ota', 'receive', '--phase', 'c'), ['commit']),
            (('armhpc-rescue', 'watchdog', 'prepare', '--sata', 'a'), ['absent']),
        ):
            with self.subTest(words=words):
                self.assertEqual(self.complete(*words), expected)

    def test_value_arguments_do_not_become_positional_commands(self):
        self.assertIn('--target', self.complete('armhpc', 'ota', 'apply', '--release', 'current', '--'))
        self.assertIn('--bundle', self.complete('armhpc-rescue', 'ota', 'boot', '--request-id', '=', 'abc', 'apply', '--'))
        self.assertNotIn('--seconds', self.complete('armhpc', 'identify', '--seconds', '30', '--'))
        self.assertIn('--origin', self.complete('armhpc', 'ota', 'source', 'set', '--origin', 'http://host', '--'))
        self.assertEqual(self.complete('armhpc', 'identify', '--seconds', ''), ['30', '60', '120', '300'])

    def test_files_with_spaces_and_directory_only_arguments(self):
        package = self.root / 'maintenance package.tar.xz'
        package.touch()
        directory = self.root / 'maintenance root'
        directory.mkdir()
        choices = self.complete('armhpc-rescue', 'ota', 'tools', 'update', '--bundle', str(self.root / 'maint'))
        self.assertEqual(set(choices), {str(package), str(directory)})
        self.assertEqual(self.complete('armhpc', 'firstrun', '--rootfs', str(self.root / 'maint')), [str(directory)])

    def test_interface_and_local_id_values_are_read_without_invoking_commands(self):
        net = self.root / 'net'
        (net / 'eth0/device').mkdir(parents=True)
        (net / 'enp1s0/device').mkdir(parents=True)
        (net / 'lo').mkdir()
        self.assertEqual(self.complete('armhpc', 'identify', '--interface', 'e'), ['enp1s0', 'eth0'])
        state = self.root / 'state'
        job = '11111111-2222-3333-4444-555555555555'
        for directory, filename in (('manifests', 'release-1.json'), ('jobs', job + '.json'), ('watchdog', job + '.json')):
            p = state / directory / filename
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{}')
        (state / 'watchdog/latest.json').write_text('{}')
        (state / 'manifests/evil$(touch OWNED).json').write_text('{}')
        self.assertEqual(self.complete('armhpc', 'ota', 'apply', '--release', 'r'), ['release-1'])
        self.assertEqual(self.complete('armhpc', 'ota', 'status', '--job', ''), [job])
        self.assertEqual(self.complete('armhpc-rescue', 'watchdog', 'test', '--test-id', ''), [job])
        self.assertFalse((self.root / 'OWNED').exists())

    def test_absolute_entrypoint_uses_the_same_command_group(self):
        self.assertEqual(self.complete('/usr/local/sbin/armhpc-rescue', 'ota', 'rec'), ['recover', 'receive'])

    def test_syntax_and_loader_registration(self):
        subprocess.run(['/bin/bash', '-n', str(SCRIPT)], check=True)
        loaded = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c',
                                 'source "$1"; complete -p armhpc armhpc-rescue armhpc-kube', 'completion', str(SCRIPT)],
                                check=True, capture_output=True, text=True, env=self.env)
        self.assertEqual(loaded.stdout.count('-F _armhpc_complete'), 3)
        hook = ROOT / 'components/common/rootfs-overlay/etc/profile.d/armhpc-completion.sh'
        subprocess.run(['/bin/dash', '-n', str(hook)], check=True)
        subprocess.run(['/bin/dash', '-c', '. "$1"', 'completion', str(hook)], check=True, env=self.env)

    def test_actual_readline_tab_completion_and_space_escaping(self):
        import pty
        rc = self.root / 'bashrc'
        rc.write_text('source ' + shlex.quote(str(SCRIPT)) + '''
PS1='COMP> '
armhpc() { printf '\\nCAPTURE\\n'; printf '<%s>\\n' "$@"; printf 'DONE\\n'; }
armhpc-rescue() { armhpc "$@"; }
''')
        package = self.root / 'maintenance package.tar.xz'
        package.touch()
        master, slave = pty.openpty()
        process = subprocess.Popen(['/bin/bash', '--noprofile', '--rcfile', str(rc), '-i'],
                                   stdin=slave, stdout=slave, stderr=slave, env=self.env, start_new_session=True)
        os.close(slave)
        def until(marker):
            content = bytearray()
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.05)
                if ready:
                    content.extend(os.read(master, 8192))
                    if marker in content:
                        return bytes(content)
            self.fail('Readline did not return: ' + repr(bytes(content)))
        try:
            until(b'COMP> ')
            for line, expected in (
                ('armhpc id\t\n', b'<identify>'),
                ('armhpc ota apply --target=sy\t\n', b'<--target=system>'),
                ('armhpc-rescue ota tools update --bundle ' + str(self.root / 'maint') + '\t\n',
                 ('<' + str(package) + '>').encode()),
            ):
                os.write(master, line.encode())
                output = until(b'DONE')
                self.assertIn(expected, output)
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            os.close(master)


if __name__ == '__main__':
    unittest.main()
