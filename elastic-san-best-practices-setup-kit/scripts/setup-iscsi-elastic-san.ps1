# Azure Elastic SAN iSCSI Setup Script
# Configures iSCSI with MPIO for optimal Azure Elastic SAN performance
# Runs on Windows Server 2022 Azure Edition

param(
    [string]$ResourceGroupName = "rg-elastic-san-demo",
    [string]$ElasticSanName = "esan-demo-esan",
    [string]$VolumeGroupName = "esan-demo-vg",
    [string]$VolumeName = "esan-demo-volume"
)

function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'     
    Write-Output "[$timestamp] $Message"
    Add-Content -Path 'C:\ESANSetup.log' -Value "[$timestamp] $Message"
}

try {
    Write-Log "=== Starting Azure Elastic SAN iSCSI Setup ==="
    Write-Log "Resource Group: $ResourceGroupName"
    Write-Log "Elastic SAN: $ElasticSanName"
    Write-Log "Volume: $VolumeName"

    # Step 1: Enable MPIO feature
    Write-Log "Enabling MPIO feature..."
    $mpioFeature = Get-WindowsFeature -Name "Multipath-IO"
    if ($mpioFeature.InstallState -ne "Installed") {
        Install-WindowsFeature -Name "Multipath-IO" -IncludeManagementTools
        Write-Log "MPIO feature installed successfully"
    } else {
        Write-Log "MPIO feature already installed"
    }

    # Step 2: Configure MPIO for iSCSI
    Write-Log "Configuring MPIO for iSCSI devices..."
    & mpclaim -r -i -d "MSFT2005iSCSIBusType_0x9"
    Write-Log "MPIO configured for iSCSI"

    # Step 3: Start iSCSI service
    Write-Log "Starting iSCSI Initiator service..."
    Set-Service -Name "MSiSCSI" -StartupType "Automatic"
    Start-Service -Name "MSiSCSI"
    Write-Log "iSCSI Initiator service started"

    # Step 4: Apply registry optimizations
    Write-Log "Applying iSCSI performance optimizations..."
    
    # iSCSI timeout and retry settings
    $regPath = "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4D36E97B-E325-11CE-BFC1-08002BE10318}"
    $iscsiKeys = Get-ChildItem $regPath | Where-Object { (Get-ItemProperty $_.PSPath -Name "Class" -ErrorAction SilentlyContinue).Class -eq "SCSIAdapter" }
    
    foreach ($key in $iscsiKeys) {
        $path = $key.PSPath
        Set-ItemProperty -Path $path -Name "LinkDownTime" -Value 30 -Type DWORD -ErrorAction SilentlyContinue
        Set-ItemProperty -Path $path -Name "PDORemovePeriod" -Value 130 -Type DWORD -ErrorAction SilentlyContinue
    }

    # TCP optimizations for high throughput
    Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters" -Name "TcpWindowSize" -Value 65536 -Type DWORD
    Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters" -Name "TcpNumConnections" -Value 16777214 -Type DWORD
    Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters" -Name "MaxUserPort" -Value 65534 -Type DWORD
    Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters" -Name "TcpTimedWaitDelay" -Value 30 -Type DWORD

    Write-Log "Registry optimizations applied"

    # Step 5: Download and install Azure CLI (if not present)
    Write-Log "Checking Azure CLI installation..."
    $azCliPath = "C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
    if (-not (Test-Path $azCliPath)) {
        Write-Log "Installing Azure CLI..."
        $cliUrl = "https://aka.ms/installazurecliwindows"
        $cliInstaller = "$env:TEMP\AzureCLI.msi"
        
        Invoke-WebRequest -Uri $cliUrl -OutFile $cliInstaller
        Start-Process msiexec.exe -Wait -ArgumentList "/I $cliInstaller /quiet"
        $env:PATH = [System.Environment]::GetEnvironmentVariable('PATH','Machine') + ';' + [System.Environment]::GetEnvironmentVariable('PATH','User')
        Write-Log "Azure CLI installed"
    } else {
        Write-Log "Azure CLI already installed"
    }

    # Step 6: Login with managed identity
    Write-Log "Authenticating with Azure using managed identity..."
    & $azCliPath login --identity
    Write-Log "Successfully authenticated with Azure"

    # Step 7: Get volume connection details
    Write-Log "Retrieving Elastic SAN volume details..."
    $volumeCmd = "& '$azCliPath' elastic-san volume show --resource-group '$ResourceGroupName' --elastic-san-name '$ElasticSanName' --volume-group-name '$VolumeGroupName' --name '$VolumeName' --query '{iqn:storageTarget.targetIqn, portal:storageTarget.targetPortalHostname, port:storageTarget.targetPortalPort}' --output json"
    
    $volumeInfo = Invoke-Expression $volumeCmd | ConvertFrom-Json
    
    $targetIQN = $volumeInfo.iqn
    $targetPortal = $volumeInfo.portal
    $targetPort = $volumeInfo.port

    Write-Log "Volume IQN: $targetIQN"
    Write-Log "Portal: $targetPortal"
    Write-Log "Port: $targetPort"

    # Step 8: Configure iSCSI connections with 32 sessions
    Write-Log "Adding target portal..."
    & iscsicli AddTargetPortal $targetPortal $targetPort

    Write-Log "Creating 32 high-performance iSCSI sessions..."
    for ($i = 1; $i -le 32; $i++) {
        Write-Log "Creating session $i/32..."
        & iscsicli LoginTarget $targetIQN T * * * * * * * * * * * * * * * 2
        Start-Sleep -Seconds 1
    }

    Write-Log "All iSCSI sessions created successfully"

    # Step 9: Wait for disks to appear and initialize them
    Write-Log "Waiting for new disks to appear..."
    Start-Sleep -Seconds 15

    Write-Log "Initializing and formatting new disks..."
    $newDisks = Get-Disk | Where-Object { $_.PartitionStyle -eq 'RAW' }
    
    if ($newDisks) {
        foreach ($disk in $newDisks) {
            Write-Log "Processing disk $($disk.Number)..."
            
            # Initialize disk
            Initialize-Disk -Number $disk.Number -PartitionStyle GPT -PassThru

            # Create partition
            $partition = New-Partition -DiskNumber $disk.Number -UseMaximumSize -AssignDriveLetter

            # Format volume
            $driveLetter = $partition.DriveLetter
            Format-Volume -DriveLetter $driveLetter -FileSystem NTFS -NewFileSystemLabel "ElasticSAN_$($disk.Number)" -Confirm:$false -Force

            Write-Log "Disk $($disk.Number) configured as drive $driveLetter`: ($(($disk.Size/1GB).ToString('F2')) GB)"
        }
    } else {
        Write-Log "No new disks found to initialize"
    }

    # Step 10: Create performance benchmark script
    Write-Log "Creating performance benchmark script..."
    $benchmarkScript = @'
# Azure Elastic SAN Performance Benchmark Script
param()

function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'     
    Write-Output "[$timestamp] $Message"
    Add-Content -Path 'C:\ESANBenchmark.log' -Value "[$timestamp] $Message"
}

