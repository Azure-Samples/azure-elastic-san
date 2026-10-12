$disconnectPath = Join-Path $PSScriptRoot 'disconnect.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($disconnectPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -like '*-Disconnect*' }, $false)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}

# Exercise the actual disconnect block without the interactive confirmation or
# Azure discovery. Native commands and session discovery stay mocked.
$source = [IO.File]::ReadAllText($disconnectPath)
$disconnectBlock = [scriptblock]::Create($source.Substring($source.IndexOf('############## Disconnect volumes')))
function Get-IscsiSession { [CmdletBinding()]param() }
function iscsicli {
    $script:nativeCalls.Add(@($args))
    $global:LASTEXITCODE = 0
    if ($args[0] -eq $script:failedCommand) {
        $global:LASTEXITCODE = $script:nativeExit
        'The operation failed.'
    } elseif ($args[0] -eq 'ListPersistentTargets') {
        $script:listing
    } else {
        'The operation completed successfully.'
    }
}

function New-TestPersistentListing($Entries) {
    'Microsoft iSCSI Initiator Version 10.0'
    "Total of $($Entries.Count) persistent targets"
    foreach ($entry in $Entries) {
        ''
        "    Target Name           : $($entry.Name)"
        "    Address and Socket    : $($entry.Address) $($entry.Port)"
        '    Session Type          : Data'
        '    Initiator Name        : Root\ISCSIPRT\0000_0'
        '    Port Number           : <Any Port>'
    }
    ''
    'The operation completed successfully.'
}

