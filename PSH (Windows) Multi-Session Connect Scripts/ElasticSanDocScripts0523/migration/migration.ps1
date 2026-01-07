$vgname = "<name of volume group>"
$rgname = "<your resource group name>"
$esname = "<name of esan>" 
$subscriptionId = "<subscription id>"

# Define the list of volumes to process
$volumes = @(
    @{
        volname = "<name of existing volume>";
        newvolname = "<name of new volume>";
        snapshotname = "<name of snapshot>";
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

        Write-Host "Successfully processed volume: $volname"
    } catch {
        Write-Host "Failed to process volume: $($volume.volname). Error: $($_.Exception.Message)" -ForegroundColor Red
    }
}