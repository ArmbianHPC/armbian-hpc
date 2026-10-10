# SPDX-License-Identifier: GPL-2.0-only
# Configure only interactive SSH sessions and keep the shell after exit/failure.
case "$-" in *i*) ;; *) return 0 ;; esac
[ -n "${SSH_CONNECTION:-}" ] || return 0
[ -t 0 ] && [ -t 1 ] || return 0
[ "${ARMHPC_CONFIGURATION_DISABLE:-0}" != 1 ] || return 0
if [ -f /etc/armhpc/rescue-environment ]; then
    armhpc_entry=/usr/local/sbin/armhpc-rescue
else
    armhpc_entry=/usr/local/sbin/armhpc
fi
if [ -x "$armhpc_entry" ]; then
    if [ "$(id -u)" = 0 ]; then
        "$armhpc_entry" configuration --auto
    else
        case " $(id -nG) " in *" sudo "*) "$armhpc_entry" configuration --auto ;; esac
    fi
fi
unset armhpc_entry
:
