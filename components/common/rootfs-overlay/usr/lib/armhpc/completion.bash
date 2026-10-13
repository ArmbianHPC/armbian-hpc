# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only

[ -n "${BASH_VERSION-}" ] || return 0

_armhpc_complete_words()
{
    local item
    for item in "$@"; do
        [[ $item == "$cur"* ]] && COMPREPLY+=("$prefix$item")
    done
    return 0
}

_armhpc_complete_path()
{
    local item action=${1:-f}
    compopt -o filenames 2>/dev/null || :
    while IFS= read -r item; do
        COMPREPLY+=("$prefix$item")
    done < <(compgen -"$action" -- "$cur")
}

_armhpc_completion_value_option()
{
    case $1 in
        --interface|--seconds|--period-ms|--heartbeat|--error|--rootfs|--username|--public-key-file|--policy|--disk|\
        --release|--manifest|--target|--anchor-manifest|--request-id|--job|--interval|--url|--tools-source|--ca|--origin|\
        --bundle|--phase|--exported-backup-sha256|--kind|--sata|--test-id|--address|--gateway|--dns|--rollback-after|--k3s-version|--transaction) return 0 ;;
        *) return 1 ;;
    esac
}

_armhpc_complete_value()
{
    local item name
    local state=${_ARMHPC_COMPLETION_STATE_ROOT:-/var/lib/armhpc/ota}
    case $1 in
        --target) _armhpc_complete_words all system rescue ;;
        --phase) _armhpc_complete_words prepare commit ;;
        --kind) _armhpc_complete_words expire direct-restart panic reboot ;;
        --sata) _armhpc_complete_words present absent ;;
        --heartbeat) _armhpc_complete_words on off ;;
        --seconds) _armhpc_complete_words 30 60 120 300 ;;
        --period-ms) _armhpc_complete_words 500 1000 2000 ;;
        --interval) _armhpc_complete_words 2 5 10 ;;
        --rollback-after) _armhpc_complete_words 90 120 300 ;;
        --k3s-version) _armhpc_complete_words v1.37.1+k3s1 ;;
        --rootfs) _armhpc_complete_path d ;;
        --manifest|--anchor-manifest|--bundle|--ca|--public-key-file|--policy|--disk) _armhpc_complete_path ;;
        --interface)
            for item in "${_ARMHPC_COMPLETION_NET_ROOT:-/sys/class/net}"/*; do
                [[ -d $item/device ]] || continue
                _armhpc_complete_words "${item##*/}"
            done
            ;;
        --release)
            _armhpc_complete_words initial current
            for item in "$state"/manifests/*.json; do
                [[ -f $item ]] || continue
                name=${item##*/}; name=${name%.json}
                [[ $name =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] && _armhpc_complete_words "$name"
            done
            ;;
        --job|--test-id)
            local folder=jobs
            [[ $1 == --test-id ]] && folder=watchdog
            for item in "$state/$folder"/*.json; do
                [[ -f $item ]] || continue
                name=${item##*/}; name=${name%.json}
                [[ $name =~ ^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$ ]] && _armhpc_complete_words "$name"
            done
            ;;
        *) : ;;
    esac
}

_armhpc_complete_options()
{
    local option word present
    for option in "$@"; do
        present=0
        for word in "${COMP_WORDS[@]:1:COMP_CWORD-1}"; do
            if [[ ${word%%=*} == "$option" && $option != --origin ]]; then
                present=1
                break
            fi
        done
        (( present )) || _armhpc_complete_words "$option"
    done
}

