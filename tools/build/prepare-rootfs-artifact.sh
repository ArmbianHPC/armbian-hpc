#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Expand a locally built Armbian cache and install the target's runtime overlay.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C.UTF-8
cache=$(realpath -e -- "${1:?Supply the built rootfs cache}")
cache_sha256="${2:?Supply its expected SHA256}"
stage=$(realpath -e -- "${3:?Supply the validated framework stage}")
work=$(realpath -e -- "${4:?Supply an existing Linux work directory}")
test "$EUID" -eq 0
[[ "$cache_sha256" =~ ^[0-9a-f]{64}$ ]]
test "$(sha256sum "$cache" | cut -d' ' -f1)" = "$cache_sha256"
test ! -e "$stage/.incomplete"
test "$work" != /
root="$work/rootfs"
test ! -e "$root"
mkdir "$root"
tar --zstd --numeric-owner --acls --xattrs --xattrs-include='*' -xpf "$cache" -C "$root"
architecture=$(python3 - "$stage/resolved.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))['architecture'])
PY
)
test "$(chroot "$root" dpkg --print-architecture)" = "$architecture"
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
if [[ ! -e "$root/usr/sbin/policy-rc.d" ]]; then
    printf '#!/bin/sh\nexit 101\n' > "$root/usr/sbin/policy-rc.d"
    chmod 0755 "$root/usr/sbin/policy-rc.d"
fi
cp --dereference /etc/resolv.conf "$root/etc/resolv.conf"
# Inherit the dedicated build host's optional proxy without saving it in rootfs.
chroot "$root" /usr/bin/env DEBIAN_FRONTEND=noninteractive \
    http_proxy="${http_proxy:-}" https_proxy="${https_proxy:-}" apt-get update
mapfile -t packages < "$stage/packages.txt"
for package in "${packages[@]}"; do [[ "$package" =~ ^[a-z0-9][a-z0-9+.-]*$ ]]; done
chroot "$root" /usr/bin/env DEBIAN_FRONTEND=noninteractive \
    http_proxy="${http_proxy:-}" https_proxy="${https_proxy:-}" \
    apt-get install --yes --no-install-recommends "${packages[@]}"
cp -a "$stage/rootfs-overlay/." "$root/"
chroot "$root" python3 - <<'PY'
import json
from pathlib import Path
for name,mode in json.loads(Path('/etc/image-framework/permissions.json').read_text()).items():
    Path(name).chmod(int(mode,8))
PY
printf '%s\n' "$cache_sha256" > "$root/.armhpc-customized"
chroot "$root" /usr/local/sbin/armhpc --help
printf 'Prepared rootfs: %s\n' "$root"
