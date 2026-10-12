$connectPath = Join-Path $PSScriptRoot 'connect.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($connectPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
foreach ($definition in $ast.FindAll({ param($node)
    ($node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and ($node.Name -like '*-Zonal*' -or $node.Name -like '*-Esan*')) -or
    $node -is [System.Management.Automation.Language.TypeDefinitionAst] }, $false)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}

function Get-AzContext { [CmdletBinding()]param() }
function Get-AzElasticSanVolumeGroup { [CmdletBinding()]param($ResourceGroupName, $ElasticSanName, $Name) }
function Get-AzElasticSanVolume { [CmdletBinding()]param($ResourceGroupName, $ElasticSanName, $VolumeGroupName, $Name) }
function Get-WindowsFeature { [CmdletBinding()]param($Name) }
function Get-IscsiSession { [CmdletBinding()]param() }
function iscsicli {
    $script:nativeCalls.Add(@($args))
    $global:LASTEXITCODE = 0
    'The operation completed successfully.'
}

function New-TestLocations([string]$Mappings = '[{"logicalZone":"2","physicalZone":"eastus-az3"}]') {
    ,(ConvertFrom-Json ('{"value":[{"name":"eastus","availabilityZoneMappings":' + $Mappings + '}]}')).value
}

Describe 'Mapping and complete IQN contract' {
    It 'normalizes mapping values without changing the service identity' {
        $locations = New-TestLocations '[{"logicalZone":" 2 ","physicalZone":" EASTUS-AZ3 "}]'
        (Get-ZonalPhysicalZone $locations ' EASTUS ' ' 2 ') | Should Be 'eastus-az3'
        (Get-ZonalTargetIqn 'iqn.2023-01.example:volume-1' ' EASTUS-AZ3 ') | Should Be 'iqn.2023-01.example:volume-1:az-eastus-az3'
    }
    It 'rejects missing, malformed and duplicate mappings including unused entries' {
        foreach ($mappings in @(
            '[]', '{}', 'null', '[null]', '[{"logicalZone":"2"}]',
            '[{"logicalZone":2,"physicalZone":"eastus-az3"}]',
            '[{"logicalZone":"1","physicalZone":"eastus-az1"}]',
            '[{"logicalZone":"2","physicalZone":"eastus-az3"},{"logicalZone":" 2 ","physicalZone":"eastus-az1"}]',
            '[{"logicalZone":"2","physicalZone":"eastus-az3"},{"logicalZone":"1","physicalZone":" EASTUS-AZ3 "}]',
            '[{"logicalZone":"2","physicalZone":"eastus-az3"},{"logicalZone":"1","physicalZone":"bad/zone"}]'
        )) {
            { Get-ZonalPhysicalZone (New-TestLocations $mappings) 'eastus' '2' } | Should Throw
        }
    }
    It 'rejects absent, duplicate and malformed regions' {
        $region = (New-TestLocations)[0]
        foreach ($locations in @(@(), @($region, $region), @($null), @([pscustomobject]@{name=1}))) {
            { Get-ZonalPhysicalZone $locations 'eastus' '2' } | Should Throw
        }
        { Get-ZonalPhysicalZone @($region) 'westus' '2' } | Should Throw
        { Get-ZonalPhysicalZone $region 'eastus' '2' } | Should Throw
    }
    It 'fails closed on the whole IQN rather than lowercasing an opaque identity' {
        foreach ($iqn in @('iqn.test:UPPER', 'IQN.test:one', 'iqn.test:one/part', 'iqn.test:one_part',
            'iqn.test:one@part', 'iqn.test:one:az-old', 'iqn.test:one ', "iqn.test:one`n", '',
            "iqn.test:$([char]0x00e9)", 'not-an-iqn', $null)) {
            { Get-ZonalTargetIqn $iqn 'eastus-az3' } | Should Throw
        }
    }
    It 'accepts 223 UTF-8 bytes and rejects 224 bytes after decoration' {
        (Get-ZonalTargetIqn ('iqn.' + ('a' * 214)) 'x').Length | Should Be 223
        { Get-ZonalTargetIqn ('iqn.' + ('a' * 215)) 'x' } | Should Throw '223 UTF-8 bytes'
        foreach ($zone in @('', 'bad/zone', "zone`nother", '_zone')) {
            { Get-ZonalTargetIqn 'iqn.test:one' $zone } | Should Throw
        }
    }
    It 'accepts only canonical subscription GUIDs' {
        (ConvertTo-ZonalSubscriptionId ' ABCDEF01-2345-6789-ABCD-0123456789AB ') | Should Be 'abcdef01-2345-6789-abcd-0123456789ab'
        foreach ($value in @('abcdef0123456789abcd0123456789ab', '{abcdef01-2345-6789-abcd-0123456789ab}', 'my-sub', 1, $null)) {
            { ConvertTo-ZonalSubscriptionId $value } | Should Throw
        }
    }
}

