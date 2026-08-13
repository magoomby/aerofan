<#
.SYNOPSIS
    Check for PawnIO, fetch the LpcACPIEC module, and verify the driver opens.

.DESCRIPTION
    Run this once, elevated. It does not write to the embedded controller.

    PawnIO itself is not installed by this script - it is a signed kernel driver
    and you should install it yourself, knowingly, from https://pawnio.eu/.
    This script tells you whether it is present and correctly wired up, and
    places the one module aerofan needs.

.NOTES
    Why PawnIO: this machine runs Memory Integrity (HVCI) with the vulnerable
    driver blocklist on, so WinRing0 - the usual choice - will not load.
#>

[CmdletBinding()]
param(
    [string] $ModuleUrl = 'https://github.com/namazso/PawnIO.Modules/releases/latest/download/LpcACPIEC.bin',
    [string] $RepoRoot  = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'

function Test-Elevated {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal] $identity
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Elevated)) {
    Write-Error 'Run this from an elevated PowerShell (Run as administrator).'
    return
}

Write-Host ''
Write-Host '== PawnIO ==' -ForegroundColor Cyan

$installDir = Join-Path $env:ProgramFiles 'PawnIO'
$libPath    = Join-Path $installDir 'PawnIOLib.dll'

if (-not (Test-Path $installDir)) {
    Write-Host '  Not installed.' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '  Install it from https://pawnio.eu/ and re-run this script.'
    Write-Host '  It is a signed driver that runs sandboxed bytecode modules;'
    Write-Host '  the module aerofan loads can only touch ports 0x62 and 0x66.'
    return
}

Write-Host "  Install dir : $installDir" -ForegroundColor Green
if (Test-Path $libPath) {
    $v = (Get-Item $libPath).VersionInfo.FileVersion
    Write-Host "  PawnIOLib   : present (file version $v)" -ForegroundColor Green
} else {
    Write-Warning "  PawnIOLib.dll not found under $installDir"
}

$svc = Get-Service -Name 'PawnIO' -ErrorAction SilentlyContinue
if ($svc) {
    Write-Host "  Driver svc  : $($svc.Status)" -ForegroundColor Green
} else {
    Write-Warning '  PawnIO service not registered.'
}

Write-Host ''
Write-Host '== LpcACPIEC module ==' -ForegroundColor Cyan

$modulesDir = Join-Path $RepoRoot 'modules'
$null = New-Item -ItemType Directory -Path $modulesDir -Force
$blob = Join-Path $modulesDir 'LpcACPIEC.bin'

if (Test-Path $blob) {
    Write-Host "  Already present: $blob" -ForegroundColor Green
} else {
    Write-Host "  Downloading from $ModuleUrl"
    try {
        Invoke-WebRequest -Uri $ModuleUrl -OutFile $blob -UseBasicParsing
        Write-Host "  Saved to $blob" -ForegroundColor Green
    } catch {
        Write-Warning "  Download failed: $($_.Exception.Message)"
        Write-Host '  Grab LpcACPIEC.bin manually from'
        Write-Host '  https://github.com/namazso/PawnIO.Modules/releases'
        Write-Host "  and place it at $blob"
        return
    }
}

Write-Host ''
Write-Host '== Access_EC mutex ==' -ForegroundColor Cyan
Write-Host '  Checked at runtime by aerofan; see the probe output.'

Write-Host ''
Write-Host '== Next ==' -ForegroundColor Cyan
Write-Host '  python -m aerofan.probe              # read-only baseline'
Write-Host '  python -m aerofan.probe --load 60    # read-only, with load'
Write-Host ''
Write-Host '  Neither writes to the EC.' -ForegroundColor Green
Write-Host ''
