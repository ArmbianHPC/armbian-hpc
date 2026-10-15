<!-- SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only -->
# Base image and firmware updates in 1.3.0

[简体中文](BASE-IMAGE-1.3.0.zh-CN.md)

This version rebuilds the OECT and OESP Linux 6.12.111 kernels, restores explicitly registered node resources from DATA, and adds management network, authentication, firewall, resource and encrypted swap controls. Software checks and physical acceptance have separate results. Version 1.3.0 is delivered locally for operator acceptance.

## Kernel and startup

Both board builds merge `profiles/kernel/kubernetes.json` on every invocation, including cached builds. Packaging requires a valid final configuration, module dependency index and matching kernel/DTB/initramfs. `KERNEL-RESULT.json`, `kernel.config`, `MODULES.json` and source archives identify the actual inputs. Board drivers and memory definitions are retained; usable RAM is measured from `MemTotal`.

Normal system startup follows:

1. Verify writable ext4 DATA, UUID and SATA serial/WWN.
2. Restore only registered missing programs, units, configuration and enablement from checksum-verified DATA resources.
3. Prepare declared tmpfiles, including `/run/xtables.lock`, before dependent service sandboxes.
4. Start the explicitly configured swap profile.
5. Start K3s and registered services through their dependencies.

Missing, replaced or read-only DATA blocks dependent services and leaves SSH available. Normal boot does not partition disks or format DATA. Rescue can start without SATA. Global `swap.target` is available; the default ARMHPC swap profile is disabled.

## Management network

```sh
armhpc network configure
armhpc network set --interface eth0 --dhcp --management --family ipv4 --metric 100 --plan --json
armhpc network set --interface eth0 --address 192.168.100.20/24 --gateway 192.168.100.1 --management --management-target 192.168.100.10 --metric 100 --route-table 200 --source 192.168.100.20/32 --plan --json
```

Review the plan, remove `--plan` and add `--yes` to apply. Reconnect over SSH and confirm the displayed transaction. The default confirmation window is 90 seconds. Failure to confirm restores the previous files and management policy.

New devices use eth0/IPv4 as their management path. OESP eth1 has a higher route metric and is not required for online readiness. Readiness checks carrier, a usable required-family address, and the selected route/source to an explicit management target or saved K3s API address; otherwise it checks the family default route. Each wait is bounded at 45 seconds. No fixed public probe address is used.

```sh
armhpc firewall set --role worker --management-cidr 192.168.100.0/24 --cluster-cidr 192.168.100.0/24 --json
```

The firewall command previews its rules. Add `--apply --yes` to apply and confirm from a separate SSH connection within 90 seconds. Use `server` or `server-etcd` where appropriate. Only `table inet armhpc` is managed; external firewall policy is preserved and may still deny traffic. Review CIDRs, SSH port, K3s role and any optional NodePort exposure first.

## Persistent programs and K3s

```sh
armhpc state status --json
armhpc state reconcile --json
armhpc state reconcile --apply --yes --json
armhpc-kube check --json
```

The service catalog lives in `/data/node-config/armhpc/state.json`. Resources are retained by SHA256 on the same verified DATA mount. Reconciliation restores missing files, rejects altered files and corrupted caches, and retains enablement. It does not replace an entire old `/etc`, reset a node, clear K3s/etcd data, select a new version, or join another cluster.

To import another service, provide an explicit root-relative manifest:

```json
{"unit":"example.service","enabled":true,"version":"1.0","source":"administrator-managed","files":["usr/local/bin/example","etc/systemd/system/example.service","etc/tmpfiles.d/example.conf"]}
```

```sh
armhpc state register --manifest /root/example-service.json --json
armhpc state register --manifest /root/example-service.json --apply --yes --json
```

Preview validates every file before changing state. Imports require root-owned resources that are not writable by other users. Arbitrary filesystem paths, absolute symlink targets and undeclared symlink targets are rejected. Files with capabilities or extended attributes need an explicit package adapter. Keep the resource manifest and verify service behavior after import.

The K3s wizard preserves an existing selected version. Joining a cluster requires an explicit complete version when no trusted version is available. Installer bytes, the exact-architecture program, release SHA256 list and same-version airgap archive are verified and retained on DATA; installation uses that local program. A missing binary or unit is not treated as installed merely because a marker exists. A consistent legacy installation can be imported once; missing or conflicting resources produce a repair state.

Use the wizard for official/custom/Rancher China artifact sources or an explicit offline release directory. An offline directory contains the selected architecture's checksum list, binary, airgap archive and pinned `install.sh`. TLS verification stays enabled. Container registry configuration has a separate import path:

```sh
armhpc-kube registry import --file /root/registries.yaml --default-fallback deny --json
armhpc-kube registry import --file /root/registries.yaml --default-fallback deny --apply --yes --json
```

Stop the drained K3s service before applying. CA and client certificate/key files are copied to private DATA paths. Status omits credentials. Default endpoint fallback applies to registries declared in the mirror configuration, according to K3s behavior; it is not an outbound firewall. Persistent kubelet configuration directories require K3s v1.32 or newer. Existing older versions are retained and need a reviewed adapter before enabling these profiles.

## Authentication

Initial image login is `root` / `1`. Rescue has independent authentication. After the normal system actually creates or joins K3s and its matching node is Ready, the next interactive SSH login offers password change, key-only SSH guidance, or deferral. Automated SSH commands and SFTP do not enter that prompt.

