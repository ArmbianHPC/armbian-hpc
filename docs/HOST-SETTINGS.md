# Hostname and network configuration

Run configuration commands as root, or prefix them with `sudo`.

These commands run in the normal system. Rescue keeps its generated hostname and
its own network configuration. Use `--help` for arguments and `--json` for
protocol 1 responses.

## Hostname

```sh
armhpc hostname show
armhpc hostname set compute-07 --yes
armhpc hostname configure
```

`set` updates `/etc/hostname`, the primary `127.0.1.1` entry in `/etc/hosts` and
the running hostname. Existing hosts entries and aliases are retained. `configure`
opens an interactive wizard; it requires an SSH or serial terminal. No reboot is
needed. A prompt in an already open shell may retain the previous name until a
new shell is opened.

Hostnames must be DNS names of at most 64 characters, with labels of at most 63
characters. The default placeholder names `armbian`, `armhpc`, `localhost` and
`localhost.localdomain` are reserved. Boot initialization respects valid existing
hostnames, including ones configured with `hostnamectl`. An unset, invalid or
placeholder hostname receives the generated `armhpc-<MAC suffix>` default.
Rescue continues to generate `armhpc-rescue-<MAC suffix>`.

Hostname changes do not rename an already registered K3s node or edit its
`node-name` setting. Manage cluster node changes separately.

## Network

The built-in writer manages physical interfaces owned by **systemd-networkd**.
It does not take over NetworkManager, ifupdown, bridges, bonds, VLANs or tunnels.
An explicit interface is required when more than one physical interface exists.
Custom DNS requires systemd-resolved and its connected `/etc/resolv.conf`.

Inspect current addresses, routes, interface state and the latest transaction:

```sh
armhpc network show --interface eth0 --json
```

Preview DHCP or static IPv4 without changing files:

```sh
armhpc network set --interface eth0 --dhcp --plan --json
armhpc network set --interface eth0 --address 192.168.100.20/24 --gateway 192.168.100.1 --dns 192.168.100.1 --dns 1.1.1.1 --plan --json
```

Apply either configuration:

```sh
armhpc network set --interface eth0 --dhcp --yes --json
armhpc network set --interface eth0 --address 192.168.100.20/24 --gateway 192.168.100.1 --dns 192.168.100.1 --dns 1.1.1.1 --yes --json
```

The command returns a transaction ID and `confirm_command`. An independent
systemd service applies the configuration after two seconds. SSH may disconnect;
reconnect using the new address, or use the USB console, and execute the returned
command before the default **90-second** confirmation deadline:

```sh
armhpc network confirm --transaction <returned-transaction-id> --yes --json
```

An interactive SSH login automatically opens a confirmation prompt before the
application configuration menu when a transaction is pending. It displays the
requested settings and remaining time: choose `y` to save, `r` to roll back, or
Enter to continue to the shell without confirming. Invalid input does not restart
the countdown; no input times out at the original deadline. Login itself never
confirms a transaction. Normal logins without a pending change, noninteractive
SSH commands, SFTP and Rescue logins do not open this prompt. The existing
`ARMHPC_CONFIGURATION_DISABLE=1` login setting disables automatic prompts; manual
confirmation remains available. Administrators use their existing sudo access.

`confirm` requires physical link and an active IPv4 address; a static address must
match the requested address and prefix. It does not declare internet access or
DNS reachability verified. The deadline uses boot-relative monotonic time, so UTC
clock corrections do not extend it. `--rollback-after 180` selects another window
between 30 and 600 seconds.

Without confirmation the service restores the previous managed file, or removes
the new override to return to the previous profile. Closing SSH, the GUI or a
terminal does not cancel the service. A reboot while unconfirmed restores the
previous configuration before network startup. Confirmed changes survive reboot.

Manual rollback remains available for the latest confirmed or pending change:

```sh
armhpc network rollback --transaction <returned-transaction-id> --yes --json
```

Run `armhpc network configure` for the guided interface, DHCP/static IPv4,
gateway and DNS setup. A gateway is optional and must be a usable address on
the selected subnet. Repeat `--dns` or provide a comma-separated list. IPv6
continues with automatic configuration in the managed profile. Advanced routing
or static IPv6 configuration should use the network manager directly.

The writer creates `00-armhpc-<interface>.network` under `/etc/systemd/network`
and overrides the existing simple profile for that interface. Other configuration
files are left intact. An earlier matching profile or an unrelated file occupying
the managed filename is refused. Transaction backups are root-readable and are
not shown in status responses. A file changed outside the transaction produces a
rollback error instead of silently overwriting that change.

Configuration changes share the OTA writer lock and are refused during active
OTA, tool maintenance or reset diagnostics. Confirm or roll back network changes
before starting OTA or tool maintenance.

## Preservation boundaries

| Operation | System hostname and hosts entries | System network configuration |
| --- | --- | --- |
| Reboot normal system | Preserved | Confirmed configuration preserved |
| OTA `apply` of system/all | Preserved from the old system | Preserved by the explicit network-file policy |
| OTA `recover` of the system | Preserved; interrupted deployment reuses the verified pre-write snapshot | Preserved from that snapshot |
| Rescue menu: restore existing SATA system partition | Preserved with selected machine identity | Reset to the factory configuration |
| Rescue menu: install/repartition the whole SATA disk | Fresh default hostname | Fresh factory configuration |
| Flash a new complete image | Depends on which disks are rewritten; no preservation guarantee for overwritten filesystems | Same boundary |

Rescue hostnames are excluded from the hostname preservation policy. Neither
OTA operation restores an entire old `/etc`. Installing or recovering an older
baseline retains a newer authenticated maintenance runtime, so its hostname
startup fix remains available.

## Controller and completion

The controller command panel accepts these noninteractive commands and provides
hostname/network state presets. Settings remain normal-system-only; the
controller does not translate them into Rescue commands. Interactive wizards
require an SSH or serial terminal.

Bash Tab completion covers both commands, their public actions and arguments.
For an existing terminal load it with:

```sh
source /usr/lib/armhpc/completion.bash
```

The installed device runtime and controller must include these commands. Source
changes alone do not add commands to previously installed firmware.
