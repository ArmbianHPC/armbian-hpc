#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Assemble a rootfs artifact from a prepared chroot and matching kernel packages.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C.UTF-8
root=$(realpath -e -- "${1:?Supply the prepared rootfs directory}")
stage=$(realpath -e -- "${2:?Supply the validated framework stage directory}")
debs=$(realpath -e -- "${3:?Supply the compiled kernel package directory}")
output=$(realpath -e -- "${4:?Supply an existing artifact output directory}")
test "$EUID" -eq 0
test "$root" != /
test -f "$root/.armhpc-customized"
test ! -e "$stage/.incomplete"
test ! -e "$output/factory-rootfs.tar.zst"
case "$output/" in "$root/"*) echo 'Output must be outside the rootfs.' >&2; exit 2;; esac
if mountpoint -q "$root"; then
    echo 'Build in an ordinary chroot directory, not a mounted disk partition.' >&2
    exit 2
fi
test -f "$stage/rootfs-overlay/etc/image-framework/firmware-policy.json"
mkdir -p "$output/kernel"
cleanup() {
    for target in dev/pts dev proc sys; do
        if mountpoint -q "$root/$target"; then umount "$root/$target"; fi
    done
}
trap cleanup EXIT
mkdir -p "$root/dev/pts" "$root/proc" "$root/sys"
mount --bind /dev "$root/dev"
mount --bind /dev/pts "$root/dev/pts"
mount -t proc proc "$root/proc"
mount -t sysfs sysfs "$root/sys"
mapfile -t image_packages < <(find "$debs" -maxdepth 1 -type f -name 'linux-image-current-rockchip64_*_arm64*.deb' -print)
mapfile -t dtb_packages < <(find "$debs" -maxdepth 1 -type f -name 'linux-dtb-current-rockchip64_*_arm64*.deb' -print)
test "${#image_packages[@]}" -eq 1
test "${#dtb_packages[@]}" -eq 1
mkdir -p "$root/tmp/armhpc-kernel-packages" "$root/tmp/overlay"
cp "${image_packages[0]}" "${dtb_packages[0]}" "$root/tmp/armhpc-kernel-packages/"
cp -a "$stage/rootfs-overlay/." "$root/tmp/overlay/"
if [[ -f "$root/boot/initrd.img-6.12.111-current-rockchip64" ]] \
    && cmp -s <(dpkg-deb --ctrl-tarfile "${image_packages[0]}" | tar -xOf - ./md5sums) \
        "$root/var/lib/dpkg/info/linux-image-current-rockchip64.md5sums" \
    && cmp -s <(dpkg-deb --ctrl-tarfile "${dtb_packages[0]}" | tar -xOf - ./md5sums) \
        "$root/var/lib/dpkg/info/linux-dtb-current-rockchip64.md5sums" \
    && [[ -z "$(chroot "$root" dpkg -V linux-image-current-rockchip64 linux-dtb-current-rockchip64)" ]]; then
    echo 'Matching kernel packages and initramfs are already installed.'
else
    chroot "$root" /usr/bin/env DEBIAN_FRONTEND=noninteractive /bin/bash -c \
        'dpkg -i /tmp/armhpc-kernel-packages/*.deb'
fi
kernel_release=$(basename -- "$(find "$root/lib/modules" -mindepth 1 -maxdepth 1 -type d -print)")
[[ "$kernel_release" =~ ^6\.12\.111-current-rockchip64$ ]]
chroot "$root" depmod "$kernel_release"
test -s "$root/boot/initrd.img-$kernel_release"
# Upstream's base cache installs these build tools. They are unnecessary in this
# runtime artifact and are excluded by the framework's rootfs payload policy.
chroot "$root" /usr/bin/env DEBIAN_FRONTEND=noninteractive \
    apt-get purge --yes u-boot-tools wireless-regdb
rm -f -- "$root/var/cache/apt/pkgcache.bin" "$root/var/cache/apt/srcpkgcache.bin"
for name in initrd.img initrd.img.old vmlinuz vmlinuz.old; do
    if [[ -L "$root/$name" ]]; then rm -f -- "$root/$name"; fi
done
cp "$stage/userpatches/customize-image.sh" "$root/tmp/armhpc-customize-image.sh"
chroot "$root" /bin/bash /tmp/armhpc-customize-image.sh
if [[ -x "$root/etc/initramfs-tools/hooks/armhpc-root-selection" ]]; then
    chroot "$root" update-initramfs -u -k "$kernel_release"
fi
python3 - "$root" "$stage" <<'PY'
import json
from pathlib import Path
import sys
root,stage=map(Path,sys.argv[1:])
layout=json.loads((stage/'storage-layout.json').read_text())
parts={p['role']:p for p in layout['external']['partitions']}
boot=next(p for p in layout['boot_disk']['partitions'] if p['role']=='boot')
(root/'etc/fstab').write_text(
    f"LABEL={parts['root']['label']} / ext4 defaults,noatime 0 1\n"
    f"LABEL={boot['label']} /boot ext4 ro,defaults,nofail 0 2\n"
    f"LABEL={parts['data']['label']} /data ext4 defaults,nofail,x-systemd.device-timeout=15s 0 2\n")