Describe 'Bounded metadata and provider requests' {
    It 'configures IMDS with Metadata header, proxy bypass and a complete-response timeout' {
        $client = New-ZonalMetadataClient
        try {
            $client.Timeout.TotalSeconds | Should Be 5
            ($client.DefaultRequestHeaders.GetValues('Metadata') -join '') | Should Be 'true'
        } finally { $client.Dispose() }
        $definition = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'New-ZonalMetadataClient' }, $false)[0]
        $definition.Extent.Text | Should Match '\.UseProxy = \$false'
        $definition = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-ZonalComputeMetadata' }, $false)[0]
        $definition.Extent.Text | Should Match '169\.254\.169\.254/metadata/instance/compute\?api-version=2021-02-01'
        $definition.Extent.Text | Should Match 'GetStringAsync'
    }
    It 'returns real in-process objects without serializing the authentication context' {
        $value = [pscustomobject]@{ TokenCachePlaceholder = New-Object object }
        $result = Invoke-ZonalProviderRequest 'Write-Output' @{ InputObject = $value }
        [object]::ReferenceEquals($result.TokenCachePlaceholder, $value.TokenCachePlaceholder) | Should Be $true
    }
    It 'propagates provider errors and cancels a stalled local pipeline' {
        { Invoke-ZonalProviderRequest 'Write-Error' @{ Message = 'provider failed' } } | Should Throw 'provider failed'
        $watch = [Diagnostics.Stopwatch]::StartNew()
        { Invoke-ZonalProviderRequest 'Start-Sleep' @{ Seconds = 30 } -TimeoutSeconds 1 } | Should Throw 'deadline'
        $watch.Elapsed.TotalSeconds | Should BeLessThan 10
    }
    It 'returns at the deadline even when a provider ignores cancellation' {
        Initialize-ZonalProviderCancellation
        $watch = [Diagnostics.Stopwatch]::StartNew()
        { Invoke-ZonalProviderRequest 'Invoke-Expression' @{
            Command = '[System.Threading.Thread]::Sleep(4000); "late provider result"'
        } -TimeoutSeconds 1 } | Should Throw 'result will be discarded'
        $watch.Elapsed.TotalSeconds | Should BeLessThan 3
    }
    It 'disposes the timed-out pipeline after its outstanding read finally exits' {
        Initialize-ZonalProviderCancellation
        $pipeline = [System.Management.Automation.PowerShell]::Create()
        $started = New-Object System.Threading.ManualResetEvent($false)
        $null = $pipeline.AddScript('param($started) $null = $started.Set(); [System.Threading.Thread]::Sleep(1000)').AddArgument($started)
        $pending = $pipeline.BeginInvoke()
        $started.WaitOne(5000) | Should Be $true
        [ElasticSan.ZonalProviderCancellation]::StopAndDispose($pipeline, $pending)
        $disposed = $false
        $watch = [Diagnostics.Stopwatch]::StartNew()
        while (!$disposed -and $watch.Elapsed.TotalSeconds -lt 5) {
            try { $null = $pending.AsyncWaitHandle.WaitOne(0) }
            catch [System.ObjectDisposedException] { $disposed = $true }
            if (!$disposed) { Start-Sleep -Milliseconds 50 }
        }
        $disposed | Should Be $true
        { $pipeline.BeginInvoke() } | Should Throw
        $started.Dispose()
    }
}

Describe 'Subscription-scoped mapping discovery' {
    BeforeEach {
        $script:subscription = 'abcdef01-2345-6789-abcd-0123456789ab'
        $script:context = [pscustomobject]@{ Subscription = [pscustomobject]@{ Id = $script:subscription } }
        $script:compute = [pscustomobject]@{ zone = '2'; subscriptionId = $script:subscription; location = 'eastus' }
        $script:sanLocation = 'eastus'; $script:armStatus = 200
        $script:armContent = '{"value":[{"name":"eastus","availabilityZoneMappings":[{"logicalZone":"2","physicalZone":"eastus-az3"}]}]}'
        Mock Get-ZonalComputeMetadata { $script:compute }
        Mock Invoke-ZonalProviderRequest {
            if ($Command -eq 'Get-AzElasticSan') { [pscustomobject]@{ Location = $script:sanLocation } }
            elseif ($Command -eq 'Invoke-AzRestMethod') { [pscustomobject]@{ StatusCode = $script:armStatus; Content = $script:armContent } }
            else { throw "Unexpected provider command $Command" }
        }
    }
    It 'retains the explicit subscription and original authentication context' {
        (Resolve-ZonalPhysicalZone $script:context $script:subscription 'rg' 'san') | Should Be 'eastus-az3'
        Assert-MockCalled Invoke-ZonalProviderRequest -Scope It -Times 1 -Exactly -ParameterFilter {
            $Command -eq 'Get-AzElasticSan' -and $Parameters.SubscriptionId -eq $script:subscription -and
            [object]::ReferenceEquals($Parameters.DefaultProfile, $script:context)
        }
        Assert-MockCalled Invoke-ZonalProviderRequest -Scope It -Times 1 -Exactly -ParameterFilter {
            $Command -eq 'Invoke-AzRestMethod' -and $Parameters.Method -eq 'GET' -and
            $Parameters.Path -eq "/subscriptions/$script:subscription/locations?api-version=2022-12-01" -and
            [object]::ReferenceEquals($Parameters.DefaultProfile, $script:context)
        }
    }
    It 'rejects cross-subscription and non-zonal VMs before provider reads' {
        $script:compute.subscriptionId = '11111111-1111-1111-1111-111111111111'
        { Resolve-ZonalPhysicalZone $script:context $script:subscription 'rg' 'san' } | Should Throw 'subscription does not match'
        $script:compute.subscriptionId = $script:subscription
        $script:compute.zone = ''
        { Resolve-ZonalPhysicalZone $script:context $script:subscription 'rg' 'san' } | Should Throw 'not availability-zone pinned'
        Assert-MockCalled Invoke-ZonalProviderRequest -Scope It -Times 0 -Exactly
    }
    It 'rejects region mismatch, provider failures and malformed payloads' {
        $script:sanLocation = 'westus'
        { Resolve-ZonalPhysicalZone $script:context $script:subscription 'rg' 'san' } | Should Throw 'same region'
        $script:sanLocation = 'eastus'; $script:armStatus = 403
        { Resolve-ZonalPhysicalZone $script:context $script:subscription 'rg' 'san' } | Should Throw 'HTTP 403'
        $script:armStatus = 200; $script:armContent = '{'
        { Resolve-ZonalPhysicalZone $script:context $script:subscription 'rg' 'san' } | Should Throw
    }
}

