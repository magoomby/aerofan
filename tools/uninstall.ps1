<#
.SYNOPSIS
    Remove the aerofan service and tray icon.

.DESCRIPTION
    Run from an elevated PowerShell:

        .\tools\uninstall.ps1

    In order, because the order matters:

      1. close the tray icon
      2. stop the service - which, on its way out, hands the fans back to the
         embedded controller. That is the one step worth waiting for.
      3. delete the service and the logon entry
      4. confirm the EC has the fans

    The repo itself is left alone; so is PawnIO, which you installed separately
    and may well be using for something else. %ProgramData%\aerofan is kept
    unless you ask for it to go, because the log is usually the reason you are
    uninstalling.

.PARAMETER RemoveData
    Also delete %ProgramData%\aerofan - the saved profile, the config and the log.

.PARAMETER ResetCpuLimit
    Also clear any CPU maximum frequency cap. That cap lives in the Windows
    power scheme rather than in aerofan, so it survives an uninstall and would
    otherwise go on throttling the machine with nothing left to explain why.
    Without this switch the uninstaller reports the cap and leaves it alone.

.EXAMPLE
    .\tools\uninstall.ps1
    .\tools\uninstall.ps1 -RemoveData -ResetCpuLimit
#>

[CmdletBinding()]
param([switch]$RemoveData, [switch]$ResetCpuLimit)

$ErrorActionPreference = 'Continue'

$ServiceName = 'AeroFan'
$RunKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run'
$RunValue = 'AeroFanTray'
$DataDir = Join-Path $env:ProgramData 'aerofan'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Write-Step($text) { Write-Host "`n  $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "    ok    $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "    warn  $text" -ForegroundColor Yellow }

Write-Host "`n  aerofan uninstaller" -ForegroundColor White

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "`n    STOP  Run this from an elevated PowerShell.`n" -ForegroundColor Red
    exit 1
}

# --- 1. the tray icon --------------------------------------------------------

Write-Step 'Closing the tray icon'

$found = 0
try {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" `
        -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*tray.py*' } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $found++
        }
} catch { }
if ($found) { Write-Ok "closed $found tray process(es)" } else { Write-Ok 'none running' }

if (Get-ItemProperty -Path $RunKey -Name $RunValue -ErrorAction SilentlyContinue) {
    Remove-ItemProperty -Path $RunKey -Name $RunValue -Force
    Write-Ok 'removed from the logon Run key'
} else {
    Write-Ok 'no logon entry to remove'
}

# --- 2. the service ----------------------------------------------------------

Write-Step "Stopping the $ServiceName service"

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $service) {
    Write-Ok 'not installed'
} else {
    if ($service.Status -ne 'Stopped') {
        # The service releases the fans as it stops. Wait for it rather than
        # deleting out from under it - a killed service leaves custom mode on,
        # and the fans then sit at whatever duty was last written until the EC
        # is reset by a sleep or a reboot.
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        try {
            $service.WaitForStatus('Stopped', '00:00:45')
            Write-Ok 'stopped cleanly - the fans were handed back to the EC'
        } catch {
            Write-Warn 'it did not stop in 45s; deleting anyway'
        }
    } else {
        Write-Ok 'was already stopped'
    }

    & sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 1
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        Write-Warn ('Windows still lists the service. It will disappear once ' +
                    'everything holding a handle to it closes - usually the ' +
                    'Services window. A reboot always clears it.')
    } else {
        Write-Ok 'service deleted'
    }
}

# --- 3. make sure the EC really has the fans ---------------------------------

Write-Step 'Checking the fans are back on the EC curve'

$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if ($pythonCommand) {
    $output = & $pythonCommand.Source -c @"
import sys
sys.path.insert(0, r'$Repo')
try:
    from aerofan.control import Controller
    from aerofan.ec import EmbeddedController
    from aerofan.pawnio import PawnIO
    io = PawnIO().open()
    try:
        controller = Controller(EmbeddedController(io))
        if controller.holds_control():
            controller.release()
            print('released custom mode; the EC has the fans again')
        else:
            print('custom mode is already off; the EC has the fans')
    finally:
        io.close()
except Exception as exc:
    print('could not check: %s' % exc)
"@ 2>&1
    Write-Ok (($output | Select-Object -Last 1) -as [string])
} else {
    Write-Warn 'Python is not on PATH, so this could not be verified.'
}

# --- 4. the CPU cap, which outlives us -------------------------------------

Write-Step 'Checking the CPU frequency cap'

$q = powercfg /query SCHEME_CURRENT SUB_PROCESSOR PROCFREQMAX 2>$null
$ac = ($q | Select-String 'Current AC Power Setting Index:\s*0x([0-9a-f]+)').Matches.Groups[1].Value
$dc = ($q | Select-String 'Current DC Power Setting Index:\s*0x([0-9a-f]+)').Matches.Groups[1].Value
$acMhz = if ($ac) { [Convert]::ToInt32($ac, 16) } else { 0 }
$dcMhz = if ($dc) { [Convert]::ToInt32($dc, 16) } else { 0 }

if ($acMhz -eq 0 -and $dcMhz -eq 0) {
    Write-Ok 'no cap set - the CPU is unrestricted'
} elseif ($ResetCpuLimit) {
    powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCFREQMAX 0 | Out-Null
    powercfg /setdcvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCFREQMAX 0 | Out-Null
    powercfg /setactive SCHEME_CURRENT | Out-Null
    Write-Ok "cleared (was $acMhz MHz on AC, $dcMhz MHz on battery)"
} else {
    # Worth being loud about. A capped CPU with no aerofan left on the machine
    # is a laptop that is mysteriously slow and nothing to point at.
    Write-Warn ("the CPU is still capped at $acMhz MHz on AC / $dcMhz MHz on " +
                "battery. That is a Windows power-scheme setting, not an " +
                "aerofan one, so removing aerofan does not remove it.")
    Write-Host "          Clear it with:  .\tools\uninstall.ps1 -ResetCpuLimit" -ForegroundColor Yellow
    Write-Host "          or by hand:     powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCFREQMAX 0" -ForegroundColor Yellow
    Write-Host "                          powercfg /setdcvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCFREQMAX 0" -ForegroundColor Yellow
    Write-Host "                          powercfg /setactive SCHEME_CURRENT" -ForegroundColor Yellow
}

# --- 5. data -----------------------------------------------------------------

if ($RemoveData) {
    Write-Step 'Removing %ProgramData%\aerofan'
    if (Test-Path $DataDir) {
        Remove-Item -Path $DataDir -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path $DataDir) { Write-Warn "could not fully remove $DataDir" }
        else { Write-Ok 'removed' }
    } else {
        Write-Ok 'nothing there'
    }
} else {
    Write-Step 'Keeping your settings'
    Write-Ok "$DataDir left in place (add -RemoveData to delete it)"
}

Write-Host "`n  Done. The repo and PawnIO are untouched." -ForegroundColor White
Write-Host "  Reinstall any time with  .\tools\install.ps1`n"
