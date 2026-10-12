# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only

if [ -n "${BASH_VERSION-}" ]; then
    case "$-" in
        *i*)
            if [ -r /usr/lib/armhpc/completion.bash ]; then
                . /usr/lib/armhpc/completion.bash
            fi
            ;;
    esac
fi
