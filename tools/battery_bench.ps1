<#
.SYNOPSIS
    Measure what each CPU cap actually costs you in watts, on battery.

.DESCRIPTION
    Answers "which frequency should I use in a lecture" with your numbers
    instead of somebody's rule of thumb, because the honest answer depends on
    your machine, your panel brightness and what you happen to be running.

    UNPLUG FIRST. The only real power meter a laptop has is its own battery,
    and it reports nothing useful while it is charging.

    For each cap it sets the limit, waits for the machine to settle, then
    samples the battery's discharge rate in milliwatts and reports the median.
    Run it while doing roughly what you would be doing in a lecture - a
    document open, a browser, nothing heavy - so the numbers mean something.

    Takes about 90 seconds per setting. Restores what you started with.

    A caveat worth reading the results with: a frequency cap only bites when
    something asks for performance. Note-taking leaves the CPU idle most of
    the time, so the gap between caps is often smaller than people expect, and
    "race to idle" means a slower CPU sometimes uses MORE energy for a burst
    of work because it stays awake longer doing it. That is exactly why this
    measures rather than assumes.

.PARAMETER Caps
    Which limits to test, in MHz. 0 means unlimited.

.PARAMETER Seconds
    How long to sample each setting. Longer is steadier; 60 is a good minimum.

.EXAMPLE
    .\tools\battery_bench.ps1
    .\tools\battery_bench.ps1 -Caps 1000,1400,1800,2300,0 -Seconds 90
#>

[CmdletBinding()]
param(
    [int[]]$Caps = @(1400, 2300, 0),
    [int]$Seconds = 60
)

$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Get-Draw {
    $s = Get-CimInstance -Namespace root\wmi -ClassName BatteryStatus -ErrorAction SilentlyContinue |
         Select-Object -First 1
    if (-not $s) { return $null }
    if ($s.PowerOnline) { return 'AC' }
    return [int]$s.DischargeRate    # milliwatts
}

Write-Host "`n  aerofan battery bench" -ForegroundColor White

$probe = Get-Draw
if ($null -eq $probe) {
    Write-Host "`n    STOP  No battery telemetry on this machine.`n" -ForegroundColor Red
    exit 1
}
if ($probe -eq 'AC') {
    Write-Host "`n    STOP  Still plugged in. A charging battery reports no" -ForegroundColor Red
    Write-Host "          discharge rate, so there is nothing to measure." -ForegroundColor Red
    Write-Host "          Unplug and run this again.`n" -ForegroundColor Red
    exit 1
}

$startCap = (& python -c "import sys; sys.path.insert(0, r'$repo'); from aerofan import cpufreq; print(cpufreq.read_limit()['limit'])" 2>$null)
Write-Host "  starting cap: $startCap MHz (0 = unlimited) - will be restored"
Write-Host "  sampling $Seconds s per setting. Keep doing whatever you would be doing.`n"

$results = @()
foreach ($cap in $Caps) {
    & python -m aerofan.cli cpu $cap 2>&1 | Out-Null
    Write-Host ("  {0,-10} settling..." -f "$cap MHz") -NoNewline
    Start-Sleep -Seconds 20        # let turbo residency and temperatures settle

    $samples = @()
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $d = Get-Draw
        if ($d -eq 'AC') {
            Write-Host "`r    STOP  Plugged back in mid-run; results discarded.`n" -ForegroundColor Red
            & python -m aerofan.cli cpu $startCap 2>&1 | Out-Null
            exit 1
        }
        if ($d -gt 0) { $samples += $d }
        Start-Sleep -Seconds 2
    }
    if (-not $samples.Count) { Write-Host "`r  $cap MHz: no samples"; continue }

    $sorted = $samples | Sort-Object
    $median = $sorted[[int]($sorted.Count / 2)]
    $results += [pscustomobject]@{
        Cap = $cap; MedianmW = $median
        MinmW = $sorted[0]; MaxmW = $sorted[-1]; Samples = $samples.Count
    }
    Write-Host ("`r  {0,-10} {1,6} mW median   ({2}-{3} mW over {4} samples)" -f
        "$cap MHz", $median, $sorted[0], $sorted[-1], $samples.Count)
}

& python -m aerofan.cli cpu $startCap 2>&1 | Out-Null

Write-Host "`n  Results" -ForegroundColor White
$best = $results | Sort-Object MedianmW | Select-Object -First 1
$worst = $results | Sort-Object MedianmW -Descending | Select-Object -First 1
foreach ($r in $results | Sort-Object Cap) {
    $label = if ($r.Cap -eq 0) { 'unlimited' } else { "$($r.Cap) MHz" }
    $delta = if ($worst.MedianmW) { 100 * ($worst.MedianmW - $r.MedianmW) / $worst.MedianmW } else { 0 }
    Write-Host ("    {0,-12} {1,6} mW    {2,5:n1}% better than the worst" -f
        $label, $r.MedianmW, $delta)
}

if ($best.MedianmW -gt 0) {
    $wh = 94.0    # AERO 15 design capacity; adjust if yours differs
    Write-Host ("`n    Lowest draw was {0} at {1} mW - roughly {2:n1} hours from a full {3} Wh battery." -f
        $(if ($best.Cap -eq 0) { 'unlimited' } else { "$($best.Cap) MHz" }),
        $best.MedianmW, ($wh * 1000 / $best.MedianmW), $wh)
    Write-Host "    Compare that against how the machine felt at each setting - the"
    Write-Host "    cheapest cap is not worth much if it makes typing feel sticky.`n"
}
