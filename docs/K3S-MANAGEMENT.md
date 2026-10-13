# K3s management

Run these commands as root in the normal system.

| Command | Action |
|---|---|
| `armhpc-kube uninstall` | Stop workloads, archive node-local K3s data, uninstall, and clear wizard state. |
| `armhpc-kube reinstall` | Perform the same reset and open the interactive installation wizard. |
| `armhpc-kube clear` | Reset an unfinished installation; refuse an installed or running node. |

Use `--preview --json` to review paths. Use `--yes --json` for an explicitly approved batch uninstall or clear. Reinstall requires an interactive SSH terminal. `--k3s-version v1.37.1+k3s1` sets the next wizard default.

Archives are private directories under `/data/backups/k3s-reset-*`. They contain cluster credentials and local volume data, and must remain on the device or in private storage. An interrupted operation continues its original archive when the same command is run again.

For a complete cluster rebuild, reset workers and additional servers first, then the first server. Create the first server with option 2, wait until it is Ready, and join additional servers with option 4 and workers with option 3. Use the same complete K3s version.
