<#
.SYNOPSIS
Connects Azure Elastic SAN volumes to this Windows machine with multiple persistent iSCSI sessions per volume.

.DESCRIPTION
Run from an elevated PowerShell session with the Az.ElasticSan module signed in (Connect-AzAccount).

The script runs these steps in order in every mode (default, -EnableZonalAffinity, -EnableVipDistribution).
It stops (throws) before adding any session when a step fails, so a partial or misleading connection is
never reported as success.

 1. Checks that the session is elevated and detects Windows 10/11 (client) or Windows Server.
 2. Looks up each volume's target IQN, portal and port with the Az.ElasticSan module (plus the zone
    mapping in zonal mode).
 3. Makes sure the iSCSI initiator service (MSiSCSI) starts automatically and is running, enables
    Multipath I/O (an optional feature on client, a server feature on Windows Server) and makes MSDSM
    claim iSCSI disks.
 4. Unless -SkipRecommendedSettings is set, applies the client settings from
    https://learn.microsoft.com/azure/storage/elastic-san/elastic-san-best-practices: MSDSM round robin
    load balance policy, a 30-second MPIO disk timeout, and the iSCSI initiator registry values. Only
    values that differ are changed.
 5. If enabling Multipath I/O needs a restart, stops before adding sessions: reboot, then re-run the
    script. Sessions added before MPIO is active show up as duplicate disks.
 6. Reads the live sessions and persistent logins of every volume before changing anything. A volume
    that already has either is skipped; this script never adds to, or removes, existing sessions. A volume
    selected more than once is connected once. Also stops if the new sessions would exceed the Windows
    limit of 256 persistent iSCSI logins.
 7. Saves the persistent logins of each remaining volume, prints a validation report ([PASS], [WARN] and
    [FAIL] lines and a summary), and says when a reboot is needed: for changed settings, or for saved
    persistent logins whose sessions aren't live yet.

The script throws, which gives a non-zero exit code with powershell.exe -File, when it stops early or
when any validation check fails.

.PARAMETER ResourceGroupName
Resource group of the Elastic SAN.

.PARAMETER ElasticSanName
Elastic SAN name.

.PARAMETER VolumeGroupName
Volume group name.

.PARAMETER VolumeName
Volumes to connect.

.PARAMETER SkipRecommendedSettings
Opt out of the recommended client settings: the MSDSM load balance policy, the MPIO disk timeout and the
iSCSI initiator registry values are neither changed nor validated. The iSCSI service, Multipath I/O and
MSDSM iSCSI claiming are still configured, because multiple sessions don't work correctly without them.

.PARAMETER NumSession
Sessions per volume, 1-32. Default 32. Windows allows at most 256 iSCSI sessions in total, so use fewer
sessions per volume when connecting more than eight volumes. Use 1 on Windows editions without Multipath I/O.

.PARAMETER EnableZonalAffinity
Connect to the zonal target IQN (:az-<physicalZone> suffix). See README.md in this folder.

.PARAMETER EnableVipDistribution
Spread the saved persistent logins across the portal's resolved IPv4 VIPs. See README.md in this folder.

.EXAMPLE
.\connect.ps1 -ResourceGroupName myRG -ElasticSanName mySan -VolumeGroupName myVG -VolumeName vol1,vol2

.EXAMPLE
.\connect.ps1 -ResourceGroupName myRG -ElasticSanName mySan -VolumeGroupName myVG -VolumeName vol1 -NumSession 8 -SkipRecommendedSettings
#>
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
    [Parameter(HelpMessage = "Skip applying and validating the recommended client settings (MSDSM load balance policy, MPIO disk timeout, iSCSI initiator registry values).")]
    [switch]
    $SkipRecommendedSettings,
    [Parameter(HelpMessage = "Number of sessions to be connected for each volume. Default value is 32. Input value should be in range of 1-32.")]
    [ValidateRange(1,32)]
    [int]
    $NumSession,
    [Parameter(HelpMessage = "Opt in to subscription-scoped zonal IQN mapping. Requires backend zonal IQN support.")]
    [switch]
    $EnableZonalAffinity,
    [Parameter(HelpMessage = "Distribute original persistent login portals across resolved IPv4 VIPs, independently of zonal IQN mapping.")]
    [switch]
    $EnableVipDistribution
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

function ConvertTo-ZonalSubscriptionId($Value) {
    if ($Value -isnot [string] -or $Value.Trim() -cnotmatch '\A[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\z') {
        throw 'Subscription ID must be a canonical GUID.'
    }
    $Value.Trim().ToLowerInvariant()
}

function New-ZonalMetadataClient {
    Add-Type -AssemblyName System.Net.Http -ErrorAction Stop
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.UseProxy = $false
    $handler.AllowAutoRedirect = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(5)
    $client.DefaultRequestHeaders.Add('Metadata', 'true')
    $client
}

function Get-ZonalComputeMetadata {
    $client = New-ZonalMetadataClient
    try {
        # GetStringAsync buffers the complete response under HttpClient.Timeout,
        # unlike a separate per-read timeout that a slow body can keep extending.
        $client.GetStringAsync('http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01').GetAwaiter().GetResult() |
            ConvertFrom-Json -ErrorAction Stop
    } finally { $client.Dispose() }
}

function Initialize-ZonalProviderCancellation {
    if ('ElasticSan.ZonalProviderCancellation' -as [type]) { return }
    # A .NET callback can clean up on the provider thread without a PowerShell
    # event loop. In particular, synchronous Stop/Dispose must not extend the
    # caller deadline when Invoke-AzRestMethod ignores cancellation.
    Add-Type -ErrorAction Stop -TypeDefinition @'
using System;
using System.Management.Automation;
namespace ElasticSan {
    public static class ZonalProviderCancellation {
        public static void StopAndDispose(PowerShell pipeline, IAsyncResult invocation) {
            pipeline.BeginStop(stopped => {
                try {
                    pipeline.EndStop(stopped);
                } finally {
                    pipeline.Dispose();
                    invocation.AsyncWaitHandle.Close();
                }
            }, null);
        }
    }
}
'@
}

