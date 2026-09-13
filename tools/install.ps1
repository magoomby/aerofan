<#
.SYNOPSIS
    Install aerofan as a Windows service, with a tray icon that starts at logon.

.DESCRIPTION
    Run once, from an elevated PowerShell, in the repo you cloned:

        .\tools\install.ps1

    It installs in place - the service runs the files where they are, so a
    `git pull` followed by `Restart-Service AeroFan` is a complete update.
    Nothing is copied into Program Files.

    What it does:

      1. checks Windows, Python and PawnIO, and stops if any of them is missing
      2. creates %ProgramData%\aerofan for the profile, the config and the log
      3. registers the AeroFan service to start at boot, as SYSTEM, depending
         on PawnIO, restarting itself if it ever crashes
      4. adds the tray icon to HKLM Run so it starts at every logon
      5. starts both

    Everything it creates, uninstall.ps1 removes.

.PARAMETER FanProfile
    Also accepted as -Profile. The profile to start on. Defaults to leaving whatever is already saved,
    or 'auto' on a first install. 'auto' means the EC's own firmware curve.

.PARAMETER NoTray
    Install the service but not the tray icon.

.EXAMPLE
    .\tools\install.ps1
    .\tools\install.ps1 -Profile aggressive
#>

[CmdletBinding()]
param(
    [ValidateSet('auto', 'quiet', 'balanced', 'aggressive', 'max')]
    [Alias('Profile')]
    [string]$FanProfile,
    [switch]$NoTray
)

$ErrorActionPreference = 'Stop'

$ServiceName = 'AeroFan'
$ServiceDisplay = 'AeroFan Fan Control'
$ServiceDescription = 'Drives the Gigabyte AERO 15 fans from a temperature ' +
    'curve, remembers the profile you chose, and hands the fans back to the ' +
    'embedded controller on any failure.'
$RunKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run'
$RunValue = 'AeroFanTray'

$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ServiceScript = Join-Path $Repo 'aerofan\winservice.py'
$TrayScript = Join-Path $Repo 'aerofan\tray.py'
$DataDir = Join-Path $env:ProgramData 'aerofan'

# Windows PowerShell's Set-Content -Encoding UTF8 writes a byte order mark,
# and a BOM in front of a JSON document is a parse error for most readers.
# aerofan tolerates one now, but writing a clean file is the honest fix.
function Write-Json($path, $object) {
    $text = ($object | ConvertTo-Json)
    [System.IO.File]::WriteAllText($path, $text,
        (New-Object System.Text.UTF8Encoding($false)))
}

function Write-Step($text) { Write-Host "`n  $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "    ok    $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "    warn  $text" -ForegroundColor Yellow }
function Fail($text) {
    Write-Host "`n    STOP  $text`n" -ForegroundColor Red
    exit 1
}

Write-Host "`n  aerofan installer" -ForegroundColor White
Write-Host "  $Repo"

# --- 1. prerequisites --------------------------------------------------------

Write-Step 'Checking prerequisites'

if ($env:PROCESSOR_ARCHITECTURE -notmatch '64') {
    Fail 'This needs 64-bit Windows.'
}

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail ("Run this from an elevated PowerShell - creating a service needs it.`n" +
          "        Right-click PowerShell -> Run as administrator, then:`n" +
          "        cd '$Repo'; .\tools\install.ps1")
}
Write-Ok 'running elevated'

if (-not (Test-Path $ServiceScript)) { Fail "Cannot find $ServiceScript" }
if (-not (Test-Path $TrayScript))    { Fail "Cannot find $TrayScript" }

# The interpreter has to be the real executable, not the WindowsApps alias:
# those are per-user reparse points and do not resolve for the SYSTEM account.
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $pythonCommand) { $pythonCommand = Get-Command py.exe -ErrorAction SilentlyContinue }
if (-not $pythonCommand) {
    Fail ("Python is not on PATH. Install Python 3.10 or newer from`n" +
          '        https://www.python.org/downloads/ and tick "Add python.exe to PATH".')
}

$realPython = (& $pythonCommand.Source -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
if (-not $realPython -or -not (Test-Path $realPython)) {
    Fail "Could not work out where Python actually lives (asked $($pythonCommand.Source))."
}
$pythonDir = Split-Path $realPython
$pythonw = Join-Path $pythonDir 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $realPython }