Describe 'Default mode through the hardened entry point and golden commands' {
    BeforeEach {
        $script:nativeCalls = New-Object 'System.Collections.Generic.List[object]'
        # Elevation, edition, host preparation and validation are covered by connect.Hardening.Tests.ps1.
        # A whole-script run can't mock the elevation check, so these tests call the entry function.
        Mock Test-EsanAdministrator { $true }
        Mock Get-EsanWindowsEdition { 'Server' }
        Mock Initialize-EsanHost {}
        Mock Complete-EsanConnection {}
        Mock Get-CimInstance {}
        Mock Get-AzElasticSanVolumeGroup { [pscustomobject]@{} }
        Mock Get-AzElasticSanVolume { [pscustomobject]@{ StorageTargetIqn = 'iqn.test:ONE'; StorageTargetPortalHostname = 'PORTAL.EXAMPLE'; StorageTargetPortalPort = 3260 } }
        Mock Get-IscsiSession {}
        Mock Get-AzContext { throw 'Default mode must not request mapping.' }
        Mock Write-Host {}
    }
    It 'matches the hardened default execution block golden fingerprint' {
        # ADO 39929094 replaced the upstream legacy block with the hardened flow. The fingerprint now pins
        # that block, so zonal/VIP changes can't silently alter it; intentional hardening edits update it.
        $text = [IO.File]::ReadAllText($connectPath).Replace("`r`n", "`n")
        $marker = '##################### CHECK DEPENDENCY #################################'
        $legacy = $text.Substring($text.IndexOf($marker)).TrimEnd("`r", "`n")
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($legacy))).Replace('-', '').ToLowerInvariant() |
                Should Be 'a9246833f1aa32d2e79950736f4cf2fcd1c7ad9c2f6f43554bf213687301a52f'
        } finally { $sha.Dispose() }
    }
    It 'retains the default count, FQDN casing and literal native argv with neither switch' {
        Invoke-EsanConnect -ResourceGroupName 'rg' -ElasticSanName 'san' -VolumeGroupName 'vg' -VolumeName @('one')
        $script:nativeCalls.Count | Should Be 33
        ($script:nativeCalls[0] -join '|') | Should Be 'AddTarget|iqn.test:ONE|*|PORTAL.EXAMPLE|3260|*|0|*|*|*|*|*|*|*|*|*|0'
        ($script:nativeCalls[1] -join '|') | Should Be 'PersistentLoginTarget|iqn.test:one|t|portal.example|3260|Root\ISCSIPRT\0000_0|-1|*|0x00000002|1|1|*|*|*|*|*|*|*|0'
        Assert-MockCalled Get-AzContext -Scope It -Times 0 -Exactly
    }
    It 'retains the requested count with both switches explicitly disabled' {
        Invoke-EsanConnect -ResourceGroupName 'rg' -ElasticSanName 'san' -VolumeGroupName 'vg' -VolumeName @('one') -NumSession 2 -EnableZonalAffinity:$false -EnableVipDistribution:$false
        $script:nativeCalls.Count | Should Be 3
    }
    It 'retains case-insensitive existing-target skip behavior' {
        Mock Get-IscsiSession { [pscustomobject]@{ TargetNodeAddress = 'iqn.test:one' } }
        Invoke-EsanConnect -ResourceGroupName 'rg' -ElasticSanName 'san' -VolumeGroupName 'vg' -VolumeName @('one') -NumSession 1
        $script:nativeCalls.Count | Should Be 0
    }
}
