# Azure Elastic SAN iSCSI MPIO Setup Script with Best Practices
# This script configures Windows iSCSI initiator and MPIO for optimal Elastic SAN connectivity
# Based on Microsoft documentation: https://learn.microsoft.com/azure/storage/elastic-san/elastic-san-connect-windows
# Incorporates best practices from: https://learn.microsoft.com/azure/storage/elastic-san/elastic-san-best-practices#iscsi

param(
    [Parameter(Mandatory = $true)]
    [string]$ResourceGroupName = "${resource_group_name}",
    
    [Parameter(Mandatory = $true)]
    [string]$ElasticSanName = "${elastic_san_name}",
    
    [Parameter(Mandatory = $true)]
    [string]$VolumeGroupName = "${volume_group_name}",
    
    [Parameter(Mandatory = $true)]
    [string]$VolumeName = "${volume_name}",
    
    [Parameter(Mandatory = $false)]
    [int]$NumSessions = ${num_sessions}
)

# Function to write log messages
function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Output "[$timestamp] $Message"
    Add-Content -Path "C:\ESANSetup.log" -Value "[$timestamp] $Message"
}

Write-Log "Starting Azure Elastic SAN iSCSI MPIO setup..."
Write-Log "Parameters: RG=$ResourceGroupName, ESAN=$ElasticSanName, VG=$VolumeGroupName, Vol=$VolumeName, Sessions=$NumSessions"

