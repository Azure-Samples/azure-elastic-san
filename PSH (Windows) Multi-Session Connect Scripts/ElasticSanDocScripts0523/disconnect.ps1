Param(
    [Parameter(Mandatory,
    HelpMessage = "Resource group name")]
    [string]
    $ResourceGroupName,
    [Parameter(Mandatory,
    HelpMessage = "Elastic SAN name")]
    [string]
    $ElasticSanName,
    [Parameter(Mandatory,
    HelpMessage = "Volume group name")]
    [string]
    $VolumeGroupName,
    [Parameter(Mandatory,
    HelpMessage = "Volumes to be disconnected")]
    [string[]]
    $VolumeName
)

$title    = 'Confirm'
$choices = @(
    [System.Management.Automation.Host.ChoiceDescription]::new("&Yes", "Yes")
    [System.Management.Automation.Host.ChoiceDescription]::new("&No", "No")
)
$question = 'Running this script will remove access to all the selected volumes, all existing sessions to these volumes will be disconnected. Do you wish to continue?'
$decision = $Host.UI.PromptForChoice($title, $question, $choices, 0)

if ($decision -eq 1) {
    Exit
}

################ Definition of VolumeData #################################
class VolumeData
{
    [ValidateNotNullOrEmpty()][string]$VolumeName
    [ValidateNotNullOrEmpty()][string]$TargetIQN
    [ValidateNotNullOrEmpty()][string]$TargetHostName
    [ValidateNotNullOrEmpty()][string]$TargetPort

    VolumeData($VolumeName, $TargetIQN, $TargetHostName, $TargetPort) {
       $this.VolumeName = $VolumeName
       $this.TargetIQN = $TargetIQN
       $this.TargetHostName = $TargetHostName
       $this.TargetPort = $TargetPort
    }
}

############### Gather information of input volumes ########################

# Get volume group resource to fail fast
$vg = Get-AzElasticSanVolumeGroup -ResourceGroupName $ResourceGroupName -ElasticSanName $ElasticSanName -Name $VolumeGroupName -ErrorAction Stop

$volumesToDisconnect= New-Object System.Collections.Generic.List[VolumeData]
$invalidVolumes = New-Object System.Collections.Generic.List[string]
foreach($volume in $volumeName) {
    try {
        $vol = Get-AzElasticSanVolume -ResourceGroupName $ResourceGroupName -ElasticSanName $ElasticSanName -VolumeGroupName $VolumeGroupName -Name $volume -ErrorAction Stop
        $targetIqn = $vol.StorageTargetIqn
        $targetHostname = $vol.StorageTargetPortalHostname
        $targetPort = $vol.StorageTargetPortalPort
        $volumesToDisconnect.Add([VolumeData]::new($volume,$targetIqn,$targetHostname, $targetPort))
        Write-Host Gathered info of $volume successfully -ForegroundColor Cyan
    } catch {
        Write-Error $_
        $invalidVolumes.Add($volume)
    }    
}
if ($invalidVolumes.Count -gt 0) {
    # Terminate the script if any of the input volumes are invalid
    Write-Error "Invalid volumes: $($invalidVolumes -Join ",")" -ErrorAction Stop
}

function Test-DisconnectTargetName([string]$TargetName, [string]$TargetIqn) {
    $TargetName.Equals($TargetIqn, [StringComparison]::OrdinalIgnoreCase) -or
        $TargetName.StartsWith("$TargetIqn`:az-", [StringComparison]::OrdinalIgnoreCase)
}

function Invoke-DisconnectIscsiCli([string[]]$Arguments) {
    $output = @(& iscsicli @Arguments 2>&1)
    $status = $LASTEXITCODE
    # iscsicli can return exit zero for an API failure. Unknown/localized
    # status must not be mistaken for successful inventory or cleanup.
    if ($status -ne 0 -or ($output -join "`n").TrimEnd() -cnotmatch '(^|\r?\n)The operation completed successfully\.\z') {
        throw "iscsicli $($Arguments[0]) failed or returned unrecognized status (exit $status): $($output -join ' '). Cleanup may be incomplete; inspect the selected targets before retrying."
    }
    $output
}

