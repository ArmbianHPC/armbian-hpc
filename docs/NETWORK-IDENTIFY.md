# Network port identification

[简体中文](NETWORK-IDENTIFY.zh-CN.md)

Run as root to flash the port's green PHY LED at a regular cadence. The command starts a bounded background service and returns once flashing begins. It restores the saved hardware link/activity rules when the duration expires or the operation is stopped.

## Commands

In the normal system:

```sh
armhpc identify --seconds 30 --json
armhpc identify --status --json
armhpc identify --stop --json
```

In Rescue, use `armhpc-rescue identify` with the same options. The controller command panel includes start, stop, and status presets and routes `armhpc identify` to the device's current environment. Selected devices can use the usual 1–8 concurrency setting.

| Option | Meaning |
| --- | --- |
| `--interface eth0` | Select a physical port. Default: the board profile's interface or the only physical interface. |
| `--seconds 30` | Duration from 1 to 600 seconds; default 30. |
| `--period-ms 1000` | Complete on/off cycle from 200 to 5000 milliseconds; default 1000, with equal on/off halves. |
| `--status` | Query the latest operation. |
| `--stop` | Stop and restore the port's saved LED rules. |
| `--json` | Return a protocol 1 result for controller tools and scripts. |

The port must have an active link and a unique green LED associated with its PHY or network device. The LED must already use the hardware-offloaded `netdev` trigger. Identification changes only that LED's link rules and ends with a failure report if the link disappears or the PHY rejects the requested rules.

## Results and recovery

`running` means flashing has started. `completed` or `stopped` with `restored: true` means the original rules were restored. `failed` includes an error and the restoration result. An interrupted worker can be repaired with `--stop`; a subsequent start also restores the saved rules before taking a new snapshot. `restore-failed` requires inspecting the error and retrying `--stop`.

The background service continues when SSH or the serial connection closes. A second start on the same port is rejected while flashing is active. Rebooting ends the operation and the normal PHY initialisation sets the boot-time LED policy.

## Board configuration

Discovery checks the green LED's physical-device association, including both child `of_node` and parent `device/of_node` mappings. A status LED elsewhere on the board is excluded. `identify_index` in `/etc/armhpc/phy-led.json` optionally selects one output of the configured interface; OECT uses index 1 for its green link LED.
