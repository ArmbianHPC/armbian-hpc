# v1.2.1-r1

- Integrate managed K3s probes, paired BOOT recovery and shared ext4 startup fixes.
- Record revision-aware OECT and OESP build provenance and attachment names.
- Allow independent framework and OTA versions with explicit compatibility requirements and separate source provenance.

# v1.2.0


- Add OES1PLUS v2.2 integration with dual Ethernet, SATA, status lights, port identification and fan control.
- Update OECT and OESP system components, Rescue A/B, configuration retention and recovery handling.
- Add explicit fixed-BOOT software refresh and complete multi-board release packaging.

# v1.1.4

Package the OESP storage binding correction with the existing fixed BOOT pair and native cable-flash workflow.

# v1.1.3

Package the OESP recovery revision with the existing fixed BOOT pair and updated runtime.

# v1.1.2

Allow a signed retained Rescue to start from RAM when BOOT cannot mount. Package the pinned USB loader and bounded recovery transport.

# v1.1.1

- Add OESP performance policy retry and reviewed RTL8153B firmware assembly.
- Integrate optional USB console support and shared command/runtime resources.
- Extend paired BOOT, DTB and normal-system maintenance with rollback protection.

# v1.1.0

- Add persistent normal-system hostnames and guided network configuration with confirmation and rollback.
- Add Ethernet-port identification and Bash completion for ARMHPC commands.
- Extend K3s setup with a pinned China download source, retries and node-local reset/reinstall commands.
- Update OECT firmware and signed OTA packaging with the shared 1.1.0 runtime.


# 1.0.0

首次正式发布：配置框架、OECT 适配、SquashFS Rescue A/B、SATA 安装恢复、网络身份、USB 串口、时间程序和独立 OTA 接入。