```sh
armhpc auth status --json
armhpc auth password
armhpc auth keys --json
armhpc auth key-add --public-key-file /root/administrator.pub --yes --json
armhpc auth key-only --public-key-file /root/administrator.pub --yes --json
```

Keep the original session open. Key-only setup requires a new SSH connection using the selected public key, then a second new key-authenticated connection to confirm the changed policy. Run the displayed `armhpc auth confirm --transaction … --yes --json` in each new session. Password sessions and the original connection cannot confirm. The transaction rolls back after 90 seconds or reboot. Effective sshd policy and conflicts are checked before activation. Console PAM/password access remains available. Uninstalling K3s does not restore an initial password.

## Swap, resources and health

```sh
armhpc swap status --json
armhpc swap set --profile host-noswap --size-gib 16 --json
armhpc swap set --profile limited --size-gib 16 --json
armhpc resources set --role worker --memory-gib 4 --json
armhpc health status --json
```

These commands preview by default. Stop the drained K3s service before applying a profile with `--apply --yes`. Profiles are `disabled`, `host-noswap` (host swap, Pods NoSwap) and `limited` (Pods LimitedSwap). Swap uses a fully allocated, root-owned 0600 file on the verified DATA disk and a random-key dm-crypt mapping recreated each boot. No key is retained and hibernation is unsupported. Explicit `--import-existing` is required for an unregistered known file. A changed DATA identity, sparse file or changed inode is rejected.

An enabled profile fails closed for dependent services unless `--allow-degraded-noswap` was explicitly selected. `MemorySwapMax=0` applies to the K3s service itself; actual Pod cgroup ancestry and kubelet effective swap behavior still require the workload acceptance checks. Resource profiles use measured memory and retain complete eviction signals; absent an explicit profile, kubelet defaults remain in effect. Conflicting external kubelet settings are reported.

Health collection runs every 60 seconds with a 15-second service limit. JSON and metrics are written atomically under `/run/armhpc`; bounded boot samples and Linux journal excerpts are retained on DATA when available. Reports include age and collector result. Unconfirmed reset causes remain unknown. Early bootloader output and unvalidated ramoops memory regions are not synthesized.

## Firmware OTA and rollback

Use the matching 1.3.0 controller and maintenance package. Import the board's signed TAR.XZ and select **all**. The included `firmware/boot-update.tar.xz` uses the signed `firmware-update` purpose and requires agent 1.3.0. The controller:

1. Maintains tools in the normal system and Rescue A/B.
2. Verifies device, board, layout, BOOT ABI, signature, payload hashes and the supported old BOOT pairing.
3. Enters confirmed Rescue, prepares a private BOOT recovery backup and exports/verifies it on the computer.
4. Deploys the new versioned BOOT files and paired inactive Rescue.
5. Confirms the new Rescue boot, updates the paired SATA system and verifies state/services before committing.

Do not close or delete the private recovery backup. A lost connection or closed controller leaves the persistent task available for observation; it does not create a new job or imply success. Use the original task ID for status, retry or rollback. A BOOT pairing from an unsupported older version is rejected before switching; use a matching reviewed migration bundle or an image instead.

Normal firmware confirmation does not sign watchdog/kexec acceptance. If a kernel cannot execute the recovery path, exported BOOT recovery and local intervention may still be required. Rollback restores the related BOOT/resources and selection; it preserves identity, latest time records and DATA and never downgrades an etcd/K3s database automatically.

## Acceptance and requirement mapping

Copy `tools/verify-node-1.3.py` to the node. It stores reports only on a real writable OTA_SHARED mount:

```sh
python3 /root/verify-node-1.3.py
python3 /root/verify-node-1.3.py --baseline
# After the agreed upgrade/switch sequence, in the normal system:
python3 /root/verify-node-1.3.py --compare
```

Baseline mode explicitly writes a 1 MiB marker on verified DATA and retains a private immutable configuration/identity baseline. Compare reports pass/fail without printing credentials. Local checks do not sign physical acceptance.

| Original checklist | Implementation/evidence | Remaining operator acceptance |
|---|---|---|
| 3 Kernel | Shared final configuration gate, modules/config/kernel hashes | CPU 100m quota, seccomp and actual cluster runtime |
| 4 Network | Management transaction, family/route/source checks, owned firewall | Both ports, absent DHCPv6, gateway failure, cross-node DNS/Service/NAT and policy allow/deny |
| 5 DATA/runtime | UUID/SATA checks, restore barrier, tmpfiles and swap ordering | DATA delay/missing/read-only, no hidden-root writes or namespace failure |
| 6 State | Exact resource catalog, preview/import, cached repair | Interrupted upgrade and service recovery with identity/DATA comparison |
| 7 Authentication | Normal-system reminder and two key sessions | Reconnect confirmation and timed rollback; Rescue independence |
| 8 K3s | Exact version/arch resources, YAML checks, CRI/API consistency | Cold images, logs/exec, all actual cluster roles |
| 9 Swap/resources | Explicit profiles and measured budgets | Effective kubelet settings and Pod/host swap behavior |
| 10 Health | Bounded local JSON/metrics and persistent samples | Thermal load and previous-boot evidence |
| 12 Hardware | Per-board report template, separate acceptance status | 3 normal reboots, 3 cold starts, controlled power loss and 24-hour load per board |
| 13 Delivery | IMG.XZ, paired signed OTA, tools, source/hash/config records | Operator review and returned physical reports |

OECT watchdog natural-timeout recovery, node8's abnormal reset cause and node4's physical capacity remain evidence-dependent open items. No firmware image changes physical RAM or claims those checks have passed.
