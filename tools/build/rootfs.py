# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Rootfs preparation helpers shared by the paired OECT build pipeline."""
from contextlib import contextmanager
import hashlib,importlib.util,json,os,shutil,subprocess
from pathlib import Path
def require(value, message):
    if not value:
        raise RuntimeError(message)


def run(*args, capture=False, **options):
    result = subprocess.run(list(map(str, args)), check=True, text=True,
                            capture_output=capture, **options)
    return result.stdout.strip() if capture else None


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def mounted(device, directory, readonly=False):
    run('mount', '-t', 'ext4', '-o', 'ro,noload' if readonly else 'rw', device, directory)
    try:
        yield Path(directory)
    finally:
        run('umount', directory)


@contextmanager
def chroot_mounts(root):
    mounted_paths = []
    try:
        for name in ('dev', 'proc', 'sys'):
            target = root/name
            target.mkdir(exist_ok=True)
            require(subprocess.run(['mountpoint', '-q', target]).returncode != 0, 'Build mount already active')
            run('mount', '--bind', '/'+name, target)
            mounted_paths.append(target)
        yield
    finally:
        for path in reversed(mounted_paths):
            run('umount', path)


def overlay(root, source):
    for path in source.rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            require(not path.is_symlink(), 'Source overlay contains a symlink')
            relative = path.relative_to(source)
            target = root/relative
            require(not target.is_symlink(), 'Overlay target is a symlink: '+str(relative))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            executable = str(relative).startswith(('usr/local/sbin/', 'etc/initramfs-tools/hooks/', 'etc/initramfs-tools/scripts/'))
            target.chmod(0o755 if executable else 0o644)


def reset_identity(root):
    (root/'etc/machine-id').write_text('')
    for relative in ('etc/image-framework/admin-provisioned.json', 'etc/image-framework/admin-provisioning.json',
                     'var/lib/systemd/random-seed', 'var/lib/armhpc/network-identity.json',
                     'root/.armhpc-rescue-wizard-seen', 'etc/armhpc/force-rescue'):
        (root/relative).unlink(missing_ok=True)
    for key in (root/'etc/ssh').glob('ssh_host_*'):
        key.unlink()
    for folder in ('run', 'dev', 'proc', 'sys', 'tmp'):
        directory = root/folder
        for path in directory.iterdir():
            if path.name in ('pts', 'shm', 'mqueue') and path.is_dir():
                continue
            if path.is_dir() and not path.is_symlink():
                require(path.resolve().is_relative_to(root.resolve()), 'Cleanup escaped the build root')
                shutil.rmtree(path)
            else:
                path.unlink()
