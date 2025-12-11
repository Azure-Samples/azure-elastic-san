$vgname = "avs-gen10-vg1"
$rgname = "avs-esan-testing-rg"
$esname = "avs-gen10-testing" 
$subscriptionId = "dd80b94e-0463-4a65-8d04-c94f403879dc"
 
# Check dependency
$title    = 'Confirm'
$choices = @(
    [System.Management.Automation.Host.ChoiceDescription]::new("&Yes to terminate", "Yes to terminate")
    [System.Management.Automation.Host.ChoiceDescription]::new("&No to proceed with rest of the steps", "No to proceed with rest of the steps")
)
 
## iSCSI initiator
$iscsiWarning = $false
try {
    $checkResult = Get-Service -Name MSiSCSI -ErrorAction Stop
} catch {
    $iscsiWarning = $true
}
if (($checkResult.Status -ne "Running") -or $iscsiWarning) {
    $question = 'iSCSI initiator is not installed or enabled. It is required for successful execution of this connect script. Do you wish to terminate the script to install it?'
    $decision = $Host.UI.PromptForChoice($title, $question, $choices, 0)
    if ($decision -eq 0) {
        exit
    }
}
 
## Multipath I/O
$multipathWarning = $false
try {
    $checkResult = Get-WindowsFeature -Name 'Multipath-IO' -ErrorAction Stop
} catch {
    $multipathWarning = $true
}
if (($checkResult.InstallState -ne "Installed") -or $multipathWarning) {
    $question = 'Multipath I/O is not installed or enabled. It is recommended for multi-session setup. Do you wish to terminate the script to install it?'
    $decision = $Host.UI.PromptForChoice($title, $question, $choices, 0)
    if ($decision -eq 0) {
        exit
    }
}
 
# Define the list of volumes to process
$volumes = @(
    @{
        volname = "vol1";
        newvolname = "volconnect";
        snapshotname = "snapshot-volconnect";
    }
 
    # Add more volumes as needed
)
 
# Prompt user to confirm volumes are disconnected with clear instructions
Write-Host "IMPORTANT: Before continuing, please ensure that:" -ForegroundColor Yellow
Write-Host " - All volumes are properly disconnected from any attached systems" -ForegroundColor Yellow
Write-Host " - All I/O operations to these volumes are completely paused" -ForegroundColor Yellow
Write-Host " - No applications are currently accessing these volumes" -ForegroundColor Yellow
Write-Host ""
Write-Host "Failure to disconnect volumes properly may result in data corruption or snapshot failures." -ForegroundColor Red
Write-Host ""
 
$response = Read-Host "Please verify: Are all volumes disconnected and I/O operations paused? (Yes/No)"
if ($response.ToLower() -ne "yes") {
    Write-Host "Operation cancelled. Please disconnect all volumes and pause I/O operations before running this script." -ForegroundColor Red
    Write-Host "For guidance on disconnecting volumes, refer to the documentation at: https://learn.microsoft.com/en-us/azure/storage/elastic-san/elastic-san-delete" -ForegroundColor Cyan
    exit
}
 
# Connect to your account (Ignore Warning if using Azure Cloud Shell)
Connect-AzAccount
 
# Loop through each volume and process
foreach ($volume in $volumes) {
    try {
        $volname = $volume.volname
        $newvolname = $volume.newvolname
        $snapshotname = $volume.snapshotname
 
        # Check if the new volume already exists
        $existingVolume = Get-AzElasticSanVolume -SubscriptionId $subscriptionId -ResourceGroupName $rgname -ElasticSanName $esname -VolumeGroupName $vgname -Name $newvolname -ErrorAction SilentlyContinue
        if ($existingVolume) {
            Write-Host "Volume $newvolname already exists. Skipping..." -ForegroundColor Yellow
            continue
        }
 
        # Get the existing volume
        $vol = Get-AzElasticSanVolume -SubscriptionId $subscriptionId -ResourceGroupName $rgname -ElasticSanName $esname -VolumeGroupName $vgname -Name $volname -ErrorAction Stop
 
        # Create a snapshot
        Write-Host "Creating a snapshot of the volume: $volname"
        $snapshot = New-AzElasticSanVolumeSnapshot -SubscriptionId $subscriptionId -ResourceGroupName $rgname -ElasticSanName $esname -VolumeGroupName $vgname -Name $snapshotname -CreationDataSourceId $vol.Id -ErrorAction Stop
 
        # Create a new volume using the snapshot
        Write-Host "Creating a new volume from the snapshot"
        New-AzElasticSanVolume -ElasticSanName $esname -ResourceGroupName $rgname -SubscriptionId $subscriptionId -VolumeGroupName $vgname -Name $newvolname -CreationDataSourceId $snapshot.Id -CreationDataCreateSource VolumeSnapshot -SizeGiB $vol.SizeGiB -ErrorAction Stop
 
        # Get the new volume details to ensure we have the StorageTarget properties
        $newVol = Get-AzElasticSanVolume -ElasticSanName $esname -ResourceGroupName $rgname -SubscriptionId $subscriptionId -VolumeGroupName $vgname -Name $newvolname -ErrorAction Stop
 
        # Connect the new volume
        $TargetIQN = $newVol.StorageTargetIqn
        $TargetHostName = $newVol.StorageTargetPortalHostname
        $TargetPort = $newVol.StorageTargetPortalPort
        $NumSession = 32
        $maxLoginRetriesPerSession = 5
 
        Write-Host "Connecting to volume: $newvolname [$TargetIQN]" -ForegroundColor Cyan
        
        $sessions = Get-IscsiSession
        if ($sessions -ne $null) {
            $sessions = (Get-IscsiSession).TargetNodeAddress.ToLower() | Select -Unique
        }
 
        # Check if the volume is already connected
        if ($sessions -ne $null -and $sessions -contains $TargetIQN.ToLower()) {
            Write-Host "Volume $newvolname is already connected." -ForegroundColor Magenta
        } else {
            iscsicli AddTarget $TargetIQN * $TargetHostName $TargetPort * 0 * * * * * * * * * 0
            $LoginOptions = '0x00000002'
            for ($i = 0; $i -lt $NumSession; $i++) {
                iscsicli PersistentLoginTarget $TargetIQN.ToLower() t $TargetHostName.ToLower() $TargetPort Root\ISCSIPRT\0000_0 -1 * $LoginOptions 1 1 * * * * * * * 0
                $loginAttempts = 0
                do {
                    iscsicli LoginTarget $TargetIQN t $TargetHostName $TargetPort Root\ISCSIPRT\0000_0 -1 * $LoginOptions 1 1 * * * * * * * 0
                    $loginAttempts += 1
                } while (($LASTEXITCODE -ne 0) -and ($loginAttempts -lt $maxLoginRetriesPerSession))
            }
            Write-Host "Successfully connected volume: $newvolname" -ForegroundColor Green
        }
 
        Write-Host "Successfully processed volume: $volname"
    } catch {
        Write-Host "Failed to process volume: $($volume.volname). Error: $($_.Exception.Message)" -ForegroundColor Red
    }
}
