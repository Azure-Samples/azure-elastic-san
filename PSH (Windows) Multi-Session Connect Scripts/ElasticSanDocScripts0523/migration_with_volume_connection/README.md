# Azure Elastic SAN Volume Migration with Auto-Connect Script

This PowerShell script automates the migration of Azure Elastic SAN volumes by creating snapshots, provisioning new volumes from those snapshots, and automatically connecting them with Multipath IO.

## Important Safety Checks

**⚠️ CRITICAL: Before running this script:**

1. Disconnect all volumes from any attached systems
2. Pause all I/O operations to these volumes
3. Ensure no applications are accessing the volumes

For guidance on disconnecting volumes, refer to the [Azure documentation](https://learn.microsoft.com/en-us/azure/storage/elastic-san/elastic-san-delete).

## Overview

The script performs the following operations for each volume:
1. Validates required dependencies (iSCSI Initiator and Multipath I/O)
2. Creates a snapshot of the existing volume
3. Creates a new volume from the snapshot
4. Automatically connects the new volume using 32 iSCSI sessions
5. Preserves the original volume size and data

**Note:** This script creates a copy of the volume and does not delete the existing volume. The original volume remains intact after the migration process completes.

## Prerequisites

### Required Windows Features
- **iSCSI Initiator** - Must be installed and running
- **Multipath I/O** - Recommended for multi-session setup

The script automatically checks for these dependencies and prompts you to install them if missing.

## Configuration

Before running the script, update the following variables at the top of `migration_with_volume_connection.ps1`:

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

## Usage
- Install the Azure PowerShell module before running the script
- Open Powershell as Administrator and run:
  ```powershell
  Install-Module -Name Az -Repository PSGallery -Force
  ```
- Ensure that you have the latest [Az.ElasticSan module](https://www.powershellgallery.com/packages/Az.ElasticSan/1.4.0) installed.
- Update the Variables as mentioned below
- Run the script from a PowerShell window inside the customer Windows VM 


## Connection Details

Each new volume is automatically connected with:
- **32 iSCSI sessions** for optimal performance
- **Up to 5 login retry attempts** per session for reliability

## Error Handling

The script includes error handling for each volume operation:
- If a new volume already exists, it will be skipped
- If a volume is already connected, connection will be skipped
- If an error occurs during snapshot or volume creation, the error is logged and the script continues with the next volume
- Failed operations are reported with detailed error messages

## Example Output

```
Confirm
[Y] Yes to terminate  [N] No to proceed with rest of the steps  [?] Help (default is "Y"): N

IMPORTANT: Before continuing, please ensure that:
 - All volumes are properly disconnected from any attached systems
 - All I/O operations to these volumes are completely paused
 - No applications are currently accessing these volumes

Please verify: Are all volumes disconnected and I/O operations paused? (Yes/No): Yes

Creating a snapshot of the volume: vol1
Creating a new volume from the snapshot
Connecting to volume: volconnect [iqn.2023-01.net.windows.core.blob...]
Successfully connected volume: volconnect
Successfully processed volume: vol1
```