function Invoke-ZonalProviderRequest([string]$Command, [hashtable]$Parameters, [ValidateRange(1,300)][int]$TimeoutSeconds = 30) {
    # Get-AzElasticSan* has no -AsJob or timeout parameter. An in-process
    # pipeline keeps DefaultProfile as the real object, not a serialized login.
    Initialize-ZonalProviderCancellation
    $pipeline = [System.Management.Automation.PowerShell]::Create()
    $pending = $null
    $deferredCleanup = $false
    try {
        $null = $pipeline.AddCommand($Command).AddParameters($Parameters).AddParameter('ErrorAction', 'Stop')
        $pending = $pipeline.BeginInvoke()
        if (!$pending.AsyncWaitHandle.WaitOne([TimeSpan]::FromSeconds($TimeoutSeconds))) {
            [ElasticSan.ZonalProviderCancellation]::StopAndDispose($pipeline, $pending)
            $deferredCleanup = $true
            throw "$Command exceeded its $TimeoutSeconds-second provider deadline; no connection changes were started. Cancellation was requested; an outstanding read may finish in the background and its result will be discarded."
        }
        $pipeline.EndInvoke($pending)
    } finally {
        if (!$deferredCleanup) {
            if ($null -ne $pending) { $pending.AsyncWaitHandle.Close() }
            $pipeline.Dispose()
        }
    }
}

function Get-ZonalPhysicalZone($Locations, [string]$Location, [string]$LogicalZone) {
    if ($Locations -isnot [array]) { throw 'ARM locations must be an array.' }
    $regions = @{}
    foreach ($candidate in $Locations) {
        if ($candidate -isnot [pscustomobject] -or $candidate.name -isnot [string] -or
            [string]::IsNullOrWhiteSpace($candidate.name)) { throw 'Malformed ARM location.' }
        $key = $candidate.name.Trim()
        if ($regions.ContainsKey($key)) { throw "Duplicate ARM location '$key'." }
        $regions[$key] = $candidate
    }
    $region = $regions[$Location.Trim()]
    if ($null -eq $region) { throw "No ARM location matches '$Location'." }
    if ($region.availabilityZoneMappings -isnot [array] -or $region.availabilityZoneMappings.Count -eq 0) {
        throw 'Missing or malformed availabilityZoneMappings.'
    }
    $zones = @{}
    $physicalZones = @{}
    foreach ($mapping in $region.availabilityZoneMappings) {
        if ($mapping -isnot [pscustomobject] -or $mapping.logicalZone -isnot [string] -or
            $mapping.logicalZone.Trim() -cnotmatch '\A[1-9][0-9]*\z' -or
            $mapping.physicalZone -isnot [string]) { throw 'Malformed availabilityZoneMappings entry.' }
        $logical = $mapping.logicalZone.Trim()
        $physical = $mapping.physicalZone.Trim().ToLowerInvariant()
        if ($physical -cnotmatch '\A[a-z0-9.-]+\z') { throw 'Physical zone is unsafe for an IQN suffix.' }
        if ($zones.ContainsKey($logical) -or $physicalZones.ContainsKey($physical)) {
            throw "Duplicate logical or physical zone in availabilityZoneMappings ('$logical', '$physical')."
        }
        $zones[$logical] = $physical
        $physicalZones[$physical] = $true
    }
    if (!$zones.ContainsKey($LogicalZone.Trim())) { throw "No mapping for logical zone '$LogicalZone'." }
    $zones[$LogicalZone.Trim()]
}

function Resolve-ZonalPhysicalZone($Context, [string]$SubscriptionId, [string]$ResourceGroup, [string]$SanName) {
    $compute = Get-ZonalComputeMetadata
    if ($compute.zone -isnot [string] -or $compute.zone.Trim() -cnotmatch '\A[1-9][0-9]*\z') {
        throw 'VM is not availability-zone pinned; IMDS compute.zone is empty, missing or malformed.'
    }
    if ((ConvertTo-ZonalSubscriptionId $compute.subscriptionId) -ne $SubscriptionId) {
        throw 'Elastic SAN subscription does not match VM subscription.'
    }
    if ($compute.location -isnot [string] -or [string]::IsNullOrWhiteSpace($compute.location)) {
        throw 'IMDS compute.location is empty or missing.'
    }
    $san = Invoke-ZonalProviderRequest 'Get-AzElasticSan' @{
        ResourceGroupName = $ResourceGroup; Name = $SanName; SubscriptionId = $SubscriptionId; DefaultProfile = $Context
    }
    if ($san.Location -isnot [string] -or [string]::IsNullOrWhiteSpace($san.Location) -or
        $san.Location.Trim() -ine $compute.location.Trim()) { throw 'The VM and Elastic SAN must be in the same region.' }
    $response = Invoke-ZonalProviderRequest 'Invoke-AzRestMethod' @{
        Method = 'GET'; Path = "/subscriptions/$SubscriptionId/locations?api-version=2022-12-01"; DefaultProfile = $Context
    }
    if ($response.StatusCode -ne 200) { throw "ARM locations failed: HTTP $($response.StatusCode)." }
    $payload = $response.Content | ConvertFrom-Json -ErrorAction Stop
    Get-ZonalPhysicalZone $payload.value $san.Location $compute.zone
}