function Get-DisconnectPersistentTargets {
    $output = Invoke-DisconnectIscsiCli -Arguments @('ListPersistentTargets')
    $targets = New-Object 'System.Collections.Generic.List[object]'
    $target = $null
    $expectedCount = $null
    foreach ($line in $output) {
        if ($line -match '^\s*Total of\s+(\d+)\s+pers?istent targets\s*$') {
            $expectedCount = [int]$Matches[1]
        }
        $parts = $line -split ':', 2
        if ($parts.Count -ne 2) { continue }
        $label = $parts[0].Trim() -replace '\s+', ' '
        $value = $parts[1].Trim()
        if ($label -eq 'Target Name') {
            if ([string]::IsNullOrWhiteSpace($value)) { throw 'Missing persistent target name.' }
            $target = [pscustomobject]@{ TargetName = $value; Address = $null; Port = $null }
            $targets.Add($target)
        } elseif ($label -eq 'Address and Socket') {
            # Split the label only at its first colon; IPv6 belongs to the
            # address, and the final whitespace-separated token is the port.
            $port = 0
            if ($null -eq $target -or $null -ne $target.Address -or $value -notmatch '^(.+?)\s+([0-9]+)$' -or
                ![int]::TryParse($Matches[2], [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
                throw 'Malformed or duplicate persistent target Address and Socket.'
            }
            $target.Address = $Matches[1].Trim()
            $target.Port = $port
        }
    }
    if ($null -eq $expectedCount -or $expectedCount -ne $targets.Count) {
        throw 'Unrecognized or incomplete persistent target listing; expected English ListPersistentTargets output.'
    }
    foreach ($entry in $targets) {
        if ([string]::IsNullOrWhiteSpace($entry.Address)) { throw "Missing persistent portal for '$($entry.TargetName)'." }
    }
    $targets
}

############## Disconnect volumes ###############################
$persistentTargets = @(Get-DisconnectPersistentTargets)
$processedIqns = @{}
foreach($volume in $volumesToDisconnect) {
    if ($processedIqns.ContainsKey($volume.TargetIQN)) { continue }
    $processedIqns[$volume.TargetIQN] = $true
    $sessions = @(Get-IscsiSession -ErrorAction Stop | Where-Object { Test-DisconnectTargetName $_.TargetNodeAddress $volume.TargetIQN })
    $persistentLogins = @($persistentTargets | Where-Object { Test-DisconnectTargetName $_.TargetName $volume.TargetIQN })
    if ($sessions.Count -eq 0 -and $persistentLogins.Count -eq 0) {
        Write-Host $volume.VolumeName [$($volume.TargetIQN)]: Skipped as this volume is not connected -ForegroundColor Magenta
        continue  
    }

    Write-Host $volume.VolumeName [$($volume.TargetIQN)]: Disconnecting volume -ForegroundColor Cyan
    $targetNames = @{}
    # remove connected sessions 
    foreach ($session in $sessions) {
        $null = Invoke-DisconnectIscsiCli -Arguments @('LogoutTarget', $session.SessionIdentifier)
        $targetNames[$session.TargetNodeAddress] = $true
    }
    # Saved portals can be FQDNs or VIPs and can differ from a live redirect.
    foreach ($login in $persistentLogins) {
        $null = Invoke-DisconnectIscsiCli -Arguments @('RemovePersistentTarget', 'ROOT\ISCSIPRT\0000_0', $login.TargetName, '-1', $login.Address, "$($login.Port)")
        $targetNames[$login.TargetName] = $true
    }
    foreach ($targetName in $targetNames.Keys) {
        $null = Invoke-DisconnectIscsiCli -Arguments @('RemoveTarget', $targetName)
    }
}