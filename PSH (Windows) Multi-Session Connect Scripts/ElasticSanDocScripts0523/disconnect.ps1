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

############## Disconnect volumes ###############################
$persistentTargets = iscsicli ListPersistentTargets
foreach($volume in $volumesToDisconnect) {
    $sessions = Get-IscsiSession | ?{$_.TargetNodeAddress -eq $volume.TargetIQN}
    if ($sessions -eq $null) {
        Write-Host $volume.VolumeName [$($volume.TargetIQN)]: Skipped as this volume is not connected -ForegroundColor Magenta
        continue  
    }

    Write-Host $volume.VolumeName [$($volume.TargetIQN)]: Disconncting volume -ForegroundColor Cyan
    # remove connected sessions 
    foreach ($session in $sessions) {
        iscsicli LogoutTarget $session.SessionIdentifier
    }
    # remove persistent targets 
    # An example of the string of target name --  Target Name           : iqn.2023-02.net.windows.core.blob.elasticsan.es-ldjjtvaggga0:yifantestvol2
    $persistentTargetCount = (($persistentTargets | ?{$_.ToLower().Contains($volume.TargetIQN.ToLower())}) | ?{($_ -split ":",2)[1].Trim().ToLower() -eq $volume.TargetIQN.ToLower()}).Count
    for ($i = 0; $i -lt $persistentTargetCount; $i++) {
        iscsicli RemovePersistentTarget ROOT\ISCSIPRT\0000_0 $volume.TargetIQN -1 $volume.TargetHostName $volume.TargetPort
    }
    # remove target
    iscsicli RemoveTarget $volume.TargetIQN.ToLower()

}