param(
    [Parameter(Mandatory)]
    [string]$IQN
)

$targetIQNs = $IQN.Split(",") | ForEach-Object { $_.Trim() }
foreach ($iqn in $targetIQNs) {
    if ($iqn -notlike "*net.windows.core.blob.ElasticSan*") {
        Write-Warning ("IQN {0} is not IQN v1. Please provide a valid IQN v1 string. Exiting." -f $iqn)
        return
    }
}

Write-Host "This script is intended only for use with volumes specified by the Azure Elastic SAN team in an email titled:
            `"Action Required: Breaking Change Will Affect Access to Your Elastic SAN Volumes.`" that have been backfilled with a new IQN v2.
            ` This script will disconnect all active sessions for the IQN v1 volumes that you have provided. 
            ` Once this script is complete, please restart your VM and run 'Get-IscsiSession' to verify that no sessions are active.`n
            ` Please confirm that you want to proceed with deleting these IQNs:`n" -ForegroundColor Yellow

$sessions = @{}

foreach ($iqn in $targetIQNs) {
    $sessions[$iqn] = Get-IscsiSession |
        Where-Object {
            $_.IsConnected -eq $true -and
            $_.TargetNodeAddress -like $iqn
        }
}

foreach ($iqn in $targetIQNs) {
    if ($sessions[$iqn].Count -eq 0) {
        Write-Host ("{0}, No active sessions found on this VM.`n" -f $iqn)
    }
    else {
        Write-Host ("{0}, Session Count: {1}`n" -f $iqn, $sessions[$iqn].Count)
    }    
}

while ($true) {
    $choice = Read-Host "Before running this script, please navigate to your volume's connect script on ms.portal.azure.com and 
                        `confirm that the IQN listed in the call to volume_data.append() contains the substring 'net.azure.storage.blob' and not 
                        `'net.windows.core.blob'. Have you verified that all of the volumes you will be disconnecting using this script have an 
                        `IQN v2 in your portal connect script?\n (Y/N): "
    if ($choice -eq "Y") {
        break
    }
    elseif ($choice -eq "N") {
        Write-Host "Exiting without disconnecting sessions."
        return
    }
    else {
        Write-Warning "Invalid choice. Please type 'Y' or 'N'."
    }
}

while ($true) {
    $choice = Read-Host "Would you like to proceed in disconnecting all sessions for these volumes? (Y/N): "
    if ($choice -eq "Y") {
        break
    }
    elseif ($choice -eq "N") {
        Write-Host "Exiting without disconnecting sessions."
        return
    }
    else {
        Write-Warning "Invalid choice. Please type 'Y' or 'N'."
    }
}

foreach ($iqn in $targetIQNs) {
    foreach ($s in $sessions[$iqn]) {
        Write-Host ("Unregistering: {0}  |  {1}" -f $s.SessionIdentifier, $s.TargetNodeAddress)
        try {
                Unregister-IscsiSession -SessionIdentifier $s.SessionIdentifier
                $unregisteredIQNs += $s.TargetNodeAddress
            }
        
        catch {
            Write-Warning ("Failed to unregister {0}: {1}" -f $s.SessionIdentifier, $_.Exception.Message)
        }
    }
}

Write-Host "`nPlease reboot your VM and run 'Get-IscsiSession' to verify no sessions are active." -ForegroundColor Red


