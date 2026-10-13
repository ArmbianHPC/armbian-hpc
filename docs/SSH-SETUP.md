# K3s configuration

Run `armhpc-kube configuration` in the normal system to create a SQLite server, initialise an embedded-etcd cluster, or join an existing cluster as an agent or server. `armhpc configuration` opens the same workload wizard. Node names, versions, cluster settings and credentials are stored locally.

The download menu offers upstream releases, a custom HTTPS mirror, and the Rancher Chinese community mirror. The China option selects the matching installer and binary origin, verifies the installer against a separate pinned SHA256, and enables its native download path rules. Keep the K3s version in its original form, such as `v1.36.5+k3s1`; the mirror's download directory uses a hyphen internally.

Installer network failures are retried at most three times. Certificate errors, invalid checksums, oversized scripts and permanent HTTP errors are rejected. Only a verified installer is committed and executed. If the mirror changes its installer, update the reviewed pin through a new software revision.

Container registry mirrors are a separate setting. Configure them on each node that needs them. The wizard accepts HTTPS registry endpoints and keeps existing node and cluster settings when a saved plan is retried.

Run `armhpc-kube check` to check DATA mounts, node identity, installation state and persistent directories. An interrupted download can be retried using the saved plan. To change its source, choose **2: correct settings or download source**, then **3: Rancher Chinese community mirror**. Each subsequent server must use the same critical cluster settings as the first server, including Secret encryption.

The installer and software downloads use HTTPS and SHA256 verification. Cluster tokens, credentials and upstream K3s binaries are excluded from this repository and OTA system packages. OTA restores an installed K3s binary at the recorded version. Restoring the normal system from Rescue preserves DATA; cluster maintenance is managed by the administrator.
