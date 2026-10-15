# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
param(
    [ValidateSet('load-ram','attach-only','probe-only')][string]$Mode='load-ram',
    [switch]$ElevatedLaunch
)
$ErrorActionPreference='Stop'
$ArmHpcPrincipal=[Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
$ArmHpcElevated=$ArmHpcPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $ArmHpcElevated) {
    if ($ElevatedLaunch) {
        Write-Host 'Administrator access was not granted. Diagnostic did not start.'
        Read-Host 'Press Enter to close'
        exit 5
    }
    Write-Host 'WorldCup USB access requires administrator rights. Windows will request elevation.'
    try {
        $ArmHpcArguments=@('-NoProfile','-ExecutionPolicy','Bypass','-File',('"'+$PSCommandPath+'"'),'-Mode',$Mode,'-ElevatedLaunch')
        $ArmHpcProcess=Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $ArmHpcArguments -Verb RunAs -WorkingDirectory $PSScriptRoot -WindowStyle Normal -Wait -PassThru
        exit $ArmHpcProcess.ExitCode
    } catch {
        Write-Host ('Elevation was cancelled or failed. Diagnostic did not start: '+$_.Exception.Message)
        Read-Host 'Press Enter to close'
        exit 5
    }
}
$ArmHpcPython=Join-Path $PSScriptRoot 'python\python.exe'
$ArmHpcScript=Join-Path $PSScriptRoot 'oesp-usb-diagnose.py'
$ArmHpcOutput=Join-Path $PSScriptRoot 'private-reports'
if ($Mode -eq 'probe-only') {
    & $ArmHpcPython -B $ArmHpcScript --load-ram --probe-only --output $ArmHpcOutput
} else {
    & $ArmHpcPython -B $ArmHpcScript ('--'+$Mode) --output $ArmHpcOutput
}
$ArmHpcResult=$LASTEXITCODE
Write-Host ''
Write-Host 'Send only the report.json path printed above. Keep captured BIN files private.'
Read-Host 'Press Enter to close'
exit $ArmHpcResult
