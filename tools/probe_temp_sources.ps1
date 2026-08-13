# Which CPU temperature sources exist outside the EC?
$ErrorActionPreference = 'SilentlyContinue'

Write-Output "=== Thermal Zone perf counters ==="
try {
    (Get-Counter '\Thermal Zone Information(*)\Temperature' -ErrorAction Stop).CounterSamples |
        Select-Object InstanceName, CookedValue | Format-Table -AutoSize | Out-String -Width 200
} catch { Write-Output "  unavailable: $($_.Exception.Message)" }

Write-Output "=== MSAcpi_ThermalZoneTemperature ==="
$tz = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature
if ($tz) { $tz | Select-Object InstanceName, CurrentTemperature | Format-Table -AutoSize | Out-String -Width 200 }
else { Write-Output "  none" }

Write-Output "=== Win32_TemperatureProbe ==="
$tp = Get-CimInstance Win32_TemperatureProbe
if ($tp) { $tp | Select-Object Name, CurrentReading | Format-Table -AutoSize | Out-String -Width 200 }
else { Write-Output "  none" }

Write-Output "=== Processor perf (proxy for load, not temp) ==="
(Get-CimInstance Win32_Processor | Select-Object Name, LoadPercentage, CurrentClockSpeed, MaxClockSpeed) |
    Format-List | Out-String -Width 200

Write-Output "=== done ==="