_armhpc_complete()
{
    local cur=${COMP_WORDS[COMP_CWORD]-} prev= prefix= token value_option= skip=0 i
    local program=${COMP_WORDS[0]##*/}
    local -a positional=() options=(-h --help)
    COMPREPLY=()
    (( COMP_CWORD > 0 )) || return 0
    compopt +o filenames +o nospace 2>/dev/null || :
    (( COMP_CWORD > 0 )) && prev=${COMP_WORDS[COMP_CWORD-1]}
    if [[ $cur == --*=* ]]; then
        value_option=${cur%%=*}; prefix=$value_option=; cur=${cur#*=}
    elif [[ $prev == = ]] && (( COMP_CWORD > 1 )); then
        value_option=${COMP_WORDS[COMP_CWORD-2]}
    elif _armhpc_completion_value_option "$prev"; then
        value_option=$prev
    fi
    if [[ -n $value_option ]]; then
        _armhpc_complete_value "$value_option"
        return 0
    fi
    for (( i=1; i<COMP_CWORD; i++ )); do
        token=${COMP_WORDS[i]}
        if (( skip )); then
            [[ $token == = ]] || skip=0
            continue
        fi
        if [[ $token == --*=* ]]; then continue; fi
        if _armhpc_completion_value_option "$token"; then skip=1; continue; fi
        [[ $token == -* ]] || positional+=("$token")
    done
    local command=${positional[0]-} operation=${positional[1]-} action=${positional[2]-}
    if [[ -z $command ]]; then
        if [[ $cur != -* ]]; then
            case $program in
                armhpc) _armhpc_complete_words hostname network identify watchdog usb-console ota configuration firstrun led rescue ;;
                armhpc-rescue) _armhpc_complete_words identify watchdog usb-console ota configuration status install restore provision firstrun led boot reboot console ;;
                armhpc-kube) _armhpc_complete_words configuration check uninstall reinstall clear ;;
            esac
        fi
    else
        case $command in
            hostname)
                [[ -n $operation || $cur == -* ]] || _armhpc_complete_words show set configure
                options+=(--json --yes)
                ;;
            network)
                [[ -n $operation || $cur == -* ]] || _armhpc_complete_words show set configure confirm rollback
                options+=(--json)
                case $operation in
                    show) options+=(--interface) ;;
                    set) options+=(--interface --dhcp --address --gateway --dns --rollback-after --plan --yes) ;;
                    configure) options+=(--interface --rollback-after --yes) ;;
                    confirm|rollback) options+=(--transaction --yes) ;;
                esac
                ;;
            identify) options+=(--interface --seconds --period-ms --status --stop --json) ;;
            led) options+=(--status --heartbeat --error --clear-error) ;;
            rescue|boot) options+=(--cancel --reboot) ;;
            firstrun) options+=(--rootfs --username --public-key-file) ;;
            install|restore) options+=(--disk --username --public-key-file --execute) ;;
            provision) options+=(--username --public-key-file) ;;
            uninstall|reinstall|clear)
                [[ $program != armhpc-kube ]] || options+=(--preview --yes --json --k3s-version)
                ;;
            configuration)
                options+=(--auto)
                [[ $program != armhpc-rescue ]] && options+=(--preview --policy)
                ;;
            usb-console)
                [[ -n $operation || $cur == -* ]] || _armhpc_complete_words status start stop logs
                options+=(--json)
                [[ $operation == logs ]] && options+=(--previous)
                ;;
            watchdog)
                if [[ -z $operation && $cur != -* ]]; then
                    _armhpc_complete_words status report
                    [[ $program != armhpc-rescue ]] || _armhpc_complete_words prepare test
                fi
                options+=(--json)
                case $operation in
                    prepare) [[ $program != armhpc-rescue ]] || options+=(--kind --sata --test-id --yes) ;;
                    test) [[ $program != armhpc-rescue ]] || options+=(--test-id --checked-sata-power --yes) ;;
                    report)
                        options+=(--test-id)
                        [[ $program != armhpc-rescue ]] || options+=(--confirm-recovered --manual-recovery --cancel-prepared --yes)
                        ;;
                esac
                ;;
            ota)
                options+=(--json --yes)
                if [[ -z $operation ]]; then
                    [[ $cur == -* ]] || _armhpc_complete_words bootstrap check plan download apply status logs cancel recover source doctor retry tools receive boot
                    options+=(--version)
                else
                    case $operation in
                        bootstrap) options+=(--apply --manifest --anchor-manifest) ;;
                        plan|download|apply|recover)
                            options+=(--release --manifest --target)
                            [[ $operation != plan ]] || options+=(--recovery)
                            [[ $operation != apply && $operation != recover ]] || options+=(--request-id --local-only)
                            ;;
                        status|logs) options+=(--job --watch --interval) ;;
                        cancel|retry) options+=(--job) ;;
                        source)
                            [[ -n $action || $cur == -* ]] || _armhpc_complete_words show set test
                            [[ $action != set ]] || options+=(--url --tools-source --allow-http --ca --origin)
                            ;;
                        tools)
                            [[ -n $action || $cur == -* ]] || _armhpc_complete_words status update
                            [[ $action != update ]] || options+=(--bundle --rollback --request-id)
                            ;;
                        receive) options+=(--manifest --target --request-id --phase) ;;
                        boot)
                            [[ -n $action || $cur == -* ]] || _armhpc_complete_words plan apply status rollback
                            case $action in
                                plan) options+=(--bundle) ;;
                                apply) options+=(--bundle --request-id --exported-backup-sha256 --commit) ;;
                                rollback) options+=(--request-id) ;;
                            esac
                            ;;
                    esac
                fi
                ;;
        esac
    fi
    [[ $cur != -* && ${#COMPREPLY[@]} != 0 ]] || _armhpc_complete_options "${options[@]}"
    return 0
}

complete -F _armhpc_complete armhpc armhpc-rescue armhpc-kube