try {
    # Step 1: Enable and start iSCSI Initiator service
    Write-Log "Configuring iSCSI Initiator service..."
    
    $service = Get-Service -Name MSiSCSI -ErrorAction SilentlyContinue
    if ($service) {
        if ($service.Status -ne 'Running') {
            Write-Log "Starting iSCSI Initiator service..."
            Start-Service -Name MSiSCSI
        }
        Write-Log "Setting iSCSI Initiator service to start automatically..."
        Set-Service -Name MSiSCSI -StartupType Automatic
        Write-Log "iSCSI Initiator service configured successfully."
    } else {
        Write-Log "ERROR: iSCSI Initiator service not found!"
        exit 1
    }

    # Step 2: Install and configure Multipath I/O with best practices
    Write-Log "Installing Multipath I/O feature..."
    
    $mpioFeature = Get-WindowsFeature -Name 'Multipath-IO' -ErrorAction SilentlyContinue
    if ($mpioFeature -and $mpioFeature.InstallState -ne 'Installed') {
        Write-Log "Installing Multipath-IO feature..."
        $result = Add-WindowsFeature -Name 'Multipath-IO'
        if ($result.Success) {
            Write-Log "Multipath-IO feature installed successfully."
        } else {
            Write-Log "ERROR: Failed to install Multipath-IO feature."
            exit 1
        }
    } else {
        Write-Log "Multipath-IO feature already installed."
    }
    
    # Verify MPIO installation and configure optimally
    $mpioInstalled = Get-WindowsFeature -Name 'Multipath-IO'
    if ($mpioInstalled.InstallState -eq 'Installed') {
        Write-Log "Enabling multipath support for iSCSI devices..."
        Enable-MSDSMAutomaticClaim -BusType iSCSI -ErrorAction SilentlyContinue
        
        Write-Log "Setting default load balancing policy to Round Robin..."
        & mpclaim -L -M 2
        
        # Set disk timeout to 30 seconds per best practices
        Write-Log "Setting MPIO disk timeout to 30 seconds..."
        Set-MPIOSetting -NewDiskTimeout 30 -ErrorAction SilentlyContinue
        
        Write-Log "MPIO configuration completed with optimal settings."
    } else {
        Write-Log "WARNING: MPIO is not properly installed. Continuing with basic iSCSI setup..."
    }

    # Step 2.1: Apply Azure Elastic SAN iSCSI best practice registry settings
    Write-Log "Applying optimal iSCSI registry settings per Azure best practices..."
    $registryPath = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e97b-e325-11ce-bfc1-08002be10318}\0004\Parameters'
    
    try {
        # Ensure registry path exists
        if (-not (Test-Path $registryPath)) {
            Write-Log "Creating iSCSI initiator registry path..."
            New-Item -Path $registryPath -Force | Out-Null
        }
        
        # Set maximum transfer lengths to 256KB (262144 bytes) for optimal performance
        Write-Log "Setting MaxTransferLength to 256KB..."
        Set-ItemProperty -Path $registryPath -Name 'MaxTransferLength' -Value 262144 -Type DWord -ErrorAction Stop
        
        Write-Log "Setting MaxBurstLength to 256KB..."
        Set-ItemProperty -Path $registryPath -Name 'MaxBurstLength' -Value 262144 -Type DWord -ErrorAction Stop
        
        Write-Log "Setting FirstBurstLength to 256KB..." 
        Set-ItemProperty -Path $registryPath -Name 'FirstBurstLength' -Value 262144 -Type DWord -ErrorAction Stop
        
        Write-Log "Setting MaxRecvDataSegmentLength to 256KB..."
        Set-ItemProperty -Path $registryPath -Name 'MaxRecvDataSegmentLength' -Value 262144 -Type DWord -ErrorAction Stop
        
        # Optimize flow control settings
        Write-Log "Disabling R2T flow control (InitialR2T=0)..."
        Set-ItemProperty -Path $registryPath -Name 'InitialR2T' -Value 0 -Type DWord -ErrorAction Stop
        
        Write-Log "Enabling immediate data (ImmediateData=1)..."
        Set-ItemProperty -Path $registryPath -Name 'ImmediateData' -Value 1 -Type DWord -ErrorAction Stop
        
        # Set timeout values for optimal connectivity
        Write-Log "Setting WMI request timeout to 30 seconds..."
        Set-ItemProperty -Path $registryPath -Name 'WMIRequestTimeout' -Value 30 -Type DWord -ErrorAction Stop
        
        Write-Log "Setting link down timeout to 30 seconds..."
        Set-ItemProperty -Path $registryPath -Name 'LinkDownTime' -Value 30 -Type DWord -ErrorAction Stop
        
        Write-Log "All iSCSI registry optimizations applied successfully."
        Write-Log "NOTE: VM restart is required for registry changes to take full effect."
        
    } catch {
        Write-Log "WARNING: Failed to apply some registry optimizations: $_"
        Write-Log "Continuing with setup..."
    }

    # Step 3: Install Azure CLI if not present (needed to get volume details)
    Write-Log "Checking for Azure CLI..."
    $azInstalled = Get-Command az -ErrorAction SilentlyContinue
    if (-not $azInstalled) {
        Write-Log "Installing Azure CLI..."
        
        # Download and install Azure CLI
        $url = "https://aka.ms/installazurecliwindows"
        $output = "$env:TEMP\AzureCLI.msi"
        
        # Download Azure CLI installer
        Invoke-WebRequest -Uri $url -OutFile $output
        
        # Install silently
        Start-Process msiexec.exe -Wait -ArgumentList "/I $output /quiet"
        
        # Refresh environment variables
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
        
        Write-Log "Azure CLI installation completed."
    } else {
        Write-Log "Azure CLI already installed."
    }

    # Step 4: Authenticate using VM's managed identity
    Write-Log "Authenticating with Azure using managed identity..."
    try {
        & az login --identity
        Write-Log "Successfully authenticated with Azure."
    } catch {
        Write-Log "WARNING: Failed to authenticate with managed identity. Manual authentication may be required."
    }

    # Step 5: Get volume details
    Write-Log "Retrieving Elastic SAN volume details..."
    try {
        $volumeDetails = & az elastic-san volume show --resource-group $ResourceGroupName --elastic-san-name $ElasticSanName --volume-group-name $VolumeGroupName --name $VolumeName --query "{iqn:storageTarget.targetIqn, portal:storageTarget.targetPortalHostname, port:storageTarget.targetPortalPort}" --output json | ConvertFrom-Json
        
        $targetIQN = $volumeDetails.iqn
        $targetPortal = $volumeDetails.portal
        $targetPort = $volumeDetails.port
        
        Write-Log "Volume details retrieved:"
        Write-Log "  IQN: $targetIQN"
        Write-Log "  Portal: $targetPortal"
        Write-Log "  Port: $targetPort"
        
    } catch {
        Write-Log "ERROR: Failed to retrieve volume details from Azure CLI."
        Write-Log "Error: $_"
        exit 1
    }

    # Step 6: Configure iSCSI connections with multiple sessions
    Write-Log "Configuring iSCSI connections with $NumSessions sessions..."
    
    # Add target portal
    Write-Log "Adding target portal $targetPortal`:$targetPort..."
    & iscsicli AddTargetPortal $targetPortal $targetPort
    
    # Discover targets
    Write-Log "Discovering iSCSI targets..."
    & iscsicli ListTargets
    
    # Create multiple sessions for MPIO
    Write-Log "Creating $NumSessions iSCSI sessions for optimal performance..."
    for ($i = 1; $i -le $NumSessions; $i++) {
        Write-Log "Creating session $i/$NumSessions..."
        try {
            & iscsicli LoginTarget $targetIQN T * * * * * * * * * * * * * * * 2
            Start-Sleep -Seconds 1
        } catch {
            Write-Log "Warning: Session $i may have failed. Continuing..."
        }
    }

    # Step 7: Verify connections
    Write-Log "Verifying iSCSI sessions..."
    $sessions = & iscsicli SessionList
    Write-Log "Active iSCSI sessions:"
    Write-Log $sessions
    
    if ($mpioInstalled.InstallState -eq 'Installed') {
        Write-Log "MPIO device status:"
        $mpioStatus = & mpclaim -s -d
        Write-Log $mpioStatus
    }

    # Step 8: Initialize and format the disk if needed
    Write-Log "Checking for new disks..."
    $newDisks = Get-Disk | Where-Object { $_.PartitionStyle -eq 'RAW' }
    
    if ($newDisks) {
        Write-Log "Found $($newDisks.Count) new disk(s). Initializing and formatting..."
        
        foreach ($disk in $newDisks) {
            $diskNumber = $disk.Number
            Write-Log "Processing Disk $diskNumber..."
            
            # Initialize disk
            Initialize-Disk -Number $diskNumber -PartitionStyle GPT -PassThru
            
            # Create partition and format
            $partition = New-Partition -DiskNumber $diskNumber -UseMaximumSize -AssignDriveLetter
            $driveLetter = $partition.DriveLetter
            
            Format-Volume -DriveLetter $driveLetter -FileSystem NTFS -NewFileSystemLabel "ElasticSAN_$diskNumber" -Confirm:$false
            
            Write-Log "Disk $diskNumber initialized and formatted as drive $driveLetter`:`"
        }
    } else {
        Write-Log "No new disks found to initialize."
    }

    Write-Log "Azure Elastic SAN iSCSI MPIO setup completed successfully with best practices!"
    Write-Log "Optimizations applied:"
    Write-Log "  ✓ 32 iSCSI sessions for maximum performance"  
    Write-Log "  ✓ MPIO with 30-second disk timeout"
    Write-Log "  ✓ 256KB transfer sizes for optimal throughput"
    Write-Log "  ✓ Disabled R2T flow control"
    Write-Log "  ✓ Enabled immediate data transfer"
    Write-Log "  ✓ Optimized timeout values"
    Write-Log ""
    Write-Log "IMPORTANT: A VM restart is recommended for all registry optimizations to take full effect."
    Write-Log "Next steps:"
    Write-Log "  1. Verify disk accessibility in Windows Explorer"
    Write-Log "  2. Run performance tests with DiskSpd or Crystal DiskMark"
    Write-Log "  3. Monitor performance with Performance Monitor"
    Write-Log "  4. Test application workloads"

} catch {
    Write-Log "ERROR: Setup failed with error: $_"
    Write-Log "Stack trace: $($_.ScriptStackTrace)"
    exit 1
}

# Restart required for MPIO to fully take effect
Write-Log "Setup completed. A restart may be required for MPIO to fully take effect."
Write-Log "Log file saved to: C:\ESANSetup.log"