$version = (& $realPython -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
if ([version]$version -lt [version]'3.10') {
    Fail "Python $version is too old; aerofan needs 3.10 or newer."
}
Write-Ok "python $version at $realPython"

if ($realPython -like "$env:SystemDrive\Users\*") {
    Write-Warn ('this Python lives in a user profile. SYSTEM can still run it, but ' +
                'the service will break if that profile is removed.')
}

# The modules blob and PawnIOLib.dll. aerofan/pawnio.py looks in Program Files
# and in the repo's own modules folder, so either is fine.
$pawnIoService = Get-Service -Name 'PawnIO' -ErrorAction SilentlyContinue
$pawnIoLib = @(
    (Join-Path $env:ProgramFiles 'PawnIO\PawnIOLib.dll'),
    (Join-Path ${env:ProgramW6432} 'PawnIO\PawnIOLib.dll')
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if (-not $pawnIoLib) {
    Fail ("PawnIO is not installed. It is the signed kernel driver aerofan uses`n" +
          "        to reach the embedded controller. Get it from https://pawnio.eu/`n" +
          "        then run .\tools\install-pawnio.ps1 to fetch the LpcACPIEC module.")
}
Write-Ok "PawnIO at $pawnIoLib"
if ($pawnIoService) {
    Write-Ok "PawnIO service is $($pawnIoService.Status), start type $($pawnIoService.StartType)"
} else {
    Write-Warn 'no PawnIO service found by that name; skipping the service dependency.'
}

$blob = @(
    (Join-Path $Repo 'modules\LpcACPIEC.bin'),
    (Join-Path $env:ProgramFiles 'PawnIO\LpcACPIEC.bin'),
    (Join-Path $env:ProgramFiles 'PawnIO\Modules\LpcACPIEC.bin')
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $blob) {
    Fail ("LpcACPIEC.bin is missing. Run .\tools\install-pawnio.ps1 to fetch it,`n" +
          "        or download it from https://github.com/namazso/PawnIO.Modules/releases`n" +
          "        into $Repo\modules\.")
}
Write-Ok "EC module $blob"

# Import everything the service and tray need, so a syntax error or a missing
# module is a clear failure here rather than a service that will not start.
$smoke = & $realPython -c @"
import sys
sys.path.insert(0, r'$Repo')
import aerofan.winservice, aerofan.tray, aerofan.ipc, aerofan.supervisor
print('ok')
"@ 2>&1
if ($LASTEXITCODE -ne 0 -or (($smoke -join "`n") -notmatch 'ok')) {
    Fail "aerofan does not import cleanly:`n$smoke"
}
Write-Ok 'aerofan imports cleanly'

# A service whose files can be rewritten by a non-administrator is a way in.
# Warn rather than change anything - it is your repo.
try {
    $writable = (& icacls.exe $Repo) 2>$null |
        Select-String -Pattern '(Users|Everyone|Authenticated Users):.*\((F|M|W)\)'
    if ($writable) {
        Write-Warn ("$Repo appears to be writable by non-administrators. Anything " +
                    "that can rewrite these files runs as SYSTEM at next boot.")
    }
} catch { }

# --- 2. data directory -------------------------------------------------------

Write-Step 'Setting up %ProgramData%\aerofan'

New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
Write-Ok $DataDir

$statePath = Join-Path $DataDir 'state.json'
if ($FanProfile) {
    Write-Json $statePath @{ profile = $FanProfile; updated = (Get-Date -Format 's') }
    Write-Ok "profile set to '$FanProfile'"
} elseif (-not (Test-Path $statePath)) {
    Write-Json $statePath @{ profile = 'auto'; updated = (Get-Date -Format 's') }
    Write-Ok "profile set to 'auto' (the EC's own curve) - change it from the tray icon"
} else {
    $existing = (Get-Content $statePath -Raw | ConvertFrom-Json).profile
    Write-Ok "keeping the saved profile '$existing'"
}

# --- 3. the service ----------------------------------------------------------

Write-Step "Installing the $ServiceName service"

$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existingService) {
    Write-Ok 'already present - stopping and replacing it'
    if ($existingService.Status -ne 'Stopped') {
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        $existingService.WaitForStatus('Stopped', '00:00:30')
    }
    & sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 2
}

$binaryPath = '"{0}" "{1}"' -f $pythonw, $ServiceScript
$newServiceArgs = @{
    Name           = $ServiceName
    BinaryPathName = $binaryPath
    DisplayName    = $ServiceDisplay
    Description    = $ServiceDescription
    StartupType    = 'Automatic'
}
if ($pawnIoService) { $newServiceArgs['DependsOn'] = 'PawnIO' }
New-Service @newServiceArgs | Out-Null
Write-Ok "created: $binaryPath"

# Restart on failure rather than sitting dead with the fans unmanaged. The EC
# curve takes over the moment the process dies, so a restart loop is safe.
& sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/15000/restart/60000 | Out-Null
& sc.exe failureflag $ServiceName 1 | Out-Null
Write-Ok 'restarts itself on failure'

Start-Service -Name $ServiceName
$service = Get-Service -Name $ServiceName
$service.WaitForStatus('Running', '00:00:30')
Write-Ok "service is $($service.Status)"

# Give the supervisor a moment to open the driver and answer the pipe.
$ready = $false
foreach ($attempt in 1..15) {
    Start-Sleep -Milliseconds 800
    $probe = & $realPython -c @"
import sys
sys.path.insert(0, r'$Repo')
from aerofan import ipc
print('yes' if ipc.is_running(2000) else 'no')
"@ 2>&1
    if (($probe -join "`n") -match 'yes') { $ready = $true; break }
}
if ($ready) {
    Write-Ok 'the service is answering its pipe'
} else {
    Write-Warn ("the service started but is not answering yet. Check " +
                (Join-Path $DataDir 'aerofan.log'))
}

# --- 4. the tray icon --------------------------------------------------------

if ($NoTray) {
    Write-Step 'Skipping the tray icon (-NoTray)'
} else {
    Write-Step 'Installing the tray icon'
    $trayCommand = '"{0}" "{1}"' -f $pythonw, $TrayScript
    New-ItemProperty -Path $RunKey -Name $RunValue -Value $trayCommand `
        -PropertyType String -Force | Out-Null
    Write-Ok "starts at logon: $trayCommand"

    try {
        Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like '*tray.py*' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    } catch { }
    Start-Process -FilePath $pythonw -ArgumentList "`"$TrayScript`"" -WindowStyle Hidden
    Write-Ok 'started - look for the pinwheel near the clock'
}

# --- done --------------------------------------------------------------------

Write-Host "`n  Done.`n" -ForegroundColor White
Write-Host "    Switch profiles      the tray icon, or from any shell (no admin needed):"
Write-Host "                           python -m aerofan.cli profile aggressive"
Write-Host "                           python -m aerofan.cli auto"
Write-Host "                           python -m aerofan.cli status"
Write-Host "    Service              sc query $ServiceName   /   Restart-Service $ServiceName"
Write-Host "    Log                  $(Join-Path $DataDir 'aerofan.log')"
Write-Host "    Remove it all        .\tools\uninstall.ps1"
Write-Host ""