(root/'data').mkdir(exist_ok=True)
# These identities are created separately on each installed node.
(root/'etc/machine-id').write_text('')
for path in (root/'etc/ssh').glob('ssh_host_*'):
    path.unlink()
for name in ('var/lib/systemd/random-seed', 'etc/image-framework/admin-provisioned.json'):
    path=root/name
    if path.is_file() or path.is_symlink():
        path.unlink()
# Boot artifacts travel separately for the eMMC BOOT filesystem.
for name in ('initrd.img', 'initrd.img.old', 'vmlinuz', 'vmlinuz.old'):
    path=root/name
    if path.is_symlink():
        path.unlink()
PY
chroot "$root" apt-get clean
chroot "$root" systemctl enable systemd-networkd.service systemd-resolved.service armhpc-oect-boot-diagnostics.service
python3 - "$root" <<'PY'
from pathlib import Path
import sys
root=Path(sys.argv[1])
network=root/'etc/systemd/network/20-armhpc-ethernet.network'
network.parent.mkdir(parents=True,exist_ok=True)
network.write_text('[Match]\nName=en* eth*\n\n[Network]\nDHCP=yes\nLinkLocalAddressing=ipv6\n')
resolv=root/'etc/resolv.conf'
if resolv.exists() or resolv.is_symlink():
    resolv.unlink()
resolv.symlink_to('../run/systemd/resolve/stub-resolv.conf')
policy=root/'usr/sbin/policy-rc.d'
if policy.is_file() and policy.read_text().strip()=='#!/bin/sh\nexit 101':
    policy.unlink()
PY
chroot "$root" /usr/local/sbin/armhpc --help > "$output/armhpc-help.txt"
chroot "$root" /usr/local/sbin/armhpc-kube configuration --help > "$output/k3s-help.txt"
chroot "$root" dpkg-query -W > "$output/packages.tsv"
for name in node-setup k3s-setup k3s-preflight node-install-admin oect-setup oect-k3s-preflight; do
    test ! -e "$root/usr/local/sbin/$name"
done
# Remove only temporary files created by this build, before the final payload audit.
rm -f -- "$root/tmp/armhpc-customize-image.sh" "$root/.armhpc-customized"
find "$root/tmp/armhpc-kernel-packages" -maxdepth 1 -type f -name '*.deb' -delete
python3 - "$root/tmp/overlay" <<'PY'
from pathlib import Path
import shutil,sys
path=Path(sys.argv[1]).resolve()
assert path.name=='overlay' and path.parent.name=='tmp' and path != Path('/tmp/overlay')
shutil.rmtree(path)
PY
cleanup
trap - EXIT
python3 "$stage/rootfs-overlay/usr/lib/image-framework/image-audit.py" "$root" \
    --policy "$stage/rootfs-overlay/etc/image-framework/firmware-policy.json" > "$output/rootfs-audit.json"
test -f "$root/boot/dtb-$kernel_release/rockchip/rk3566-wxy-oec-turbo-4g.dtb"
cp "$debs"/*.deb "$output/kernel/"
tar --zstd --numeric-owner --acls --xattrs --xattrs-include='*' \
    -cpf "$output/boot-files.tar.zst" -C "$root/boot" .
tar --zstd --numeric-owner --acls --xattrs --xattrs-include='*' --one-file-system \
    --exclude='./boot/*' --exclude='./data/*' --exclude='./dev/*' --exclude='./proc/*' \
    --exclude='./sys/*' --exclude='./run/*' --exclude='./tmp/*' \
    -cpf "$output/factory-rootfs.tar.zst" -C "$root" .
python3 - "$output" "$kernel_release" <<'PY'
import hashlib,json
from pathlib import Path
import sys
output=Path(sys.argv[1])
files=[]
for path in sorted(output.rglob('*')):
    if path.is_file():
        digest=hashlib.file_digest(path.open('rb'),'sha256').hexdigest()
        files.append({'path':path.relative_to(output).as_posix(),'size_bytes':path.stat().st_size,'sha256':digest})
receipt={'schema_version':1,'target':'wxy-oect-k3s','architecture':'arm64',
    'kernel_release':sys.argv[2],'factory_rootfs_built':True,'emmc_img_built':False,
    'hardware_verified':False,'contains_upstream_k3s':False,'contains_temporary_miniloader':False,
    'files':files}
(output/'artifact-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
(output/'SHA256SUMS').write_text(''.join(f"{item['sha256']}  {item['path']}\n" for item in files))
print(json.dumps(receipt,ensure_ascii=False,indent=2))
PY
