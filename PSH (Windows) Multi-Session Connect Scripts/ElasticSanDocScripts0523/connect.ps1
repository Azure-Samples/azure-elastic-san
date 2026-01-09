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
    HelpMessage = "Volumes to be connected")]
    [string[]]
    $VolumeName,
    [Parameter(HelpMessage = "Number of sessions to be connected for each volume. Default value is 32. Input value should be in range of 1-32.")]
    [ValidateRange(1,32)]
    [int]
    $NumSession
)

#################### DEFINITION OF VOLUME DATA ########################
class VolumeData
{
    [ValidateNotNullOrEmpty()][string]$VolumeName
    [ValidateNotNullOrEmpty()][string]$TargetIQN
    [ValidateNotNullOrEmpty()][string]$TargetHostName
    [ValidateNotNullOrEmpty()][string]$TargetPort
    [AllowNull()][Nullable[System.Int32]]$NumSession

    VolumeData($VolumeName, $TargetIQN, $TargetHostName, $TargetPort, $NumSession) {
       $this.VolumeName = $VolumeName
       $this.TargetIQN = $TargetIQN
       $this.TargetHostName = $TargetHostName
       $this.TargetPort = $TargetPort
       $this.NumSession = if ($NumSession -eq 0 -or $NumSession -eq $null) {32} Else {$NumSession}
    }
}

##################### CHECK DEPENDENCY #################################
$title    = 'Confirm'
$choices  = '&Yes to terminate','&No to proceed with rest of the steps'
$choices = @(
    [System.Management.Automation.Host.ChoiceDescription]::new("&Yes to terminate", "Yes to terminate")
    [System.Management.Automation.Host.ChoiceDescription]::new("&No to proceed with rest of the steps", "No to proceed with rest of the steps")
)

## iSCSI initiator check 
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

## Multipath I/O check
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


##################### GATHER INFORMATION OF INPUT VOLUMES ####################
# Get volume group resource to fail fast
$vg = Get-AzElasticSanVolumeGroup -ResourceGroupName $ResourceGroupName -ElasticSanName $ElasticSanName -Name $VolumeGroupName -ErrorAction Stop

$volumesToConnect= New-Object System.Collections.Generic.List[VolumeData]
$invalidVolumes = New-Object System.Collections.Generic.List[string]
# Get each volume in the input volume list and extract the required info for connections 
foreach($volume in $volumeName) {
    try {
        $vol = Get-AzElasticSanVolume -ResourceGroupName $ResourceGroupName -ElasticSanName $ElasticSanName -VolumeGroupName $VolumeGroupName -Name $volume -ErrorAction Stop
        $targetIqn = $vol.StorageTargetIqn
        $targetHostname = $vol.StorageTargetPortalHostname
        $targetPort = $vol.StorageTargetPortalPort
        $volumesToConnect.Add([VolumeData]::new($volume,$targetIqn,$targetHostname, $targetPort, $numSession))
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

############################### CONNECT VOLUMES ############################
$sessions = Get-IscsiSession
if ($sessions -ne $null) {
    $sessions = (Get-IscsiSession).TargetNodeAddress.ToLower() | Select -Unique
}

foreach($volume in $volumesToConnect) {
    # Check if the volume is already connected 
    if ($sessions -ne $null -and $sessions.Contains($volume.TargetIQN.ToLower())) {
        Write-Host $volume.VolumeName [$($volume.TargetIQN)]: Skipped as this volume is already connected -ForegroundColor Magenta
        continue
    }
    # connect volume 
    Write-Host $volume.VolumeName [$($volume.TargetIQN)]: Connecting to this volume -ForegroundColor Cyan
    iscsicli AddTarget $volume.TargetIQN * $volume.TargetHostName $volume.TargetPort * 0 * * * * * * * * * 0
    $LoginOptions = '0x00000002'
    for ($i = 0; $i -lt $volume.NumSession; $i++) {
        iscsicli PersistentLoginTarget $volume.TargetIQN.ToLower() t $volume.TargetHostname.ToLower() $volume.TargetPort Root\ISCSIPRT\0000_0 -1 * $LoginOptions 1 1 * * * * * * * 0
    }
}

