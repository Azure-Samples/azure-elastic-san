$connectPath = Join-Path $PSScriptRoot 'connect.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($connectPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -match '-' }, $false)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}

function Get-AzContext { [CmdletBinding()]param() }
function Get-WindowsFeature { [CmdletBinding()]param($Name) }
function Get-IscsiSession { [CmdletBinding()]param() }
function iscsicli {
    $script:nativeCalls.Add(@($args))
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'ListPersistentTargets') { $script:persistentOutput; return }
    $global:LASTEXITCODE = $script:nativeExit
    $script:nativeOutput
    # PersistentLoginTarget saves configuration; it does not create a live session.
}

function Set-TestConnectionBoundaries {
    Mock Get-ZonalComputeMetadata {
        [pscustomobject]@{ zone = '2'; subscriptionId = 'abcdef01-2345-6789-abcd-0123456789ab'; location = 'eastus' }
    }
    Mock Get-VipHostAddresses {
        if ($HostName -eq $script:dnsFailureHost) { throw [System.Net.Sockets.SocketException]::new(11001) }
        foreach ($address in $script:dnsAnswers) { [System.Net.IPAddress]::Parse($address) }
    }
    Mock Invoke-ZonalProviderRequest {
        $script:requests.Add([pscustomobject]@{ Command = $Command; Parameters = $Parameters })
        if (@($script:nativeCalls | Where-Object { $_[0] -ne 'ListPersistentTargets' }).Count) { throw 'mutation preceded batch discovery' }
        switch ($Command) {
            'Get-AzElasticSan' { [pscustomobject]@{ Location = 'eastus' } }
            'Invoke-AzRestMethod' { [pscustomobject]@{
                StatusCode = 200; Content = '{"value":[{"name":"eastus","availabilityZoneMappings":[{"logicalZone":"2","physicalZone":"eastus-az3"}]}]}'
            } }
            'Get-AzElasticSanVolumeGroup' { [pscustomobject]@{} }
            'Get-AzElasticSanVolume' {
                if ($Parameters.Name -eq 'two' -and $script:badSecond -eq 'provider') { throw 'second volume provider deadline' }
                $volume = [pscustomobject]@{
                    StorageTargetIqn = $(if ($script:duplicateIqn) { 'iqn.test:shared' } else { "iqn.test:$($Parameters.Name)" })
                    StorageTargetPortalHostname = "$($Parameters.Name).EXAMPLE"; StorageTargetPortalPort = $script:port
                }
                if ($Parameters.Name -eq 'two') {
                    switch ($script:badSecond) {
                        'iqn' { $volume.StorageTargetIqn = 'iqn.test:UPPER' }
                        'host' { $volume.StorageTargetPortalHostname = 'bad host' }
                        'port' { $volume.StorageTargetPortalPort = 65536 }
                    }
                }
                $volume
            }
            default { throw "Unexpected provider command $Command" }
        }
    }
}

Describe 'IPv4 portal selection' {
    BeforeEach { Mock Get-VipHostAddresses {} }
    It 'uses the requested synchronous host resolver boundary' {
        $definition = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-VipHostAddresses' }, $false)[0]
        $definition.Extent.Text | Should Match '\[System.Net.Dns\]::GetHostAddresses\(\$HostName\)'
    }
    It 'fails clearly for no addresses or IPv6-only answers' {
        { Resolve-VipPortals 'portal.example' } | Should Throw 'no IPv4 addresses'
        Mock Get-VipHostAddresses { [System.Net.IPAddress]::Parse('fd00::1') }
        { Resolve-VipPortals 'portal.example' } | Should Throw 'no IPv4 addresses'
    }
    It 'retains the original FQDN when only one distinct IPv4 remains' {
        Mock Get-VipHostAddresses {
            [System.Net.IPAddress]::Parse('10.0.0.9')
            [System.Net.IPAddress]::Parse('10.0.0.9')
            [System.Net.IPAddress]::Parse('fd00::1')
        }
        (@(Resolve-VipPortals 'PORTAL.EXAMPLE') -join ',') | Should Be 'PORTAL.EXAMPLE'
    }
    It 'deduplicates and sorts two or more IPv4 addresses numerically while ignoring IPv6' {
        Mock Get-VipHostAddresses {
            foreach ($address in @('10.0.0.10', 'fd00::1', '10.0.0.9', '10.0.0.10', '2.0.0.1')) {
                [System.Net.IPAddress]::Parse($address)
            }
        }
        (@(Resolve-VipPortals 'portal.example') -join ',') | Should Be '2.0.0.1,10.0.0.9,10.0.0.10'
        Mock Get-VipHostAddresses {
            [System.Net.IPAddress]::Parse('10.0.0.10'); [System.Net.IPAddress]::Parse('10.0.0.9')
        }
        (@(Resolve-VipPortals 'portal.example') -join ',') | Should Be '10.0.0.9,10.0.0.10'
    }
    It 'reports the hostname on resolver failure' {
        Mock Get-VipHostAddresses { throw [System.Net.Sockets.SocketException]::new(11001) }
        { Resolve-VipPortals 'failed.example' } | Should Throw "DNS lookup failed for 'failed.example'"
    }
}

