# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Durable configuration writes and serialization with the OTA writer."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


class ConfigError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise ConfigError(code, message)


def safe_path(root, relative):
    root = Path(root).resolve()
    relative = Path(relative)
    require(not relative.is_absolute() and '..' not in relative.parts, 'configuration_path', 'Use a root-relative configuration path')
    path = root / relative
    require(path.is_relative_to(root), 'configuration_path', 'Configuration path escapes its root')
    require(not any(item.is_symlink() for item in (path, *path.parents) if item != root and item.is_relative_to(root)),
            'configuration_path', 'Configuration files and their parents must not be symbolic links')
    return path


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, content, mode=0o644):
    path = Path(path)
    require(not path.is_symlink(), 'configuration_path', 'Refusing a symbolic-link configuration file')
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.stat() if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, mode)
        if old:
            os.fchown(fd, old.st_uid, old.st_gid)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content.encode('utf-8') if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n', 0o600)


def read_json(path, default=None):
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        require(isinstance(value, dict), 'configuration_state', 'Configuration record must be an object')
        return value
    except (ValueError, OSError) as error:
        raise ConfigError('configuration_state', 'Cannot read configuration record: ' + str(path)) from error


def run(*arguments, timeout=15):
    result = subprocess.run(list(map(str, arguments)), capture_output=True, text=True,
                            timeout=timeout, env={**os.environ, 'LC_ALL': 'C'})
    require(result.returncode == 0, 'configuration_command',
            'Command failed: ' + str(arguments[0]) + ': ' + result.stderr.strip()[-500:])
    return result.stdout.strip()


def normal_system(root):
    require(not (Path(root) / 'etc/armhpc/rescue-environment').is_file(),
            'configuration_mode', 'Hostname and network settings are managed in the normal system')
    if Path(root) == Path('/'):
        require(sys.platform == 'linux' and os.geteuid() == 0, 'root_required', 'Run configuration changes as root')


@contextmanager
def configuration_lock(root):
    path = safe_path(root, 'run/lock/armhpc-ota-writer.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ConfigError('configuration_busy', 'Another configuration or OTA operation holds the device lock') from error
        yield


def require_idle(root):
    if Path(root) != Path('/') or not Path('/usr/lib/armhpc-ota/armhpc_ota').is_dir():
        return
    sys.path.insert(0, '/usr/lib/armhpc-ota')
    from armhpc_ota.config import Config
    from armhpc_ota.store import Store, TERMINAL
    from armhpc_ota.maintenance import require_complete
    from armhpc_ota.watchdog import require_idle as watchdog_idle
    from armhpc_ota.boot_maintenance import require_idle as boot_idle
    config = Config.load()
    job = Store(config.state).read()
    require(not job or job['stage'] in TERMINAL, 'configuration_busy', 'Finish the active OTA task before changing settings')
    require_complete(config)
    watchdog_idle(config)
    boot_idle(config)


def emit(value, json_output=False):
    if json_output:
        print(json.dumps({'protocol': 1, 'ok': True, 'data': value}, ensure_ascii=False))
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def entry(function):
    try:
        return function()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        if '--json' in sys.argv:
            print(json.dumps({'protocol': 1, 'ok': False, 'error': {
                'code': getattr(error, 'code', 'configuration_io'), 'message': str(error)}}, ensure_ascii=False))
        else:
            print(str(error), file=sys.stderr)
        return 2


def approve(yes, message):
    if not yes:
        require(sys.stdin.isatty(), 'confirmation_required', 'Use --yes for a noninteractive configuration change')
        require(input(message + ' [y/N]: ').strip().lower() in ('y', 'yes'), 'cancelled', 'Configuration cancelled')
