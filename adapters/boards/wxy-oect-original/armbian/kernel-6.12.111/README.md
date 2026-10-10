# Linux 6.12.111 compatibility fixes

Base: Armbian v25.08, commit 147b9a23f09fae4d5ef8e73cf07ee69652ff4976.
Kernel: Linux 6.12.111, commit e2acc2211022246c77740d5df08265cc27eedcc5.
Original patch authors and licenses remain applicable.

- Helios64: remove obsolete max-link-speed context while preserving the GPIO change.
- Pinebook Pro: retain upstream plug cleanup and SVDM flow; omit two logging hunks.
- V4L2: remove context for an absent RK3588 clock constant; preserve the original changes.
- BCM43752: retain the upstream chip ID; add the missing PCI ID and driver support.
- I2S: omit the original patch; Linux already contains DEFAULT_MCLK_FS and its fallback.

The board hook copies the pinned upstream set to a private user patch directory.
It then applies these changes. Upstream source checkouts remain unchanged.

The final driver API patch adapts monitor-channel callbacks and reset controls
to the backported APIs in Linux 6.12.111.
