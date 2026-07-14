# Elastic SAN Best Practices Validation Script
# Run this script on the deployed Windows VM to validate the iSCSI MPIO setup
# Checks Azure Elastic SAN best practice configurations

Write-Host "=== Azure Elastic SAN Best Practices Validation ===" -ForegroundColor Green
Write-Host "Validating iSCSI MPIO configuration and optimization settings..." -ForegroundColor Yellow
Write-Host ""

# Check iSCSI service
Write-Host "1. iSCSI Service Status:" -ForegroundColor Cyan
$iscsiService = Get-Service -Name MSiSCSI
Write-Host "   Status: $($iscsiService.Status)" -ForegroundColor $(if($iscsiService.Status -eq 'Running') {'Green'} else {'Red'})
Write-Host "   Startup Type: $($iscsiService.StartType)" -ForegroundColor $(if($iscsiService.StartType -eq 'Automatic') {'Green'} else {'Yellow'})
Write-Host ""

# Check MPIO installation and configuration
Write-Host "2. Multipath I/O Status:" -ForegroundColor Cyan
$mpioFeature = Get-WindowsFeature -Name 'Multipath-IO'
Write-Host "   Install State: $($mpioFeature.InstallState)" -ForegroundColor $(if($mpioFeature.InstallState -eq 'Installed') {'Green'} else {'Red'})

# Check MPIO disk timeout (should be 30 seconds per best practices)
try {
    $mpioSettings = Get-MPIOSetting -ErrorAction SilentlyContinue
    if ($mpioSettings) {
        Write-Host "   Disk Timeout: $($mpioSettings.NewDiskTimeout) seconds" -ForegroundColor $(if($mpioSettings.NewDiskTimeout -eq 30) {'Green'} else {'Yellow'})
    }
} catch {
    Write-Host "   Disk Timeout: Unable to verify" -ForegroundColor Yellow
}
Write-Host ""

# Check best practice registry settings
Write-Host "3. iSCSI Registry Optimizations:" -ForegroundColor Cyan
$registryPath = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e97b-e325-11ce-bfc1-08002be10318}\0004\Parameters'

if (Test-Path $registryPath) {
    $settings = @{
        'MaxTransferLength' = 262144
        'MaxBurstLength' = 262144
        'FirstBurstLength' = 262144
        'MaxRecvDataSegmentLength' = 262144
        'InitialR2T' = 0
        'ImmediateData' = 1
        'WMIRequestTimeout' = 30
        'LinkDownTime' = 30
    }
    
    foreach ($setting in $settings.GetEnumerator()) {
        $currentValue = Get-ItemProperty -Path $registryPath -Name $setting.Key -ErrorAction SilentlyContinue
        if ($currentValue) {
            $actualValue = $currentValue.($setting.Key)
            $status = if ($actualValue -eq $setting.Value) {'Green'} else {'Yellow'}
            $statusText = if ($actualValue -eq $setting.Value) {'✓ Optimized'} else {"⚠ $actualValue (expected $($setting.Value))"}
            Write-Host "   $($setting.Key): $statusText" -ForegroundColor $status
        } else {
            Write-Host "   $($setting.Key): ❌ Not configured" -ForegroundColor Red
        }
    }
} else {
    Write-Host "   ❌ Registry path not found" -ForegroundColor Red
}
Write-Host ""

# Check Accelerated Networking status
Write-Host "4. Network Optimization:" -ForegroundColor Cyan
try {
    $netAdapters = Get-NetAdapter | Where-Object { $_.Status -eq 'Up' -and $_.PhysicalMediaType -ne 'Unspecified' }
    foreach ($adapter in $netAdapters) {
        $vmqEnabled = (Get-NetAdapterVmq -Name $adapter.Name -ErrorAction SilentlyContinue).Enabled
        $rssenabled = (Get-NetAdapterRss -Name $adapter.Name -ErrorAction SilentlyContinue).Enabled
        Write-Host "   $($adapter.Name):" -ForegroundColor White
        Write-Host "     VMQ (Accelerated Networking): $vmqEnabled" -ForegroundColor $(if($vmqEnabled) {'Green'} else {'Yellow'})
        Write-Host "     RSS (Receive Side Scaling): $rssenabled" -ForegroundColor $(if($rssenabled) {'Green'} else {'Yellow'})
    }
} catch {
    Write-Host "   Unable to verify network optimizations" -ForegroundColor Yellow
}
Write-Host ""

# Check iSCSI sessions
Write-Host "5. iSCSI Sessions:" -ForegroundColor Cyan
try {
    $sessions = & iscsicli SessionList
    $sessionCount = ($sessions | Where-Object { $_ -match "Session Id" }).Count
    Write-Host "   Active Sessions: $sessionCount" -ForegroundColor $(if($sessionCount -ge 32) {'Green'} elseif($sessionCount -gt 0) {'Yellow'} else {'Red'})
    
    if ($sessionCount -gt 0) {
        Write-Host "   Session Details:" -ForegroundColor White
        $sessions | Where-Object { $_ -match "Session Id|Target Name" } | ForEach-Object {
            Write-Host "     $_" -ForegroundColor Gray
        }
    }
} catch {
    Write-Host "   Error retrieving session information: $_" -ForegroundColor Red
}
Write-Host ""