function Get-ZonalTargetIqn($TargetIqn, [string]$PhysicalZone) {
    $zone = $PhysicalZone.Trim().ToLowerInvariant()
    if ($zone -cnotmatch '\A[a-z0-9.-]+\z') { throw 'Physical zone is unsafe for an IQN suffix.' }
    if ($TargetIqn -isnot [string] -or $TargetIqn -match ':az-') { throw 'Expected an undecorated service target IQN.' }
    # Only the physical zone is normalized. The service identity is opaque:
    # upper/mixed case needs a service contract decision, not a client rewrite.
    $iqn = "$TargetIqn`:az-$zone"
    if ($iqn -cnotmatch '\Aiqn\.[a-z0-9.:-]+\z') {
        throw 'The complete decorated IQN must use safe lowercase IQN characters. The service identity was not rewritten; confirm the front-end identity contract.'
    }
    if ([System.Text.Encoding]::UTF8.GetByteCount($iqn) -gt 223) { throw 'Decorated target IQN exceeds 223 UTF-8 bytes.' }
    $iqn
}

function Get-VipHostAddresses([string]$HostName) {
    [System.Net.Dns]::GetHostAddresses($HostName)
}

function Resolve-VipPortals([string]$HostName) {
    try {
        $addresses = @(Get-VipHostAddresses $HostName |
            Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork } |
            Sort-Object { [BitConverter]::ToString($_.GetAddressBytes()) } -Unique |
            ForEach-Object { $_.ToString() })
    } catch [System.Net.Sockets.SocketException] {
        throw "DNS lookup failed for '$HostName': $($_.Exception.Message)"
    }
    if ($addresses.Count -eq 0) { throw "DNS lookup for '$HostName' returned no IPv4 addresses." }
    # A single endpoint keeps DNS indirection for LRS, private endpoints and migrations.
    if ($addresses.Count -eq 1) { return $HostName }
    $addresses
}

function Invoke-ZonalIscsiCli([string[]]$Arguments, [switch]$PassThru) {
    $output = @(& iscsicli @Arguments 2>&1)
    $status = $LASTEXITCODE
    # iscsicli can report an API failure with exit zero. Do not assume success
    # from localized/unknown output or an earlier success line.
    if ($status -ne 0 -or ($output -join "`n").TrimEnd() -cnotmatch '(^|\r?\n)The operation completed successfully\.\z') {
        throw "iscsicli $($Arguments[0]) failed or returned unrecognized status (exit $status): $($output -join ' '). Sessions or persistent entries may remain. Inspect the selected target's live and persistent state before an operator-approved retry; no automatic rollback or disconnect was attempted."
    }
    if ($PassThru) { $output }
}

function Get-ZonalPersistentTargets {
    $count = $null
    $targets = @(foreach ($line in (Invoke-ZonalIscsiCli -Arguments @('ListPersistentTargets') -PassThru)) {
        if ($line -match '\A\s*Total\s+of\s+(\d+)\s+pers?istent\s+targets\s*\z') {
            if ($null -ne $count) { throw 'Persistent target inventory contains multiple totals.' }
            $count = [int]$Matches[1]
        }
        $parts = ([string]$line) -split ':', 2
        if ($parts.Count -eq 2 -and $parts[0].Trim() -match '\ATarget\s+Name\z') {
            if ([string]::IsNullOrWhiteSpace($parts[1])) { throw 'Persistent target inventory contains an empty Target Name.' }
            $parts[1].Trim()
        }
    })
    if ($null -eq $count -or $count -ne $targets.Count) {
        throw 'Persistent target inventory is incomplete or unrecognized; cannot safely determine whether the volume is already configured.'
    }
    $targets
}

