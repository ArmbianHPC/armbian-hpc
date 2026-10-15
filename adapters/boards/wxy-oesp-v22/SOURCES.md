<!-- SPDX-License-Identifier: GPL-2.0-only -->
# OES1PLUS v2.2 sources

| Input | Pinned source | License / distribution |
| --- | --- | --- |
| Linux 6.12.111 | kernel.org archive SHA256 `9e59dc67624188fa12a6601f9598499cd6662a9066be572b59f935e3d7849810` | GPL-2.0-only; upstream notices retained |
| Board DTS | ophub/linux-6.12.y commit `a6ab4bc106b5f65d6a3ecf577bf08d96ab4dced1`, `meson-g12b-s922x-oes-plus.dts` | GPL-2.0-or-later OR MIT; original authors retained in adapted DTS |
| Meson Ethernet integration | Same commit, `drivers/net/ethernet/stmicro/stmmac/dwmac-meson8b.c` | GPL-2.0-only; upstream notices retained |
| ARM64 offset / PCIe changes | First two changes from unifreq/linux-6.12.y PR 16, complete received patch SHA256 `a4daaf4cffdb003a3fdb5d0683decd72ff75c8b30487e16c87143ac52aa23b48` | Linux GPL-2.0-only; patch author/commit headers retained |
| ampart prefix builder | 7Ji/ampart source archive SHA256 `2421cd79812e68d8623cf05aeb367f7c701c47eba76f6794fb164672233e9115` | GPL-3.0; external build tool, source remains external |
| USB transport | superna9999/pyamlboot commit `d7806acc4f0a9a9d89b4e32a5c9a0ae03f7d11bf` | Module-specific MIT alternatives selected; originals and complete notices included in local transport bundle |
| RTL8153B firmware | linux-firmware tag `20230210`, `rtl_nic/rtl8153b-2.fw`, SHA256 `8e944ce86e2be0c0ae670996f63302a9ae36394baf2d5b9d2fefb248650ba6e0`; same bytes as Debian `firmware-realtek` `20230210-5` | Redistributable binary under `LICENCE.rtlwifi_firmware.txt`; copied unchanged with the original notice |
| Vendor loader skeleton | retro98boy/onethingcloud-oes-linux release v2025.08.09, `onethingcloud-oes-plus-skeleton.tar.gz`, SHA256 `f2b769242e2e7af664cc84343b4caf9158949333edef08e7103be80fb9e6c317` | Private local input; public redistribution not approved |

The image prefix includes the vendor loader and unchanged vendor DTB. Neither appears in source control. This source provenance does not certify the vendor signature chain or device boot. No new secure-boot keys, eFuse or RPMB provisioning are performed. The local input report and output hashes identify the exact bytes used.

Original ARMHPC integrations follow the repository license policy. Time, command bridging and direct OTA-library users retain the shared GPL-2.0-only OR GPL-3.0-only boundary.
