# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Keep the environment marker and public entrypoints consistent with the role."""
from pathlib import Path

RETIRED=(
    'armhpc-configuration','armhpc-firstrun','armhpc-led','armhpc-phy-led',
    'armhpc-k3s-configuration','armhpc-k3s-check','armhpc-rescue-boot',
    'armhpc-rescue-configuration','armhpc-rescue-status','armhpc-rescue-install',
    'armhpc-rescue-restore','armhpc-rescue-provision','armhpc-rescue-console',
    'node-setup','k3s-setup','k3s-preflight','node-install-admin','oect-setup','oect-k3s-preflight',
)
GROUPS=('armhpc','armhpc-kube','armhpc-rescue')


def environment_marker(root):
    root=Path(root).resolve(strict=True)
    path=root/'etc/armhpc/rescue-environment'
    if not path.parent.resolve().is_relative_to(root) or path.is_symlink():
        raise ValueError('Environment marker escapes root or is a symbolic link')
    if path.exists() and not path.is_file():
        raise ValueError('Environment marker must be an ordinary file')
    return path


def validate_environment(root,mode):
    if mode not in ('normal','rescue'):raise ValueError('Unknown command layout mode')
    marker=environment_marker(root)
    if marker.is_file() != (mode=='rescue'):
        raise ValueError('Root environment marker does not match '+mode+': '+str(root))


def set_environment(root,mode):
    if mode not in ('normal','rescue'):raise ValueError('Unknown command layout mode')
    marker=environment_marker(root)
    if mode=='rescue':
        marker.parent.mkdir(parents=True,exist_ok=True)
        if not marker.exists():
            marker.write_text('ARMHPC Rescue environment\n',encoding='ascii')
        marker.chmod(0o644)
    else:
        marker.unlink(missing_ok=True)
    validate_environment(root,mode)


def migrate_rescue_resources(root):
    """Migrate framework-owned getty drop-ins when reusing an older rootfs."""
    root=Path(root).resolve(strict=True)
    changed=[]
    base=root/'etc/systemd/system'
    for path in base.glob('*.service.d/10-armhpc-rescue.conf'):
        if not path.parent.name.startswith(('serial-getty@','getty@')):continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Rescue getty drop-in escapes root')
        text=path.read_text(encoding='utf-8')
        old='/usr/local/sbin/armhpc-rescue-console'
        if old not in text:continue
        if '--login-options' in text:
            raise ValueError('Custom Rescue login options need explicit migration')
        updated=text.replace(old,'/usr/local/sbin/armhpc-rescue --login-options console')
        path.write_text(updated,encoding='utf-8')
        changed.append(path.relative_to(root).as_posix())
    motd=root/'etc/motd'
    if motd.is_file():
        if motd.is_symlink() or not motd.resolve().is_relative_to(root):
            raise ValueError('Rescue MOTD escapes root')
        text=motd.read_text(encoding='utf-8')
        updated=text.replace('Use armhpc-rescue-configuration or armhpc --help.',
                             'Use armhpc-rescue configuration or armhpc-rescue --help.')
        if updated!=text:
            motd.write_text(updated,encoding='utf-8')
            changed.append('etc/motd')
    return changed

def enable_usb_console(root):
    import json
    root=Path(root)
    config=root/'etc/armhpc/usb-console.json'
    if not config.is_file():return []
    policy=json.loads(config.read_text())
    if not policy.get('enabled') or not policy.get('automatic_start',True):return []
    for relative in ('usr/lib/armhpc/usb_console.py','etc/systemd/system/armhpc-usb-console.service'):
        if not (root/relative).is_file():raise ValueError('Missing USB console resource: '+relative)
    wanted=root/'etc/systemd/system/multi-user.target.wants'
    wanted.mkdir(parents=True,exist_ok=True)
    for name,target in (('armhpc-usb-console.service','../armhpc-usb-console.service'),
                        ('serial-getty@ttyGS0.service','/lib/systemd/system/serial-getty@.service')):
        link=wanted/name
        if link.is_symlink():
            if link.readlink()!=Path(target):raise ValueError('USB console service enable conflict')
        elif link.exists():raise ValueError('USB console service enable conflict')
        else:link.symlink_to(target)
    return ['armhpc-usb-console.service','serial-getty@ttyGS0.service']

def finalize(root,mode):
    root=Path(root).resolve(strict=True)
    if mode not in ('normal','rescue'):raise ValueError('Unknown command layout mode')
    environment_marker(root)
    kept=('armhpc','armhpc-kube') if mode=='normal' else ('armhpc-rescue',)
    removed=[]
    for directory in ('usr/local/bin','usr/local/sbin'):
        base=root/directory
        if not base.resolve().is_relative_to(root):raise ValueError('Command directory escapes root')
        for name in (*RETIRED,*(name for name in GROUPS if name not in kept)):
            path=base/name
            if not path.exists() and not path.is_symlink():continue
            if path.is_dir() and not path.is_symlink():raise ValueError('Command path is a directory: '+str(path))
            path.unlink();removed.append(path.relative_to(root).as_posix())
    for name in kept:
        target=root/'usr/local/sbin'/name
        if not target.is_file():
            if name=='armhpc-kube':continue
            raise ValueError('Required entrypoint is missing: '+name)
        if target.is_symlink():raise ValueError('Public entrypoint must be an ordinary file')
        target.chmod(0o755)
        alias=root/'usr/local/bin'/name
        alias.parent.mkdir(parents=True,exist_ok=True)
        wanted=Path('../sbin')/name
        if alias.exists() or alias.is_symlink():
            if not alias.is_symlink() or alias.readlink()!=wanted:raise ValueError('Command alias conflict: '+str(alias))
        else:alias.symlink_to(wanted)
    # Existing nodes may retain the old generated ExecStartPre. Preserve every
    # other line and migrate only the two framework-owned drop-in paths.
    for service in ('k3s','k3s-agent'):
        path=root/'etc/systemd/system'/(service+'.service.d/10-image-framework-storage.conf')
        if not path.is_file():continue
        if path.is_symlink() or not path.parent.resolve().is_relative_to(root):raise ValueError('Unsafe K3s drop-in path')
        text=path.read_text()
        updated=text.replace('ExecStartPre=/usr/local/sbin/armhpc-k3s-check\n','ExecStartPre=/usr/local/sbin/armhpc-kube check\n')
        if updated!=text:path.write_text(updated)
    migrated=migrate_rescue_resources(root) if mode=='rescue' else []
    enabled=enable_usb_console(root)
    set_environment(root,mode)
    return {'mode':mode,'entrypoints':[name for name in kept if (root/'usr/local/sbin'/name).is_file()],'removed':removed,'migrated_resources':migrated,'usb_console_services':enabled}
