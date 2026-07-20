# Azure Elastic SAN Volume Migration Script

This PowerShell script automates the migration of Elastic SAN volumes by creating snapshots and provisioning new volumes from those snapshots.

## Overview

The `migration.ps1` script performs the following operations for each volume:
1. Creates a snapshot of the existing volume
2. Creates a new volume from that snapshot
3. Preserves the original volume size and data

**Note:** This script creates a copy of the volume and does not delete the existing volume. The original volume remains intact after the migration process completes.

## Usage

This script can be executed in two ways:

### Method 1: Azure Cloud Shell
- No additional setup required
- Simply open Cloud Shell from the Azure Portal and paste the script

### Method 2: Customer VM or Local Machine
- Requires Azure PowerShell module installation

## Configuration

Before running the script, update the following variables at the top of `migration.ps1`:

```powershell
$vgname = "<name of volume group>"          # Your volume group name
$rgname = "<your resource group name>"      # Your resource group name
$esname = "<name of esan>"                  # Your Elastic SAN name
$subscriptionId = "<subscription id>"       # Your Azure subscription ID
```

### Volume List Configuration

Define the volumes to migrate in the `$volumes` array:

```powershell
$volumes = @(
    @{
        volname = "<name of existing volume>";   # Source volume name
        newvolname = "<name of new volume>";     # New volume name
        snapshotname = "<name of snapshot>";     # Snapshot name
    }
    # Add more volumes as needed
)
```

## Important Safety Checks

**⚠️ CRITICAL: Before running this script:**

1. Disconnect all volumes from any attached systems
2. Pause all I/O operations to these volumes
3. Ensure no applications are accessing the volumes

For guidance on disconnecting volumes, refer to the [Azure documentation](https://learn.microsoft.com/en-us/azure/storage/elastic-san/elastic-san-delete).

## Error Handling

The script includes error handling for each volume operation:
- If a new volume already exists, it will be skipped
- If an error occurs during snapshot or volume creation, the error is logged and the script continues with the next volume
- Failed operations are reported with detailed error messages

## Example Output

```
IMPORTANT: Before continuing, please ensure that:
 - All volumes are properly disconnected from any attached systems
 - All I/O operations to these volumes are completely paused
 - No applications are currently accessing these volumes

Please verify: Are all volumes disconnected and I/O operations paused? (Yes/No): Yes

Creating a snapshot of the volume: vol1
Creating a new volume from the snapshot
Successfully processed volume: vol1
```