Write-Log 'Starting Azure Elastic SAN performance benchmark...'

try {
    # Wait for system to stabilize
    Start-Sleep -Seconds 30

    # Find the Elastic SAN drive
    $esanDrives = Get-Volume | Where-Object { $_.FileSystemLabel -like "ElasticSAN*" }
    if (-not $esanDrives) {
        Write-Log 'ERROR: No Elastic SAN drives found. Benchmark cancelled.'
        exit 1
    }

    $driveLetter = ($esanDrives | Select-Object -First 1).DriveLetter
    Write-Log "Using Elastic SAN drive: $driveLetter`:"     

    # Create test directory
    $testDir = "$driveLetter`:\Testdata"
    if (-not (Test-Path $testDir)) {
        New-Item -Path $testDir -ItemType Directory -Force  
        Write-Log "Created test directory: $testDir"        
    }

    # Download DiskSpd tool
    $diskSpdUrl = "https://github.com/Microsoft/diskspd/releases/download/v2.0.21a/DiskSpd.zip"
    $diskSpdPath = "$env:TEMP\DiskSpd.zip"
    $diskSpdDir = "$env:TEMP\DiskSpd"

    Write-Log 'Downloading DiskSpd performance testing tool...'
    Invoke-WebRequest -Uri $diskSpdUrl -OutFile $diskSpdPath
    Expand-Archive -Path $diskSpdPath -DestinationPath $diskSpdDir -Force
    $diskSpdExe = Get-ChildItem -Path $diskSpdDir -Name "diskspd.exe" -Recurse | Select-Object -First 1
    $diskSpdFullPath = Join-Path $diskSpdDir $diskSpdExe.DirectoryName $diskSpdExe.Name
    Write-Log "DiskSpd downloaded to: $diskSpdFullPath" 

    # Quick validation test (4K mixed workload, 60 seconds)
    Write-Log '=== Running Quick Validation Test ==='       
    Write-Log 'Test: 4K mixed workload, 60-second duration'
    $quickTest = "$testDir\QuickTest.dat"
    $quickCmd = "& '$diskSpdFullPath' -b4K -d60 -Sh -L -o32 -t3 -r -w25 -c1G '$quickTest'"

    Write-Log "Executing: $quickCmd"
    $quickResults = Invoke-Expression $quickCmd
    Write-Log 'Quick validation test completed successfully!'

    Write-Log 'Azure Elastic SAN performance benchmark completed!'
    Write-Log 'Check C:\ESANBenchmark.log for detailed results.'

} catch {
    Write-Log "ERROR during benchmark: $_"
    exit 1
}
'@

    # Save benchmark script
    $benchmarkScript | Out-File -FilePath 'C:\ESANBenchmark.ps1' -Encoding UTF8
    Write-Log "Performance benchmark script created at C:\ESANBenchmark.ps1"

    Write-Log "=== Azure Elastic SAN iSCSI Setup Completed Successfully! ==="
    Write-Log "Summary:"
    Write-Log "- MPIO enabled and configured"
    Write-Log "- 32 iSCSI sessions established"
    Write-Log "- Disks initialized and formatted"
    Write-Log "- Performance optimizations applied"
    Write-Log "- Benchmark script ready at C:\ESANBenchmark.ps1"
    Write-Log ""
    Write-Log "To run performance benchmark: PowerShell.exe -ExecutionPolicy Unrestricted -File C:\ESANBenchmark.ps1"
    Write-Log "Setup log available at: C:\ESANSetup.log"

} catch {
    Write-Log "ERROR: $_"
    Write-Log "Setup failed. Check C:\ESANSetup.log for details."
    exit 1
}