Describe 'Target-scoped Windows disconnect' {
    BeforeEach {
        $script:nativeCalls = New-Object 'System.Collections.Generic.List[object]'
        $script:failedCommand = ''
        $script:nativeExit = 0
        $script:sessions = @()
        $script:listing = @(New-TestPersistentListing @())
        $volumesToDisconnect = @([pscustomobject]@{
            VolumeName = 'one'; TargetIQN = 'iqn.test:one'
            TargetHostName = 'original.example'; TargetPort = 3260
        })
        Mock Get-IscsiSession { $script:sessions }
        Mock Write-Host {}
    }
    It 'disconnects a plain IQN using its saved FQDN and port' {
        $script:sessions = @([pscustomobject]@{ TargetNodeAddress = 'iqn.test:one'; SessionIdentifier = 'session-1' })
        $script:listing = @(New-TestPersistentListing @(
            @{ Name = 'iqn.test:one'; Address = 'saved.example'; Port = 3261 }
        ))
        . $disconnectBlock
        ($script:nativeCalls[1] -join '|') | Should Be 'LogoutTarget|session-1'
        ($script:nativeCalls[2] -join '|') | Should Be 'RemovePersistentTarget|ROOT\ISCSIPRT\0000_0|iqn.test:one|-1|saved.example|3261'
        ($script:nativeCalls[3] -join '|') | Should Be 'RemoveTarget|iqn.test:one'
    }
    It 'matches plain and multiple zonal IQNs case-insensitively and logs out every session' {
        $script:sessions = @(
            [pscustomobject]@{ TargetNodeAddress = 'IQN.TEST:ONE:AZ-eastus-az1'; SessionIdentifier = 'session-1' },
            [pscustomobject]@{ TargetNodeAddress = 'iqn.test:one:az-eastus-az1'; SessionIdentifier = 'session-2' },
            [pscustomobject]@{ TargetNodeAddress = 'iqn.test:one:az-eastus-az2'; SessionIdentifier = 'session-3' },
            [pscustomobject]@{ TargetNodeAddress = 'iqn.test:one'; SessionIdentifier = 'session-4' }
        )
        $script:listing = @(New-TestPersistentListing @(
            @{ Name = 'iqn.test:one:az-eastus-az1'; Address = 'portal.example'; Port = 3260 }
        ))
        . $disconnectBlock
        @($script:nativeCalls | Where-Object { $_[0] -eq 'LogoutTarget' }).Count | Should Be 4
        @($script:nativeCalls | Where-Object { $_[0] -eq 'RemovePersistentTarget' }).Count | Should Be 1
        $targets = @($script:nativeCalls | Where-Object { $_[0] -eq 'RemoveTarget' } | ForEach-Object { $_[1] })
        $targets.Count | Should Be 3
        ($targets -contains 'iqn.test:one:az-eastus-az1') | Should Be $true
        ($targets -contains 'iqn.test:one:az-eastus-az2') | Should Be $true
        ($targets -contains 'iqn.test:one') | Should Be $true
    }
    It 'removes all persistent-only logins using their own IPv4 and IPv6 portals' {
        $script:listing = @(New-TestPersistentListing @(
            @{ Name = 'iqn.test:one:az-eastus-az3'; Address = '10.0.0.1'; Port = 3260 },
            @{ Name = 'iqn.test:one:az-eastus-az3'; Address = '10.0.0.2'; Port = 3261 },
            @{ Name = 'iqn.test:one:az-eastus-az3'; Address = 'fd00::1'; Port = 3260 },
            @{ Name = 'iqn.test:one:az-eastus-az3'; Address = 'fd00::1'; Port = 3260 }
        ))
        . $disconnectBlock
        $removals = @($script:nativeCalls | Where-Object { $_[0] -eq 'RemovePersistentTarget' })
        $removals.Count | Should Be 4
        ($removals[0] -join '|') | Should Be 'RemovePersistentTarget|ROOT\ISCSIPRT\0000_0|iqn.test:one:az-eastus-az3|-1|10.0.0.1|3260'
        ($removals[1] -join '|') | Should Be 'RemovePersistentTarget|ROOT\ISCSIPRT\0000_0|iqn.test:one:az-eastus-az3|-1|10.0.0.2|3261'
        ($removals[2] -join '|') | Should Be 'RemovePersistentTarget|ROOT\ISCSIPRT\0000_0|iqn.test:one:az-eastus-az3|-1|fd00::1|3260'
        @($script:nativeCalls | Where-Object { $_[0] -eq 'LogoutTarget' }).Count | Should Be 0
        @($script:nativeCalls | Where-Object { $_[0] -eq 'RemoveTarget' }).Count | Should Be 1
        Assert-MockCalled Write-Host -Times 0 -Exactly -Scope It -ParameterFilter { "$Object" -like '*not connected*' }
    }
    It 'removes a live-only target even without a saved login' {
        $script:sessions = @([pscustomobject]@{ TargetNodeAddress = 'iqn.test:one:az-eastus-az3'; SessionIdentifier = 'session-1' })
        . $disconnectBlock
        $script:nativeCalls.Count | Should Be 3
        ($script:nativeCalls[1] -join '|') | Should Be 'LogoutTarget|session-1'
        ($script:nativeCalls[2] -join '|') | Should Be 'RemoveTarget|iqn.test:one:az-eastus-az3'
    }
    It 'processes each selected raw IQN once without aborting later volumes' {
        $volumesToDisconnect += [pscustomobject]@{ VolumeName = 'ONE'; TargetIQN = 'IQN.TEST:ONE' }
        $volumesToDisconnect += [pscustomobject]@{ VolumeName = 'two'; TargetIQN = 'iqn.test:two' }
        $script:listing = @(New-TestPersistentListing @(
            @{ Name = 'iqn.test:one:az-eastus-az3'; Address = '10.0.0.1'; Port = 3260 },
            @{ Name = 'iqn.test:two'; Address = 'two.example'; Port = 3260 }
        ))
        . $disconnectBlock
        $removals = @($script:nativeCalls | Where-Object { $_[0] -eq 'RemovePersistentTarget' })
        $removals.Count | Should Be 2
        $removals[0][2] | Should Be 'iqn.test:one:az-eastus-az3'
        $removals[1][2] | Should Be 'iqn.test:two'
        @($script:nativeCalls | Where-Object { $_[0] -eq 'RemoveTarget' }).Count | Should Be 2
    }
    It 'leaves similar-prefix unrelated targets and their sessions untouched' {
        $unrelated = @('iqn.test:one-more', 'iqn.test:one2:az-eastus-az3', 'iqn.test:one:azure', 'iqn.test:one:azfoo', 'iqn.test:other')
        $script:sessions = @($unrelated | ForEach-Object { [pscustomobject]@{ TargetNodeAddress = $_; SessionIdentifier = $_ } })
        $entries = @($unrelated | ForEach-Object { @{ Name = $_; Address = 'other.example'; Port = 3260 } })
        $entries += @{ Name = 'iqn.test:one:az-eastus-az3'; Address = '10.0.0.1'; Port = 3260 }
        $script:listing = @(New-TestPersistentListing $entries)
        . $disconnectBlock
        $script:nativeCalls.Count | Should Be 3
        ($script:nativeCalls[1] -join '|') | Should Be 'RemovePersistentTarget|ROOT\ISCSIPRT\0000_0|iqn.test:one:az-eastus-az3|-1|10.0.0.1|3260'
        ($script:nativeCalls[2] -join '|') | Should Be 'RemoveTarget|iqn.test:one:az-eastus-az3'
    }
    It 'reports not connected only when both live and persistent matches are absent' {
        $script:listing = @(New-TestPersistentListing @(@{ Name = 'iqn.test:other'; Address = 'other.example'; Port = 3260 }))
        . $disconnectBlock
        $script:nativeCalls.Count | Should Be 1
        Assert-MockCalled Write-Host -Times 1 -Exactly -Scope It -ParameterFilter { "$Object" -like '*not connected*' }
    }
    It 'parses flexible whitespace without splitting the colons inside an IPv6 portal' {
        $script:listing = @(
            'Total of 1 persistent targets',
            "`tTarget Name`t: IQN.TEST:ONE:AZ-eastus-az3",
            "`tAddress and Socket : fd00:1::2`t3260 ",
            'The operation completed successfully.'
        )
        . $disconnectBlock
        ($script:nativeCalls[1] -join '|') | Should Be 'RemovePersistentTarget|ROOT\ISCSIPRT\0000_0|IQN.TEST:ONE:AZ-eastus-az3|-1|fd00:1::2|3260'
    }
    It 'rejects missing, duplicated or malformed portal fields before any logout' {
        $script:sessions = @([pscustomobject]@{ TargetNodeAddress = 'iqn.test:one'; SessionIdentifier = 'session-1' })
        foreach ($portalLines in @(
            @(),
            @('Address and Socket : 10.0.0.1'),
            @('Address and Socket : 10.0.0.1 65536'),
            @('Address and Socket : 10.0.0.1 3260', 'Address and Socket : 10.0.0.2 3260')
        )) {
            $script:nativeCalls.Clear()
            $script:listing = @('Total of 1 persistent targets', 'Target Name : iqn.test:one') + $portalLines + 'The operation completed successfully.'
            { . $disconnectBlock } | Should Throw 'persistent'
            $script:nativeCalls.Count | Should Be 1
        }
    }
    It 'refuses a truncated or unrecognized persistent listing before mutation' {
        foreach ($listing in @(
            @('Total of 2 persistent targets', 'Target Name : iqn.test:one', 'Address and Socket : 10.0.0.1 3260', 'The operation completed successfully.'),
            @('Unrecognized inventory', 'The operation completed successfully.')
        )) {
            $script:nativeCalls.Clear()
            $script:listing = $listing
            { . $disconnectBlock } | Should Throw 'persistent'
            $script:nativeCalls.Count | Should Be 1
        }
    }
    It 'stops when native inventory fails even with exit status zero' {
        $script:failedCommand = 'ListPersistentTargets'
        { . $disconnectBlock } | Should Throw 'ListPersistentTargets'
        $script:nativeCalls.Count | Should Be 1
    }
    It 'stops on native removal failure instead of claiming cleanup succeeded' {
        $script:listing = @(New-TestPersistentListing @(@{ Name = 'iqn.test:one'; Address = '10.0.0.1'; Port = 3260 }))
        $script:failedCommand = 'RemovePersistentTarget'
        $script:nativeExit = 5
        { . $disconnectBlock } | Should Throw 'exit 5'
        $script:nativeCalls.Count | Should Be 2
    }
}