function Connect-ElasticSanVolumes([string]$ResourceGroup, [string]$SanName, [string]$GroupName, [string[]]$Names,
    [ValidateRange(1,32)][int]$SessionCount = 32, [switch]$ZonalAffinity, [switch]$VipDistribution,
    [string]$Edition = 'Server', [switch]$SkipRecommendedSettings) {
    $context = Get-AzContext -ErrorAction Stop
    $subscription = ConvertTo-ZonalSubscriptionId $context.Subscription.Id
    if ($ZonalAffinity) { $physicalZone = Resolve-ZonalPhysicalZone $context $subscription $ResourceGroup $SanName }
    $null = Invoke-ZonalProviderRequest 'Get-AzElasticSanVolumeGroup' @{
        ResourceGroupName = $ResourceGroup; ElasticSanName = $SanName; Name = $GroupName
        SubscriptionId = $subscription; DefaultProfile = $context
    }
    $seenNames = @{}
    $seenIqns = @{}
    $plans = @(
        foreach ($name in $Names) {
            if ([string]::IsNullOrWhiteSpace($name) -or $seenNames.ContainsKey($name)) { throw "Empty or duplicate selected volume '$name'." }
            $seenNames[$name] = $true
            $volume = Invoke-ZonalProviderRequest 'Get-AzElasticSanVolume' @{
                ResourceGroupName = $ResourceGroup; ElasticSanName = $SanName; VolumeGroupName = $GroupName; Name = $name
                SubscriptionId = $subscription; DefaultProfile = $context
            }
            $iqn = $volume.StorageTargetIqn
            if ([string]::IsNullOrWhiteSpace($iqn)) { throw 'Missing target IQN.' }
            if ($ZonalAffinity) { $iqn = Get-ZonalTargetIqn $iqn $physicalZone }
            if ($seenIqns.ContainsKey($volume.StorageTargetIqn)) { throw 'Selected volumes share the same raw IQN.' }
            $seenIqns[$volume.StorageTargetIqn] = $true
            $hostname = $volume.StorageTargetPortalHostname
            if ($hostname -isnot [string] -or $hostname -match '\s' -or !$hostname.Contains('.') -or
                [Uri]::CheckHostName($hostname) -ne [UriHostNameType]::Dns) { throw 'Expected a target portal FQDN.' }
            $port = 0
            if ([string]$volume.StorageTargetPortalPort -cnotmatch '\A[0-9]+\z' -or
                ![int]::TryParse([string]$volume.StorageTargetPortalPort, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
                throw 'Invalid target port.'
            }
            [pscustomobject]@{
                Name = $name; RawIqn = $volume.StorageTargetIqn; Iqn = $iqn
                Port = $port; HostName = $hostname; Portals = @($hostname); Skip = $false
            }
        }
    )
    if ($plans.Count -eq 0) { throw 'No volumes selected.' }

    # Mapping/IQN/input preflight for the whole batch is complete. The shared hardening steps
    # (prerequisites, recommended settings, reboot gate) run before any existing-state read or change.
    $rebootChanges = @(Initialize-EsanHost -Edition $Edition -SessionCount $SessionCount -SkipRecommendedSettings:$SkipRecommendedSettings)
    $persistentTargets = @(Get-ZonalPersistentTargets)
    $targets = @((Get-IscsiSession -ErrorAction Stop).TargetNodeAddress) + $persistentTargets
    foreach ($plan in $plans) {
        $plan.Skip = @($targets | Where-Object {
            $_ -ieq $plan.RawIqn -or ($_ -is [string] -and $_.StartsWith("$($plan.RawIqn):az-", [StringComparison]::OrdinalIgnoreCase))
        }).Count -gt 0
        if (!$plan.Skip -and $VipDistribution) { $plan.Portals = @(Resolve-VipPortals $plan.HostName) }
    }
    Assert-EsanSessionLimit -Existing $persistentTargets.Count -New ($SessionCount * @($plans | Where-Object { !$_.Skip }).Count)

    if ($ZonalAffinity) { Write-Host 'Zonal IQN routing requires front-end suffix support.' -ForegroundColor Yellow }
    foreach ($plan in $plans) {
        if ($plan.Skip) {
            Write-Host "$($plan.Name) [$($plan.Iqn)]: already configured; run disconnect.ps1 first to change the layout" -ForegroundColor Magenta
            continue
        }
        try {
            foreach ($portal in ($plan.Portals | Select-Object -First $SessionCount)) {
                Invoke-ZonalIscsiCli -Arguments @('AddTarget', $plan.Iqn, '*', $portal, "$($plan.Port)", '*', '0', '*', '*', '*', '*', '*', '*', '*', '*', '*', '0')
            }
            for ($i = 0; $i -lt $SessionCount; $i++) {
                $portal = $plan.Portals[$i % $plan.Portals.Count]
                Invoke-ZonalIscsiCli -Arguments @('PersistentLoginTarget', $plan.Iqn.ToLowerInvariant(), 't', $portal.ToLowerInvariant(), "$($plan.Port)", 'Root\ISCSIPRT\0000_0', '-1', '*', '0x00000002', '1', '1', '*', '*', '*', '*', '*', '*', '*', '0')
            }
        } catch {
            throw "Volume '$($plan.Name)': $($_.Exception.Message) Sessions or persistent entries for this or earlier volumes may remain. No automatic rollback, disconnect or rebalance was attempted. Inspect Get-IscsiSession, Get-IscsiConnection and iscsicli ListPersistentTargets; use an operator-approved target-specific recovery procedure before retrying."
        }
        Write-Host "$($plan.Name) [$($plan.Iqn)]: Saved $SessionCount persistent login requests." -ForegroundColor Cyan
    }
    Write-Host 'Reboot is required to establish the saved persistent logins. The front end may redirect sessions after login; original portals do not determine final placement.' -ForegroundColor Yellow
    [pscustomobject]@{ Plans = $plans; RebootChanges = $rebootChanges }
}

##################### CHECK DEPENDENCY #################################
# The functions below harden every connect mode. Invoke-EsanConnect, called at the end of this file, runs
# them in order. Every stop is a throw from inside a function, never exit, so it can't close the console.

function Test-EsanAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-EsanWindowsEdition {
    # Multipath I/O is an optional feature on Windows 10/11 and a server feature on Windows Server, and
    # each is managed by different cmdlets.
    $productType = (Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop).ProductType
    switch ($productType) {
        1 { return 'Client' }
        { $_ -in 2, 3 } { return 'Server' }
    }
    throw "Unsupported Windows product type '$productType'. This script supports Windows 10/11 and Windows Server."
}

function Install-EsanIscsiService {
    $service = Get-Service -Name MSiSCSI -ErrorAction Stop
    if ("$($service.StartType)" -ne 'Automatic') {
        Set-Service -Name MSiSCSI -StartupType Automatic -ErrorAction Stop
        Write-Host 'Set the iSCSI initiator service (MSiSCSI) to start automatically.' -ForegroundColor Cyan
    }
    if ("$($service.Status)" -ne 'Running') {
        Start-Service -Name MSiSCSI -ErrorAction Stop
        Write-Host 'Started the iSCSI initiator service (MSiSCSI).' -ForegroundColor Cyan
    }
    if ("$((Get-Service -Name MSiSCSI -ErrorAction Stop).Status)" -ne 'Running') {
        throw 'The iSCSI initiator service (MSiSCSI) is not running. Start it with Start-Service MSiSCSI, then re-run this script. No sessions were added.'
    }
}

function Get-EsanMultipathIOState([string]$Edition) {
    if ($Edition -eq 'Server') {
        $feature = Get-WindowsFeature -Name Multipath-IO -ErrorAction Stop
        if ($null -eq $feature) { return 'Unavailable' }
        if ("$($feature.InstallState)" -eq 'Installed') { return 'Installed' }
        return 'NotInstalled'
    }
    try {
        $feature = Get-WindowsOptionalFeature -Online -FeatureName MultiPathIO -ErrorAction Stop
    } catch {
        # Some client editions don't include the MultiPathIO feature, and DISM reports it as unknown.
        return 'Unavailable'
    }
    if ($null -eq $feature) { return 'Unavailable' }
    if ("$($feature.State)" -eq 'Enabled') { return 'Installed' }
    'NotInstalled'
}

function Install-EsanMultipathIO([string]$Edition, [int]$SessionCount) {
    $state = Get-EsanMultipathIOState -Edition $Edition
    if ($state -eq 'Unavailable') {
        if ($SessionCount -gt 1) {
            throw "Multipath I/O isn't available on this Windows edition, and $SessionCount sessions per volume need it. Use Windows Server, or re-run this script with -NumSession 1. No sessions were added."
        }
        Write-Host 'Multipath I/O is not available on this Windows edition. Continuing with 1 session per volume.' -ForegroundColor Yellow
        return [pscustomobject]@{ Available = $false; RestartNeeded = $false }
    }
    $restartNeeded = $false
    if ($state -ne 'Installed') {
        if ($Edition -eq 'Server') {
            $result = Install-WindowsFeature -Name Multipath-IO -ErrorAction Stop
            if (-not $result.Success) {
                throw "Install-WindowsFeature Multipath-IO failed with exit code $($result.ExitCode). No sessions were added."
            }
            # RestartNeeded is an enum (No, Yes, Maybe). Anything but No means MPIO may not be active yet.
            $restartNeeded = "$($result.RestartNeeded)" -ne 'No'
        } else {
            $result = Enable-WindowsOptionalFeature -Online -FeatureName MultiPathIO -NoRestart -ErrorAction Stop
            $restartNeeded = [bool]$result.RestartNeeded
        }
        Write-Host 'Enabled Multipath I/O.' -ForegroundColor Cyan
    }
    [pscustomobject]@{ Available = $true; RestartNeeded = $restartNeeded }
}

function Test-EsanMsdsmIscsiClaim {
    (Get-MSDSMAutomaticClaimSettings -ErrorAction Stop).iSCSI -eq $true
}

function Enable-EsanMsdsmIscsiClaim {
    if (Test-EsanMsdsmIscsiClaim) { return }
    Enable-MSDSMAutomaticClaim -BusType iSCSI -Confirm:$false -ErrorAction Stop | Out-Null
    if (-not (Test-EsanMsdsmIscsiClaim)) {
        throw "MSDSM still doesn't claim iSCSI disks after Enable-MSDSMAutomaticClaim -BusType iSCSI. No sessions were added, because multiple sessions to a volume that MSDSM doesn't claim show up as duplicate disks."
    }
    Write-Host 'Enabled MSDSM automatic claiming of iSCSI disks.' -ForegroundColor Cyan
}

function Get-EsanIscsiRegistryRecommendation {
    [ordered]@{
        MaxTransferLength        = 262144
        MaxBurstLength           = 262144
        FirstBurstLength         = 262144
        MaxRecvDataSegmentLength = 262144
        InitialR2T               = 0
        ImmediateData            = 1
        WMIRequestTimeout        = 30
        LinkDownTime             = 30
    }
}

function Find-EsanIscsiInitiatorKey {
    # The instance number (0000, 0004, ...) differs between machines, so find the key by device instead
    # of hard-coding it. MatchingDeviceId is locale-independent; DriverDesc can be translated.
    $classKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e97b-e325-11ce-bfc1-08002be10318}'
    $keys = foreach ($key in @(Get-ChildItem -LiteralPath $classKey -ErrorAction SilentlyContinue)) {
        # Some subkeys, such as Properties, deny reads even to administrators.
        $values = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
        if ($values.MatchingDeviceId -eq 'root\iscsiprt' -or $values.DriverDesc -eq 'Microsoft iSCSI Initiator') { $key.PSPath }
    }
    @($keys) | Sort-Object -Unique
}

