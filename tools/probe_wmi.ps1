# probe_wmi.ps1 - read-only survey of ACPI/WMI surfaces that might expose fans.
$ErrorActionPreference = 'SilentlyContinue'

Write-Output "=== root\wmi classes of interest ==="
Get-CimClass -Namespace root\wmi |
    Select-Object -ExpandProperty CimClassName |
    Where-Object { $_ -match 'GB|Gigabyte|AMW|Acpi|Fan|Thermal' } |
    Sort-Object

Write-Output ""
Write-Output "=== ACPI WMI-mapper devices ==="
Get-CimInstance Win32_PnPEntity |
    Where-Object { $_.Name -match 'WMI|Mapper' } |
    Select-Object Name, DeviceID | Format-Table -AutoSize | Out-String -Width 200

Write-Output ""
Write-Output "=== MSAcpi_ThermalZoneTemperature ==="
Get-CimInstance -Namespace root\wmi -ClassName MSAcpi_ThermalZoneTemperature |
    Select-Object InstanceName, CurrentTemperature | Format-Table -AutoSize | Out-String -Width 200

Write-Output ""
Write-Output "=== Win32_Fan ==="
Get-CimInstance Win32_Fan | Select-Object Name, DesiredSpeed, VariableSpeed | Format-Table -AutoSize | Out-String -Width 200

Write-Output ""
Write-Output "=== Gigabyte / control software services ==="
Get-Service | Where-Object { $_.Name -match 'Gigabyte|GCC|AORUS|Aero|Fusion|LHM|OpenHardware' } |
    Select-Object Name, DisplayName, Status | Format-Table -AutoSize | Out-String -Width 200

Write-Output ""
Write-Output "=== Kernel-mode IO drivers installed ==="
Get-ChildItem C:\Windows\System32\drivers -Filter *.sys |
    Where-Object { $_.Name -match 'WinRing0|inpout|RwDrv|GLCKIo|EIO|nbfc|HwRwDrv|amifldrv' } |
    Select-Object Name, Length | Format-Table -AutoSize | Out-String -Width 200

Write-Output "=== done ==="
