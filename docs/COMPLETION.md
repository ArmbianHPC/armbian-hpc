# Command completion

[简体中文](COMPLETION.zh-CN.md)

`armhpc`, `armhpc-rescue`, and `armhpc-kube` support Tab completion in Bash. It works in SSH and USB serial terminals running the device's Bash shell.

## Examples

`<Tab>` below means pressing the Tab key:

```text
armhpc id<Tab>                               → armhpc identify
armhpc identify --inter<Tab>                  → --interface
armhpc identify --interface e<Tab>            → matching physical interfaces
armhpc ota app<Tab>                           → armhpc ota apply
armhpc ota apply --target s<Tab>              → system
armhpc-rescue ota tools up<Tab>               → update
armhpc-rescue ota tools update --bundle <Tab> → local files and directories
armhpc led --heartbeat o<Tab><Tab>            → on / off
```

When several candidates match, press Tab twice to display them. Common options, target values, boot and tool maintenance actions, physical interface names, and file paths are completed. Local release, job, and watchdog-record IDs are suggested from the default shared-state directory when available. Command and value forms such as `--target=system` are supported. Internal service commands and worker options are excluded.

## Installation and activation

The firmware's common overlay installs the completion library, the Bash login hook, and autoload files for `bash-completion`. Signed tool maintenance installs and backs up the same five resources in the normal system and both Rescue writable layers. Rollback restores their previous contents, and restoration of an older system baseline retains them with the maintained runtime.

New Bash login sessions load completion automatically, including when the optional `bash-completion` package is absent. With `bash-completion` installed, interactive non-login shells can also load it by command name. To enable it in an existing shell after installation:

```sh
source /usr/lib/armhpc/completion.bash
```

Completion reads local filenames and interface names. Pressing Tab does not execute ARMHPC commands or contact update servers. These scripts use Bash's completion interface; other shells require their own integration.