function Set-EsanRecommendedSettings([bool]$MpioActive) {
    # Changes only values that differ. Outputs the changes that take effect only after a restart.
    if ($MpioActive) {
        if ("$(Get-MSDSMGlobalDefaultLoadBalancePolicy -ErrorAction Stop)" -ne 'RR') {
            Set-MSDSMGlobalDefaultLoadBalancePolicy -Policy RR -ErrorAction Stop | Out-Null
            Write-Host 'Set the MSDSM default load balance policy to round robin (RR).' -ForegroundColor Cyan
        }
        if ((Get-MPIOSetting -ErrorAction Stop).DiskTimeoutValue -ne 30) {
            Set-MPIOSetting -NewDiskTimeout 30 -ErrorAction Stop | Out-Null
            Write-Host 'Set the MPIO disk timeout to 30 seconds.' -ForegroundColor Cyan
            'MPIO disk timeout'
        }
    }
    $keys = @(Find-EsanIscsiInitiatorKey)
    if ($keys.Count -ne 1) {
        Write-Host "Warning: found $($keys.Count) iSCSI initiator registry instances instead of 1. Skipped the recommended iSCSI initiator registry values." -ForegroundColor Yellow
        return
    }
    $parameters = Join-Path $keys[0] 'Parameters'
    $current = Get-ItemProperty -LiteralPath $parameters -ErrorAction Stop
    $recommended = Get-EsanIscsiRegistryRecommendation
    foreach ($name in $recommended.Keys) {
        if ($current.$name -ne $recommended[$name]) {
            New-ItemProperty -LiteralPath $parameters -Name $name -Value $recommended[$name] -PropertyType DWord -Force -ErrorAction Stop | Out-Null
            Write-Host "Set the iSCSI initiator registry value $name to $($recommended[$name])." -ForegroundColor Cyan
            $name
        }
    }
}