Describe 'Persistent-login configuration without live-session polling' {
    BeforeEach {
        $script:nativeCalls = New-Object 'System.Collections.Generic.List[object]'
        $script:requests = New-Object 'System.Collections.Generic.List[object]'
        $script:nativeExit = 0; $script:nativeOutput = 'The operation completed successfully.'
        # Assumed English CLI format, not a captured native-host fixture.
        $script:persistentOutput = @('Total of 0 persistent targets', 'The operation completed successfully.')
        $script:existing = @(); $script:badSecond = ''; $script:duplicateIqn = $false
        $script:dnsAnswers = @('10.0.0.10', '10.0.0.9', '10.0.0.8')
        $script:dnsFailureHost = ''; $script:port = 3260
        # The shared host preparation (prerequisites, settings, reboot gate) is covered by
        # connect.Hardening.Tests.ps1; here it is a boundary.
        Mock Initialize-EsanHost {}
        Mock Get-IscsiSession { $script:existing }
        Mock Write-Host {}
        Mock Start-Sleep { throw 'Persistent logins must not wait for live sessions.' }
        Set-TestConnectionBoundaries
        Mock Get-AzContext {
            # The actual file defines helpers before its first external boundary.
            Set-TestConnectionBoundaries
            [pscustomobject]@{ Subscription = [pscustomobject]@{ Id = 'abcdef01-2345-6789-abcd-0123456789ab' } }
        }
    }
    foreach ($case in @(
        @{ Label='32 over 3'; Count=32; Addresses=@('10.0.0.10','10.0.0.9','10.0.0.8'); Counts=@(11,11,10) },
        @{ Label='32 over 2'; Count=32; Addresses=@('10.0.0.10','10.0.0.9'); Counts=@(16,16) },
        @{ Label='1 over 3'; Count=1; Addresses=@('10.0.0.10','10.0.0.9','10.0.0.8'); Counts=@(1,0,0) }
    )) {
        It "saves $($case.Label) using round-robin original portals without creating live sessions" {
            $script:dnsAnswers = $case.Addresses
            Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one') $case.Count -VipDistribution
            $logins = @($script:nativeCalls | Where-Object { $_[0] -eq 'PersistentLoginTarget' })
            $registrations = @($script:nativeCalls | Where-Object { $_[0] -eq 'AddTarget' })
            # The literal expected numeric order differs from lexical order for .9/.10.
            $portals = if ($case.Addresses.Count -eq 2) { @('10.0.0.9','10.0.0.10') } else { @('10.0.0.8','10.0.0.9','10.0.0.10') }
            $logins.Count | Should Be $case.Count
            $registrations.Count | Should Be ([Math]::Min($case.Count, $portals.Count))
            for ($i = 0; $i -lt $registrations.Count; $i++) {
                ($registrations[$i] -join '|') | Should Be "AddTarget|iqn.test:one|*|$($portals[$i])|3260|*|0|*|*|*|*|*|*|*|*|*|0"
            }
            for ($i = 0; $i -lt $logins.Count; $i++) {
                ($logins[$i] -join '|') | Should Be "PersistentLoginTarget|iqn.test:one|t|$($portals[$i % $portals.Count])|3260|Root\ISCSIPRT\0000_0|-1|*|0x00000002|1|1|*|*|*|*|*|*|*|0"
            }
            for ($i = 0; $i -lt $portals.Count; $i++) {
                @($logins | Where-Object { $_[3] -eq $portals[$i] }).Count | Should Be $case.Counts[$i]
            }
            $script:existing.Count | Should Be 0
            Assert-MockCalled Get-IscsiSession -Scope It -Times 1 -Exactly
            Assert-MockCalled Start-Sleep -Scope It -Times 0 -Exactly
            Assert-MockCalled Write-Host -Scope It -Times 1 -Exactly -ParameterFilter { "$Object" -like '*Reboot is required*' }
        }
    }
    It 'uses FQDN registration and login with one IPv4, preserving a non-default port' {
        $script:dnsAnswers = @('10.0.0.9'); $script:port = 3261
        Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one') 2 -VipDistribution
        ($script:nativeCalls[1] -join '|') | Should Be 'AddTarget|iqn.test:one|*|one.EXAMPLE|3261|*|0|*|*|*|*|*|*|*|*|*|0'
        $script:nativeCalls[2][3] | Should Be 'one.example'
        $script:nativeCalls[2][4] | Should Be '3261'
    }
    It 'skips any related live or persistent target, including another zone, without resolving DNS' {
        foreach ($target in @('iqn.test:one','IQN.TEST:ONE:az-eastus-az3','iqn.test:one:az-eastus-az1')) {
            $script:existing = @([pscustomobject]@{ TargetNodeAddress = $target })
            Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one') 32 -VipDistribution
            $script:existing = @()
            $script:persistentOutput = @('Total of 1 peristent targets', "`tTarget   Name `t: $target ", 'Address and Socket : 10.0.0.1 3260', 'The operation completed successfully.')
            Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one') 32 -VipDistribution
            $script:persistentOutput = @('Total of 0 persistent targets', 'The operation completed successfully.')
        }
        @($script:nativeCalls | Where-Object { $_[0] -ne 'ListPersistentTargets' }).Count | Should Be 0
        Assert-MockCalled Get-VipHostAddresses -Scope It -Times 0 -Exactly
        Assert-MockCalled Write-Host -Scope It -Times 6 -Exactly -ParameterFilter {
            "$Object" -like '*already configured; run disconnect.ps1 first to change the layout*'
        }
    }
    It 'does not mistake an unrelated or prefix-sharing IQN for this volume' {
        $script:existing = @([pscustomobject]@{ TargetNodeAddress = 'iqn.test:one-more:az-eastus-az3' })
        $script:persistentOutput = @('Total of 1 persistent targets','Target Name : iqn.test:other','The operation completed successfully.')
        Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one') 1 -VipDistribution
        @($script:nativeCalls | Where-Object { $_[0] -eq 'PersistentLoginTarget' }).Count | Should Be 1
    }
    It 'finishes batch DNS preflight before any registration or saved login' {
        $script:dnsFailureHost = 'two.EXAMPLE'
        { Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one','two') 32 -VipDistribution } | Should Throw "DNS lookup failed for 'two.EXAMPLE'"
        @($script:nativeCalls | Where-Object { $_[0] -ne 'ListPersistentTargets' }).Count | Should Be 0
        $script:dnsFailureHost = ''; $script:dnsAnswers = @()
        { Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one') 32 -VipDistribution } | Should Throw 'no IPv4 addresses'
        @($script:nativeCalls | Where-Object { $_[0] -ne 'ListPersistentTargets' }).Count | Should Be 0
    }
    It 'preserves mapping batch checks for invalid later IQN, host, port or provider result' {
        foreach ($failure in @('iqn','host','port','provider')) {
            $script:badSecond = $failure
            { Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one','two') 2 -ZonalAffinity -VipDistribution } | Should Throw
            $script:nativeCalls.Count | Should Be 0
        }
    }
    It 'rejects empty and duplicate selections and shared service IQNs before mutation' {
        { Connect-ElasticSanVolumes 'rg' 'san' 'vg' @() 2 -VipDistribution } | Should Throw 'No volumes'
        { Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one','ONE') 2 -VipDistribution } | Should Throw 'duplicate'
        $script:duplicateIqn = $true
        { Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one','two') 2 -VipDistribution } | Should Throw 'same raw IQN'
        $script:nativeCalls.Count | Should Be 0
    }
    It 'reports native failures and partial saved configuration without claiming live readiness' {
        $script:nativeExit = 5
        { Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one') 2 -VipDistribution } | Should Throw 'exit 5'
        $script:nativeCalls.Clear()
        $script:nativeExit = 0; $script:nativeOutput = 'The operation failed.'
        { Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one') 2 -VipDistribution } | Should Throw 'may remain'
        $script:nativeCalls.Clear()
        $script:nativeOutput = "The operation completed successfully.`nThe operation failed."
        { Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one') 2 -VipDistribution } | Should Throw 'unrecognized status'
    }
    It 'fails before mutation when the persistent listing fails, is truncated or unrecognized' {
        foreach ($listing in @(
            @('The operation failed.'),
            @('Total of 1 persistent targets','Target Name : ','The operation completed successfully.'),
            @('Total of 1 persistent targets','The operation completed successfully.'),
            @('unrecognized inventory','The operation completed successfully.'),
            @('Total of 0 persistent targets','Total of 0 persistent targets','The operation completed successfully.')
        )) {
            $script:persistentOutput = $listing
            { Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one') 2 -VipDistribution } | Should Throw
        }
        @($script:nativeCalls | Where-Object { $_[0] -ne 'ListPersistentTargets' }).Count | Should Be 0
    }
    It 'rejects counts outside 1-32 before discovery' {
        foreach ($count in @(0,33)) {
            { . $connectPath 'rg' 'san' 'vg' @('one') $count -EnableVipDistribution } | Should Throw
        }
        $script:requests.Count | Should Be 0
        $script:nativeCalls.Count | Should Be 0
    }
    It 'handles more than three VIPs without changing the requested session count' {
        $script:dnsAnswers = @('10.0.0.4','10.0.0.3','10.0.0.2','10.0.0.1')
        Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one') 7 -VipDistribution
        $logins = @($script:nativeCalls | Where-Object { $_[0] -eq 'PersistentLoginTarget' })
        $logins.Count | Should Be 7
        (@($logins | ForEach-Object { $_[3] }) -join ',') | Should Be '10.0.0.1,10.0.0.2,10.0.0.3,10.0.0.4,10.0.0.1,10.0.0.2,10.0.0.3'
    }
}

foreach ($mode in @(
    @{ Label='zonal only'; Zonal=$true; Vip=$false; Iqn='iqn.test:one:az-eastus-az3'; Portal='one.example'; Count=1 },
    @{ Label='VIP only'; Zonal=$false; Vip=$true; Iqn='iqn.test:one'; Portal='10.0.0.8'; Count=31 },
    @{ Label='both'; Zonal=$true; Vip=$true; Iqn='iqn.test:one:az-eastus-az3'; Portal='10.0.0.8'; Count=$null }
)) {
    # The mode tests call the script's entry function rather than dot-sourcing the whole script: the
    # hardened entry checks for an elevated session first, which a whole-script run can't mock.
    Describe "Entry point independent switches: $($mode.Label)" {
        It 'uses only the requested mechanisms and succeeds with no live session created' {
            $script:nativeCalls = New-Object 'System.Collections.Generic.List[object]'
            $script:requests = New-Object 'System.Collections.Generic.List[object]'
            $script:nativeExit = 0; $script:nativeOutput = 'The operation completed successfully.'
            $script:persistentOutput = @('Total of 0 persistent targets','The operation completed successfully.')
            $script:dnsAnswers = @('10.0.0.10','10.0.0.9','10.0.0.8')
            $script:dnsFailureHost = ''; $script:port = 3260; $script:badSecond = ''; $script:duplicateIqn = $false
            Mock Test-EsanAdministrator { $true }
            Mock Get-EsanWindowsEdition { 'Server' }
            Mock Initialize-EsanHost {}
            Mock Complete-EsanConnection { $script:validatedPlan = $Plan }
            Mock Get-IscsiSession {}
            Mock Write-Host {}
            Mock Get-AzContext {
                Set-TestConnectionBoundaries
                [pscustomobject]@{ Subscription = [pscustomobject]@{ Id = 'abcdef01-2345-6789-abcd-0123456789ab' } }
            }
            $parameters = @{
                ResourceGroupName='rg'; ElasticSanName='san'; VolumeGroupName='vg'; VolumeName=@('one')
                EnableZonalAffinity=$mode.Zonal; EnableVipDistribution=$mode.Vip
            }
            if ($null -ne $mode.Count) { $parameters.NumSession = $mode.Count }
            Invoke-EsanConnect @parameters
            $expectedCount = if ($null -eq $mode.Count) { 32 } else { $mode.Count }
            $logins = @($script:nativeCalls | Where-Object { $_[0] -eq 'PersistentLoginTarget' })
            $logins.Count | Should Be $expectedCount
            $logins[0][1] | Should Be $mode.Iqn
            $logins[0][3] | Should Be $mode.Portal
            # Shared validation checks the target this mode actually connected.
            "$($script:validatedPlan[0].Volume.TargetIQN)/$($script:validatedPlan[0].Volume.NumSession)/$($script:validatedPlan[0].Action)" |
                Should Be "$($mode.Iqn)/$expectedCount/Connect"
            Assert-MockCalled Get-ZonalComputeMetadata -Scope It -Times ([int]$mode.Zonal) -Exactly
            Assert-MockCalled Get-VipHostAddresses -Scope It -Times ([int]$mode.Vip) -Exactly
            @($script:requests | Where-Object { $_.Command -in @('Get-AzElasticSan','Invoke-AzRestMethod') }).Count | Should Be (2 * [int]$mode.Zonal)
            foreach ($request in $script:requests) {
                $request.Parameters.DefaultProfile.Subscription.Id | Should Be 'abcdef01-2345-6789-abcd-0123456789ab'
            }
            Assert-MockCalled Get-IscsiSession -Scope It -Times 1 -Exactly
        }
    }
}
