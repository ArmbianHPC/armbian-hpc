#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Check the actual packaged OECT DTB and kernel; physical hardware is required next."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

KERNEL = '6.12.111-current-rockchip64'
BUILTINS = ('STMMAC_ETH', 'STMMAC_PLATFORM', 'DWMAC_ROCKCHIP', 'REALTEK_PHY',
            'GPIO_ROCKCHIP', 'ROCKCHIP_IODOMAIN', 'REGULATOR_FIXED_VOLTAGE',
            'REGULATOR_PWM', 'PWM_ROCKCHIP', 'MMC_SDHCI_OF_DWCMSHC',
            'LEDS_GPIO', 'LEDS_TRIGGERS', 'LEDS_TRIGGER_HEARTBEAT')


def require(value, message):
    if not value:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--boot', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--profile', choices=('diagnostic', 'stock'), default='diagnostic')
    args = parser.parse_args()
    boot = args.boot.resolve(strict=True)
    root = args.root.resolve(strict=True)
    dtb = boot / ('dtb-' + KERNEL) / 'rockchip/rk3566-wxy-oec-turbo-4g.dtb'
    config = (boot / ('config-' + KERNEL)).read_text().splitlines()
    for symbol in BUILTINS:
        require('CONFIG_' + symbol + '=y' in config, symbol + ' must be built in.')

    def property(node, name, numeric=False):
        result = subprocess.run(['fdtget', '-t', 'x' if numeric else 's', str(dtb), node, name],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return result.stdout.strip() if result.returncode == 0 else None

    gmac = property('/aliases', 'ethernet0')
    require(gmac == '/ethernet@fe010000', 'Ethernet alias must select GMAC1.')
    require(property(gmac, 'status') == 'okay', 'GMAC1 is disabled.')
    require(property(gmac, 'phy-mode') == 'rgmii', 'Original OECT uses MAC RGMII delays.')
    require(property(gmac, 'tx_delay', True) == '4f' and property(gmac, 'rx_delay', True) == '2d', 'MAC RGMII delays differ from working reference.')
    require(property(gmac, 'clock_in_out') == 'input', 'GMAC clock direction differs.')
    require(len((property(gmac, 'pinctrl-0', True) or '').split()) == 6, 'GMAC1 pinmux is incomplete.')
    for name in ('snps,reset-gpio', 'snps,reset-delays-us'):
        require(property(gmac, name, True) is None, 'Stale MAC reset/delay property: ' + name)
    mdio = gmac + '/mdio'
    phy = mdio + '/ethernet-phy@0'
    require(property(phy, 'reg', True) == '0', 'Original OECT reference requires MDIO PHY address 0.')
    require(property(gmac, 'phy-handle', True) == property(phy, 'phandle', True), 'GMAC points at a different PHY.')
    require(property(mdio, 'reset-delay-us', True) == format(20000, 'x'), 'PHY reset assert timing differs.')
    require(property(mdio, 'reset-post-delay-us', True) == format(100000, 'x'), 'PHY reset recovery timing differs.')
    require(property(phy, 'reset-gpios', True) is None, 'MDIO and PHY cannot both own the reset GPIO.')
    reset = (property(mdio, 'reset-gpios', True) or '').split()
    require(len(reset) == 3 and reset[0] == property('/pinctrl/gpio@fe770000', 'phandle', True)
            and reset[1:] == ['12', '1'], 'PHY reset must be GPIO4_C2, active low.')
    emmc = property('/aliases', 'mmc0')
    require(len((property(emmc, 'pinctrl-0', True) or '').split()) == 4, 'eMMC pinmux is incomplete.')
    if args.profile == 'stock':
        require(property(emmc, 'max-frequency', True) == format(200000000, 'x'), 'Stock HS200 clock must be 200 MHz.')
        require(property(emmc, 'mmc-hs200-1_8v', True) is not None, 'Stock HS200 capability is missing.')
        require(property('/leds/blue-led', 'linux,default-trigger') == 'heartbeat', 'Blue heartbeat channel is missing.')
        require(property('/leds/green-led', 'linux,default-trigger') is None, 'Green must remain the base mode channel.')
        for colour in ('red', 'green', 'blue'):
            require(property('/leds/'+colour+'-led', 'function') == 'status', 'RGB status class name is not explicit.')
        require('CONFIG_PHYLIB_LEDS=y' in config, 'PHYLIB LED hardware control must be built in.')
        require('CONFIG_LEDS_TRIGGER_NETDEV=y' in config or 'CONFIG_LEDS_TRIGGER_NETDEV=m' in config, 'PHY netdev trigger is absent.')
        if 'CONFIG_LEDS_TRIGGER_NETDEV=m' in config:
            require(any((root/'lib/modules'/KERNEL).rglob('ledtrig-netdev.ko*')), 'PHY LED trigger module is missing from rootfs.')
        for index in range(3):
            require(property(phy+'/leds/led@'+str(index), 'reg', True) == format(index, 'x'), 'PHY LED index is missing.')
        for unit in ('armhpc-led.service', 'armhpc-performance.service', 'armhpc-oect-phy-leds.service'):
            result = subprocess.run(['systemctl', '--root', str(root), 'is-enabled', unit], capture_output=True, text=True)
            require(result.returncode == 0 and result.stdout.strip() == 'enabled', 'Runtime policy is not enabled: '+unit)
    else:
        require(property(emmc, 'max-frequency', True) == format(52000000, 'x'), 'Diagnostic eMMC clock must be limited to 52 MHz.')
        require(property(emmc, 'mmc-hs200-1_8v', True) is None, 'HS200 must remain disabled for bring-up.')
        require(property('/leds/green-led', 'linux,default-trigger') == 'heartbeat', 'Kernel heartbeat LED is missing.')
    require((root / 'usr/lib/armhpc/oect-boot-diagnostics.py').is_file(), 'Boot diagnostics script is missing.')
    # systemctl --root resolves absolute unit symlinks inside the target filesystem.
    result = subprocess.run(['systemctl', '--root', str(root), 'is-enabled', 'armhpc-oect-boot-diagnostics.service'],
                            capture_output=True, text=True)
    require(result.returncode == 0 and result.stdout.strip() == 'enabled', 'Boot diagnostics is not enabled.')
    print(json.dumps({'schema_version': 1, 'passed': True, 'hardware_verified': False,
        'scope': 'packaged DTB, kernel configuration and enabled diagnostics; no physical GMAC/PHY boot',
        'kernel_release': KERNEL, 'dtb_sha256': hashlib.sha256(dtb.read_bytes()).hexdigest(),
        'built_in_drivers': list(BUILTINS), 'gmac': gmac, 'phy': phy,
        'phy_mdio_address': 0, 'phy_mode': property(gmac, 'phy-mode'), 'reset_gpio': 'GPIO4_C2 active low, MDIO bus before PHY-ID reads',
        'emmc_pinmux': 'bus8/clk/cmd/datastrobe', 'profile': args.profile,
        'emmc_max_hz': 200000000 if args.profile == 'stock' else 52000000,
        'rgb_heartbeat_channel': 'blue' if args.profile == 'stock' else 'green'}, indent=2))


if __name__ == '__main__':
    main()