##################### GATHER INFORMATION OF INPUT VOLUMES ####################
function Get-EsanVolumeData([string]$ResourceGroupName, [string]$ElasticSanName, [string]$VolumeGroupName, [string[]]$VolumeName, [int]$NumSession) {
    # Get volume group resource to fail fast
    $null = Get-AzElasticSanVolumeGroup -ResourceGroupName $ResourceGroupName -ElasticSanName $ElasticSanName -Name $VolumeGroupName -ErrorAction Stop

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
    $volumesToConnect
}

############################### CONNECT VOLUMES ############################
function Get-EsanIscsiInventory {
    # Persistent logins come from the documented, locale-independent WMI class instead of localized
    # iscsicli output. Without a complete inventory a re-run could add duplicate persistent logins and
    # push the initiator toward its session limit, so any read failure stops the script.
    try {
        $sessions = @(Get-IscsiSession -ErrorAction Stop)
        $persistentLogins = @(Get-CimInstance -Namespace root\wmi -ClassName MSiSCSIInitiator_PersistentLoginClass -ErrorAction Stop)
    } catch {
        throw "Could not read the live iSCSI sessions or persistent logins, so the script stopped: $($_.Exception.Message)"
    }
    [pscustomobject]@{ Sessions = $sessions; PersistentLogins = $persistentLogins }
}

function Get-EsanConnectionPlan($Volumes) {
    # Decide for every volume before connecting any, so a stop never leaves a half-connected batch.
    # A volume selected more than once (same IQN, any case) is planned and connected once.
    $seen = @{}
    $Volumes = @(foreach ($volume in $Volumes) {
        if (-not $seen.ContainsKey($volume.TargetIQN)) { $seen[$volume.TargetIQN] = $true; $volume }
    })
    $inventory = Get-EsanIscsiInventory
    $plan = @(foreach ($volume in $Volumes) {
        $live = @($inventory.Sessions | Where-Object { $_.TargetNodeAddress -ieq $volume.TargetIQN }).Count
        $persistent = @($inventory.PersistentLogins | Where-Object { $_.TargetName -ieq $volume.TargetIQN }).Count
        $action = 'Skip'
        $warning = $null
        if ($live -eq 0 -and $persistent -eq 0) {
            $action = 'Connect'
            $message = 'Connecting to this volume'
        } elseif ($live -eq 0) {
            $message = "Skipped: persistent configuration exists but no live sessions (0 live / $persistent persistent)"
            $warning = 'persistent configuration exists but no live sessions. Reboot the VM (persistent logins re-establish at boot), or run disconnect.ps1 for this volume and re-run this script.'
        } else {
            $message = "Skipped: already connected ($live live / $persistent persistent)"
            if ($persistent -eq 0) {
                $warning = "the live sessions are not persistent and won't reconnect after a reboot. To make them persistent, run disconnect.ps1 for this volume and re-run this script."
            }
        }
        [pscustomobject]@{ Volume = $volume; Action = $action; Message = $message; Warning = $warning }
    })

    $newSessions = 0
    foreach ($item in $plan) {
        if ($item.Action -eq 'Connect') { $newSessions += $item.Volume.NumSession }
    }
    Assert-EsanSessionLimit -Existing $inventory.PersistentLogins.Count -New $newSessions
    $plan
}

function Assert-EsanSessionLimit([int]$Existing, [int]$New) {
    if ($Existing + $New -gt 256) {
        throw "Connecting would bring this machine to $($Existing + $New) persistent iSCSI logins ($Existing existing + $New new), above the Windows limit of 256. Re-run with a lower -NumSession or fewer volumes. No sessions were added."
    }
}

function Connect-EsanVolume($Volume) {
    iscsicli AddTarget $Volume.TargetIQN * $Volume.TargetHostName $Volume.TargetPort * 0 * * * * * * * * * 0
    # Keep the existing command lines: one PersistentLoginTarget per session and no separate LoginTarget,
    # which could double the sessions.
    $LoginOptions = '0x00000002'
    for ($i = 0; $i -lt $Volume.NumSession; $i++) {
        iscsicli PersistentLoginTarget $Volume.TargetIQN.ToLower() t $Volume.TargetHostname.ToLower() $Volume.TargetPort Root\ISCSIPRT\0000_0 -1 * $LoginOptions 1 1 * * * * * * * 0
    }
}

##################### VALIDATE ####################
function New-EsanCheck([string]$Status, [string]$Check, [string]$Detail) {
    [pscustomobject]@{ Status = $Status; Check = $Check; Detail = $Detail }
}

function Get-EsanCountStatus([int]$Actual, [int]$Requested, [string]$Shortfall) {
    if ($Actual -eq $Requested) { return 'PASS' }
    if ($Actual -gt $Requested) { return 'WARN' }
    $Shortfall
}

