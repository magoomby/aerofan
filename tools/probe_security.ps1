# probe_security.ps1 - what will stop an unsigned/blocklisted kernel driver loading.
$ErrorActionPreference = 'SilentlyContinue'

Write-Output "=== Memory Integrity (HVCI) ==="
$p = 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity'
Get-ItemProperty -Path $p | Select-Object Enabled, WasEnabledBy | Format-List | Out-String -Width 200

Write-Output "=== DeviceGuard running status ==="
Get-CimInstance -ClassName Win32_DeviceGuard -Namespace root\Microsoft\Windows\DeviceGuard |
    Select-Object SecurityServicesRunning, VirtualizationBasedSecurityStatus, CodeIntegrityPolicyEnforcementStatus |
    Format-List | Out-String -Width 200

Write-Output "=== Vulnerable driver blocklist ==="
$c = 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Config'
Get-ItemProperty -Path $c | Select-Object VulnerableDriverBlocklistEnable | Format-List | Out-String -Width 200

Write-Output "=== Secure Boot ==="
Confirm-SecureBootUEFI

Write-Output "=== Test signing / boot config ==="
Write-Output "(bcdedit blocked by DC; check manually if needed)"

Write-Output "=== Python + pip ==="
python --version
python -c "import sys; print(sys.executable)"
pip --version

Write-Output "=== done ==="