# Check MPIO devices
if ($mpioFeature.InstallState -eq 'Installed') {
    Write-Host "6. MPIO Device Status:" -ForegroundColor Cyan
    try {
        $mpioStatus = & mpclaim -s -d
        Write-Host "   MPIO Configuration:" -ForegroundColor White
        $mpioStatus | ForEach-Object {
            Write-Host "     $_" -ForegroundColor Gray
        }
    } catch {
        Write-Host "   Error retrieving MPIO information: $_" -ForegroundColor Red
    }
    Write-Host ""
}

# Check connected disks
Write-Host "7. Connected Disks:" -ForegroundColor Cyan
$disks = Get-Disk | Where-Object { $_.BusType -eq 'iSCSI' -or $_.FriendlyName -like "*Elastic*" }
if ($disks) {
    foreach ($disk in $disks) {
        $volumes = Get-Partition -DiskNumber $disk.Number | Get-Volume -ErrorAction SilentlyContinue
        Write-Host "   Disk $($disk.Number): $($disk.Size / 1GB) GB, $($disk.OperationalStatus)" -ForegroundColor Green
        foreach ($volume in $volumes) {
            if ($volume.DriveLetter) {
                $freeSpace = [math]::Round($volume.SizeRemaining / 1GB, 2)
                $totalSpace = [math]::Round($volume.Size / 1GB, 2)
                Write-Host "     Drive $($volume.DriveLetter): $freeSpace GB free of $totalSpace GB total" -ForegroundColor White
            }
        }
    }
} else {
    Write-Host "   No iSCSI disks found" -ForegroundColor Yellow
}
Write-Host ""

# Check setup log
Write-Host "8. Setup Log:" -ForegroundColor Cyan
if (Test-Path "C:\ESANSetup.log") {
    Write-Host "   Log file exists: C:\ESANSetup.log" -ForegroundColor Green
    $logContent = Get-Content "C:\ESANSetup.log" -Tail 10
    Write-Host "   Last 10 lines:" -ForegroundColor White
    $logContent | ForEach-Object {
        Write-Host "     $_" -ForegroundColor Gray
    }
} else {
    Write-Host "   Log file not found at C:\ESANSetup.log" -ForegroundColor Yellow
}
Write-Host ""

# Performance recommendations
Write-Host "9. Performance Optimization Status:" -ForegroundColor Cyan
Write-Host "   ✓ Check 32 iSCSI sessions are active for maximum performance" -ForegroundColor White
Write-Host "   ✓ Verify 256KB transfer sizes are configured" -ForegroundColor White
Write-Host "   ✓ Confirm MPIO disk timeout is set to 30 seconds" -ForegroundColor White
Write-Host "   ✓ Ensure Accelerated Networking is enabled" -ForegroundColor White
Write-Host "   • Use DiskSpd or Crystal DiskMark to validate performance" -ForegroundColor White
Write-Host "   • Monitor with Performance Monitor (perfmon)" -ForegroundColor White
Write-Host ""

# Summary with enhanced checks
Write-Host "=== Best Practices Validation Summary ===" -ForegroundColor Green
$issues = @()
$warnings = @()

if ($iscsiService.Status -ne 'Running') {
    $issues += "iSCSI service not running"
}
if ($mpioFeature.InstallState -ne 'Installed') {
    $issues += "MPIO not installed"
}
if ($sessionCount -lt 1) {
    $issues += "No iSCSI sessions active"
} elseif ($sessionCount -lt 32) {
    $warnings += "Less than 32 sessions (performance may not be optimal)"
}
if (-not $disks) {
    $issues += "No iSCSI disks detected"
}

# Check if registry optimizations were applied
if (Test-Path $registryPath) {
    $maxTransfer = Get-ItemProperty -Path $registryPath -Name 'MaxTransferLength' -ErrorAction SilentlyContinue
    if (-not $maxTransfer -or $maxTransfer.MaxTransferLength -ne 262144) {
        $warnings += "Registry optimizations may not be fully applied"
    }
} else {
    $warnings += "Registry optimization path not found"
}

if ($issues.Count -eq 0 -and $warnings.Count -eq 0) {
    Write-Host "✅ All best practice checks passed! Elastic SAN is optimally configured." -ForegroundColor Green
} elseif ($issues.Count -eq 0) {
    Write-Host "⚠️  Setup complete with minor optimization opportunities:" -ForegroundColor Yellow
    $warnings | ForEach-Object {
        Write-Host "   - $_" -ForegroundColor Yellow
    }
} else {
    Write-Host "❌ Issues found:" -ForegroundColor Red
    $issues | ForEach-Object {
        Write-Host "   - $_" -ForegroundColor Red
    }
    if ($warnings.Count -gt 0) {
        Write-Host "⚠️  Additional warnings:" -ForegroundColor Yellow
        $warnings | ForEach-Object {
            Write-Host "   - $_" -ForegroundColor Yellow
        }
    }
}

Write-Host ""
Write-Host "📋 For detailed troubleshooting and logs:" -ForegroundColor Cyan  
Write-Host "   • Setup log: C:\ESANSetup.log" -ForegroundColor White
Write-Host "   • Best practices guide: https://learn.microsoft.com/azure/storage/elastic-san/elastic-san-best-practices" -ForegroundColor White
Write-Host "   • Performance testing: Use DiskSpd or Crystal DiskMark" -ForegroundColor White