function Test-EsanConnection($Plan, [string]$Edition, [int]$SessionCount, [bool]$CheckRecommendedSettings) {
    # Read-only. Re-reads the machine state instead of trusting what earlier steps reported.
    $pending = New-Object System.Collections.Generic.List[string]
    $results = @(
        $service = Get-Service -Name MSiSCSI -ErrorAction Stop
        $status = if ("$($service.Status)" -eq 'Running' -and "$($service.StartType)" -eq 'Automatic') { 'PASS' } else { 'FAIL' }
        New-EsanCheck $status 'iSCSI service' "MSiSCSI is $($service.Status) with startup type $($service.StartType)"

        $mpioState = Get-EsanMultipathIOState -Edition $Edition
        $mpioInstalled = $mpioState -eq 'Installed'
        if ($mpioInstalled) {
            New-EsanCheck 'PASS' 'Multipath I/O' 'installed'
        } elseif ($SessionCount -le 1) {
            New-EsanCheck 'WARN' 'Multipath I/O' "$mpioState; not needed for 1 session per volume"
        } else {
            New-EsanCheck 'FAIL' 'Multipath I/O' "$mpioState; required for $SessionCount sessions per volume"
        }
        if ($mpioInstalled) {
            $status = if (Test-EsanMsdsmIscsiClaim) { 'PASS' } else { 'FAIL' }
            New-EsanCheck $status 'MSDSM iSCSI claim' "automatic claiming of iSCSI disks is $(if ($status -eq 'PASS') { 'enabled' } else { 'disabled' })"
        }

        if ($CheckRecommendedSettings) {
            if ($mpioInstalled) {
                $policy = "$(Get-MSDSMGlobalDefaultLoadBalancePolicy -ErrorAction Stop)"
                $status = if ($policy -eq 'RR') { 'PASS' } else { 'FAIL' }
                New-EsanCheck $status 'MSDSM load balance policy' "$policy (recommended RR)"
                $timeout = (Get-MPIOSetting -ErrorAction Stop).DiskTimeoutValue
                $status = if ($timeout -eq 30) { 'PASS' } else { 'FAIL' }
                New-EsanCheck $status 'MPIO disk timeout' "$timeout seconds (recommended 30)"
            }
            $keys = @(Find-EsanIscsiInitiatorKey)
            if ($keys.Count -ne 1) {
                New-EsanCheck 'WARN' 'iSCSI initiator registry' "found $($keys.Count) iSCSI initiator instances instead of 1; values not checked"
            } else {
                $current = Get-ItemProperty -LiteralPath (Join-Path $keys[0] 'Parameters') -ErrorAction Stop
                $recommended = Get-EsanIscsiRegistryRecommendation
                $wrong = @(foreach ($name in $recommended.Keys) {
                    if ($current.$name -ne $recommended[$name]) { "$name is '$($current.$name)', recommended $($recommended[$name])" }
                })
                if ($wrong.Count -eq 0) {
                    New-EsanCheck 'PASS' 'iSCSI initiator registry' "all $($recommended.Count) recommended values are set"
                } else {
                    New-EsanCheck 'FAIL' 'iSCSI initiator registry' ($wrong -join '; ')
                }
            }
        }

        $inventory = Get-EsanIscsiInventory
        foreach ($item in $Plan) {
            $volume = $item.Volume
            $label = "$($volume.VolumeName) [$($volume.TargetIQN)]"
            # This script never disconnects, so problems on volumes it skipped are pre-existing and only
            # warnings. Problems on volumes it connected in this run are failures.
            $shortfall = if ($item.Action -eq 'Connect') { 'FAIL' } else { 'WARN' }
            $sessions = @($inventory.Sessions | Where-Object { $_.TargetNodeAddress -ieq $volume.TargetIQN })
            $persistent = @($inventory.PersistentLogins | Where-Object { $_.TargetName -ieq $volume.TargetIQN }).Count
            if ($item.Action -eq 'Connect' -and $sessions.Count -lt $volume.NumSession) {
                # Windows may establish saved persistent logins only at boot, so missing live sessions on a
                # volume connected in this run are expected until the reboot. Its persistent logins are
                # the result this run is accountable for.
                $pending.Add($volume.VolumeName)
                New-EsanCheck 'WARN' "$label live sessions" "$($sessions.Count) of $($volume.NumSession) requested; the saved persistent logins establish the rest at boot"
            } else {
                New-EsanCheck (Get-EsanCountStatus $sessions.Count $volume.NumSession $shortfall) "$label live sessions" "$($sessions.Count) of $($volume.NumSession) requested"
            }
            New-EsanCheck (Get-EsanCountStatus $persistent $volume.NumSession $shortfall) "$label persistent logins" "$persistent of $($volume.NumSession) requested"
            if ($sessions.Count -gt 0) {
                $unhealthy = @($sessions | Where-Object { -not ($_.IsConnected -and $_.IsPersistent -and $_.IsHeaderDigest -and $_.IsDataDigest) }).Count
                if ($unhealthy -eq 0) {
                    New-EsanCheck 'PASS' "$label session state" "all $($sessions.Count) sessions are connected and persistent, with header and data digests"
                } else {
                    New-EsanCheck $shortfall "$label session state" "$unhealthy of $($sessions.Count) sessions are disconnected, not persistent, or missing a header or data digest"
                }
            }
        }
    )

    Write-Host 'Validating the configuration:' -ForegroundColor Cyan
    $colors = @{ PASS = 'Green'; WARN = 'Yellow'; FAIL = 'Red' }
    foreach ($result in $results) {
        Write-Host "[$($result.Status)] $($result.Check): $($result.Detail)" -ForegroundColor $colors[$result.Status]
    }
    $summary = [pscustomobject]@{
        Passed         = @($results | Where-Object { $_.Status -eq 'PASS' }).Count
        Warnings       = @($results | Where-Object { $_.Status -eq 'WARN' }).Count
        Failed         = @($results | Where-Object { $_.Status -eq 'FAIL' }).Count
        PendingVolumes = @($pending)
    }
    Write-Host "Validation: $($summary.Passed) passed, $($summary.Warnings) warnings, $($summary.Failed) failed" -ForegroundColor $(if ($summary.Failed -gt 0) { 'Red' } else { 'Green' })
    $summary
}

