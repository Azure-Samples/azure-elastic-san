#Requires -Version 5.0 -Modules Az.Accounts, Az.ElasticSan
#Requires -RunAsAdministrator

<#
    .SYNOPSIS
    Connects iSCSI initiator to the Elastic SAN volume provided.
    .DESCRIPTION
    Connects iSCSI initiator to the Elastic SAN volume provided.
    .PARAMETER SubscriptionID
    Azure subscription ID in the format, 00000000-0000-0000-0000-000000000000
    .PARAMETER ResourceGroupName
    An Azure resource group name
    .PARAMETER ElasticSanName
    .PARAMETER VolumeGroupName
    .PARAMETER VolumeName
    .PARAMETER ElasticSanName
    .PARAMETER VolumeGroupName
    .PARAMETER NumSession
    .PARAMETER -UseIdentity
    .OUTPUTS
    Text
    .EXAMPLE
    connect.ps1 -Subscription 00000000-0000-0000-0000-000000000000 -ResourceGroupName <RG Name> -ElasticSanName <Elastic SAN Name> -VolumeGroupName <Volume Group Name> -VolumeName <Volume Name> -NumSession <1-32> -UseIdentity
    .LINK
#>

Param(
    [Parameter(Mandatory, 
    HelpMessage = "Subscription Id")]
    [string]
    $SubscriptionId,
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
    $NumSession,
    [switch]$UseIdentity
)

############### VERIFY AZURE LOGON AND SUBSCRIPTION ###################
if ($UseIdentity) {
    Add-AzAccount -Subscription $SubscriptionID -Identity -ErrorAction Stop     #A System Managed Identity needs to be created for the VM and given the Reader role for the Elastic SAN
}
$AzContext = Set-AzContext -SubscriptionId $SubscriptionID -ErrorAction Stop
Write-Verbose "The PowerShell session needs to be logged in to Azure with an account with Reader access to the Elastic SAN or a Managed Identity can be used with the Reader role to the Elastic SAN."
Write-Verbose ($AzContext)

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
try {
    Get-AzElasticSanVolumeGroup -ResourceGroupName $ResourceGroupName -ElasticSanName $ElasticSanName -Name $VolumeGroupName | Out-Null
}
catch {
    Write-Error "Cannot connect to the Elastic SAN. Make sure you are logged in to Azure or are using an identity." -ErrorAction Stop
}

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
        iscsicli LoginTarget $volume.TargetIQN t $volume.TargetHostName $volume.TargetPort Root\ISCSIPRT\0000_0 -1 * $LoginOptions 1 1 * * * * * * * 0
    }
}