function Initialize-EsanHost([string]$Edition, [int]$SessionCount, [switch]$SkipRecommendedSettings) {
    # Prerequisites, recommended settings and the reboot gate, shared by every connect mode. Outputs the
    # setting changes that take effect only after a restart.
    Install-EsanIscsiService
    $mpio = Install-EsanMultipathIO -Edition $Edition -SessionCount $SessionCount
    $mpioActive = $mpio.Available -and -not $mpio.RestartNeeded
    if ($mpioActive) { Enable-EsanMsdsmIscsiClaim }

    $rebootChanges = @()
    if (-not $SkipRecommendedSettings) {
        $rebootChanges = @(Set-EsanRecommendedSettings -MpioActive $mpioActive)
    }
    if ($mpio.RestartNeeded) {
        # The feature cmdlet's RestartNeeded is the only signal that blocks connecting.
        throw 'Windows needs a restart to finish enabling Multipath I/O. No sessions were added, because sessions added before MPIO is active show up as duplicate disks. Reboot the VM, then re-run this script to finish MSDSM claiming, the MPIO settings and the connections.'
    }
    $rebootChanges
}

function Complete-EsanConnection($Plan, [string]$Edition, [int]$SessionCount, [switch]$SkipRecommendedSettings, [string[]]$RebootChanges, [switch]$SessionRebootAnnounced) {
    # Validation, reboot notice and the final error, shared by every connect mode.
    $validation = Test-EsanConnection -Plan $Plan -Edition $Edition -SessionCount $SessionCount -CheckRecommendedSettings (-not $SkipRecommendedSettings)
    $reasons = @()
    if ($RebootChanges.Count -gt 0) {
        $reasons += "changed $($RebootChanges -join ', '), which take effect only after the VM restarts"
    }
    # The zonal/VIP path already tells the runner to reboot for its saved logins; don't repeat it.
    if ($validation.PendingVolumes.Count -gt 0 -and -not $SessionRebootAnnounced) {
        $reasons += "the saved persistent logins for $($validation.PendingVolumes -join ', ') establish their sessions at boot"
    }
    if ($reasons.Count -gt 0) {
        Write-Host "Reboot required: $($reasons -join '; ')." -ForegroundColor Yellow
    }
    if ($validation.Failed -gt 0) {
        throw "Validation failed: $($validation.Failed) check(s) failed. Review the [FAIL] lines above."
    }
}

function Invoke-EsanConnect {
    param(
        [string]$ResourceGroupName,
        [string]$ElasticSanName,
        [string]$VolumeGroupName,
        [string[]]$VolumeName,
        [int]$NumSession,
        [switch]$SkipRecommendedSettings,
        [switch]$EnableZonalAffinity,
        [switch]$EnableVipDistribution
    )
    if (-not (Test-EsanAdministrator)) {
        throw 'Run this script from an elevated PowerShell session (Run as administrator). Without elevation, iscsicli output can look successful while no sessions are added.'
    }
    $edition = Get-EsanWindowsEdition
    # A volume selected more than once is connected once. Azure resource names are case-insensitive.
    $seen = @{}
    $VolumeName = @(foreach ($name in $VolumeName) {
        if (-not $seen.ContainsKey("$name")) { $seen["$name"] = $true; $name }
    })

    if ($EnableZonalAffinity -or $EnableVipDistribution) {
        # Zonal mapping and VIP distribution keep their own lookup, existing-state checks and connect
        # commands. Connect-ElasticSanVolumes runs Initialize-EsanHost right after its read-only lookup.
        $sessionCount = if ($NumSession -ge 1) { $NumSession } else { 32 }
        $result = Connect-ElasticSanVolumes $ResourceGroupName $ElasticSanName $VolumeGroupName $VolumeName $sessionCount -ZonalAffinity:$EnableZonalAffinity -VipDistribution:$EnableVipDistribution -Edition $edition -SkipRecommendedSettings:$SkipRecommendedSettings |
            Select-Object -Last 1
        # Validate the target each mode actually connected: the decorated IQN when zonal.
        $plan = @(foreach ($item in $result.Plans) {
            [pscustomobject]@{
                Volume = [pscustomobject]@{ VolumeName = $item.Name; TargetIQN = $item.Iqn; NumSession = $sessionCount }
                Action = if ($item.Skip) { 'Skip' } else { 'Connect' }
            }
        })
        Complete-EsanConnection -Plan $plan -Edition $edition -SessionCount $sessionCount -SkipRecommendedSettings:$SkipRecommendedSettings -RebootChanges $result.RebootChanges -SessionRebootAnnounced
        return
    }

    $volumes = @(Get-EsanVolumeData -ResourceGroupName $ResourceGroupName -ElasticSanName $ElasticSanName -VolumeGroupName $VolumeGroupName -VolumeName $VolumeName -NumSession $NumSession)
    $sessionCount = [int]($volumes | Measure-Object -Property NumSession -Maximum).Maximum
    $rebootChanges = @(Initialize-EsanHost -Edition $edition -SessionCount $sessionCount -SkipRecommendedSettings:$SkipRecommendedSettings)

    $plan = @(Get-EsanConnectionPlan -Volumes $volumes)
    foreach ($item in $plan) {
        $label = "$($item.Volume.VolumeName) [$($item.Volume.TargetIQN)]"
        if ($item.Action -eq 'Connect') {
            Write-Host "${label}: $($item.Message)" -ForegroundColor Cyan
            Connect-EsanVolume -Volume $item.Volume
        } else {
            Write-Host "${label}: $($item.Message)" -ForegroundColor Magenta
            if ($item.Warning) { Write-Host "${label}: Warning: $($item.Warning)" -ForegroundColor Yellow }
        }
    }
    Complete-EsanConnection -Plan $plan -Edition $edition -SessionCount $sessionCount -SkipRecommendedSettings:$SkipRecommendedSettings -RebootChanges $rebootChanges
}

Invoke-EsanConnect -ResourceGroupName $ResourceGroupName -ElasticSanName $ElasticSanName -VolumeGroupName $VolumeGroupName -VolumeName $VolumeName -NumSession $NumSession -SkipRecommendedSettings:$SkipRecommendedSettings -EnableZonalAffinity:$EnableZonalAffinity -EnableVipDistribution:$EnableVipDistribution
