# Behavior tests for the hardened connect.ps1 flow (ADO Task 39929094): the shared host preparation,
# existing-state policy, validation and reboot notice for every connect mode.
# Written for Pester 3.4, the version that ships with Windows:
#   Invoke-Pester -Path .\connect.Hardening.Tests.ps1
# The script itself never runs. Only its functions are loaded, and every host command they use is
# replaced by a stub that throws unless a test mocks it, so nothing on the test machine is read or changed.

$connectPath = Join-Path $PSScriptRoot 'connect.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($connectPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -match '-' }, $false)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}

function Stop-UnmockedCall([string]$Name) { throw "Unmocked host command '$Name' was called." }
function Get-AzContext { [CmdletBinding()] param() Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Get-AzElasticSanVolumeGroup { [CmdletBinding()] param($ResourceGroupName, $ElasticSanName, $Name) Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Get-AzElasticSanVolume { [CmdletBinding()] param($ResourceGroupName, $ElasticSanName, $VolumeGroupName, $Name) Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Get-CimInstance { [CmdletBinding()] param($Namespace, $ClassName) Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Get-Service { [CmdletBinding()] param($Name) Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Set-Service { [CmdletBinding()] param($Name, $StartupType) Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Start-Service { [CmdletBinding()] param($Name) Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Get-WindowsOptionalFeature { [CmdletBinding()] param([switch]$Online, $FeatureName) Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Enable-WindowsOptionalFeature { [CmdletBinding()] param([switch]$Online, $FeatureName, [switch]$NoRestart) Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Get-WindowsFeature { [CmdletBinding()] param($Name) Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Install-WindowsFeature { [CmdletBinding()] param($Name) Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Get-MSDSMAutomaticClaimSettings { [CmdletBinding()] param() Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Enable-MSDSMAutomaticClaim { [CmdletBinding(SupportsShouldProcess)] param($BusType) Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Get-MSDSMGlobalDefaultLoadBalancePolicy { [CmdletBinding()] param() Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Set-MSDSMGlobalDefaultLoadBalancePolicy { [CmdletBinding()] param($Policy) Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Get-MPIOSetting { [CmdletBinding()] param() Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Set-MPIOSetting { [CmdletBinding()] param($NewDiskTimeout) Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Get-IscsiSession { [CmdletBinding()] param() Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function Get-ItemProperty { [CmdletBinding()] param($LiteralPath, $Name) Stop-UnmockedCall $MyInvocation.MyCommand.Name }
function New-ItemProperty { [CmdletBinding()] param($LiteralPath, $Name, $Value, $PropertyType, [switch]$Force) Stop-UnmockedCall $MyInvocation.MyCommand.Name }

# Records every iscsicli call. A PersistentLoginTarget saves one persistent login and, unless a test turns
# it off, also creates one live session, so validation sees the result of the connect step.
function iscsicli {
    $script:nativeCalls.Add(@($args))
    if ($args[0] -eq 'PersistentLoginTarget' -and $script:createdSessions -lt $script:maxNewSessions) {
        $script:createdSessions++
        if ($script:createLiveSessions) { $script:sessions += New-TestSession ([string]$args[1]) -Digest $script:newSessionDigest }
        $script:persistent += [pscustomobject]@{ TargetName = [string]$args[1] }
    }
}

function New-TestVolume([string]$Name, [string]$Iqn, [int]$Sessions = 32, [string]$HostName = 'es-test.z1.blob.storage.azure.net', $Port = 3260) {
    [pscustomobject]@{ VolumeName = $Name; TargetIQN = $Iqn; TargetHostName = $HostName; TargetPort = $Port; NumSession = $Sessions }
}

function New-TestSession([string]$Iqn, [bool]$Digest = $true, [bool]$Persistent = $true) {
    [pscustomobject]@{ TargetNodeAddress = $Iqn; IsConnected = $true; IsPersistent = $Persistent; IsHeaderDigest = $Digest; IsDataDigest = $Digest }
}

function Add-TestState([string]$Iqn, [int]$Live, [int]$Persistent, [bool]$LivePersistent = $true) {
    for ($i = 0; $i -lt $Live; $i++) { $script:sessions += New-TestSession $Iqn -Persistent $LivePersistent }
    for ($i = 0; $i -lt $Persistent; $i++) { $script:persistent += [pscustomobject]@{ TargetName = $Iqn } }
}

# A healthy, fully configured Windows Server with no sessions.
function Reset-TestHost {
    $script:hostLines = New-Object 'System.Collections.Generic.List[string]'
    $script:nativeCalls = New-Object 'System.Collections.Generic.List[object]'
    $script:elevated = $true
    $script:productType = 3
    $script:volumes = @(New-TestVolume 'vol1' 'iqn.2023-01.net.windows.core.blob.elasticsan.es-test:vol1')
    $script:service = [pscustomobject]@{ Status = 'Running'; StartType = 'Automatic' }
    $script:serviceStarts = $true
    $script:serverFeature = 'Installed'
    $script:serverRestart = 'No'
    $script:clientFeature = 'Enabled'
    $script:clientFeatureThrows = $false
    $script:clientRestart = $false
    $script:claim = $true
    $script:claimSticks = $true
    $script:policy = 'RR'
    $script:diskTimeout = 30
    $script:initiatorKeys = @('Microsoft.PowerShell.Core\Registry::HKEY_LOCAL_MACHINE\TEST\Class\0004')
    $script:registry = @{
        MaxTransferLength = 262144; MaxBurstLength = 262144; FirstBurstLength = 262144; MaxRecvDataSegmentLength = 262144
        InitialR2T = 0; ImmediateData = 1; WMIRequestTimeout = 30; LinkDownTime = 30
    }
    $script:sessions = @()
    $script:persistent = @()
    $script:persistentError = $false
    $script:maxNewSessions = [int]::MaxValue
    $script:createdSessions = 0
    $script:createLiveSessions = $true
    $script:newSessionDigest = $true
}

function Invoke-TestConnect([switch]$SkipRecommendedSettings) {
    Invoke-EsanConnect -ResourceGroupName 'rg' -ElasticSanName 'san' -VolumeGroupName 'vg' -VolumeName @($script:volumes | ForEach-Object { $_.VolumeName }) -SkipRecommendedSettings:$SkipRecommendedSettings
}

function Get-TestOutput { $script:hostLines -join "`n" }
function Get-TestLogins { @($script:nativeCalls | Where-Object { $_[0] -eq 'PersistentLoginTarget' }) }

Describe 'connect.ps1 contract' {
    It 'never calls exit and keeps the original parameters' {
        $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.ExitStatementAst] }, $true).Count | Should Be 0
        $parameters = (Get-Command $connectPath).Parameters
        $position = 0
        foreach ($name in 'ResourceGroupName', 'ElasticSanName', 'VolumeGroupName', 'VolumeName') {
            $parameters[$name].ParameterSets['__AllParameterSets'].IsMandatory | Should Be $true
            $parameters[$name].ParameterSets['__AllParameterSets'].Position | Should Be $position
            $position++
        }
        $parameters['VolumeName'].ParameterType | Should Be ([string[]])
        $numSession = $parameters['NumSession']
        $numSession.ParameterSets['__AllParameterSets'].Position | Should Be 4
        $range = $numSession.Attributes | Where-Object { $_ -is [System.Management.Automation.ValidateRangeAttribute] }
        "$($range.MinRange)-$($range.MaxRange)" | Should Be '1-32'
        $parameters['SkipRecommendedSettings'].SwitchParameter | Should Be $true
    }

    It 'passes every script parameter to the single entry call' {
        $entry = @($ast.EndBlock.Statements)[-1].PipelineElements[0]
        $entry.GetCommandName() | Should Be 'Invoke-EsanConnect'
        $passed = @($entry.CommandElements | Where-Object { $_ -is [System.Management.Automation.Language.CommandParameterAst] } | ForEach-Object { $_.ParameterName }) | Sort-Object
        $declared = @($ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath }) | Sort-Object
        $passed -join ',' | Should Be ($declared -join ',')
    }

    It 'documents -SkipRecommendedSettings in comment-based help' {
        $help = Get-Help $connectPath -Full
        $parameter = $help.parameters.parameter | Where-Object { $_.name -eq 'SkipRecommendedSettings' }
        ($parameter.description | Out-String) | Should Match 'Opt out of the recommended client settings'
    }
}

Describe 'Find-EsanIscsiInitiatorKey' {
    # Fake class subkeys: name -> registry values. A name without an entry is unreadable, like Properties.
    BeforeEach {
        $script:hostLines = New-Object 'System.Collections.Generic.List[string]'
        $script:classKeyNames = @()
        $script:classKeys = @{}
        Mock Write-Host { $script:hostLines.Add("$Object") }
        Mock Get-ChildItem { foreach ($name in $script:classKeyNames) { [pscustomobject]@{ PSPath = "Registry::HKLM\Class\$name" } } }
        Mock Get-ItemProperty {
            if ($LiteralPath -like '*\Parameters') { return New-Object psobject -Property @{ MaxTransferLength = 65536 } }
            $values = $script:classKeys[($LiteralPath -split '\\')[-1]]
            if ($values) { New-Object psobject -Property $values }
        }
        Mock New-ItemProperty {}
    }

    It 'selects the initiator by MatchingDeviceId when DriverDesc is localized, ignoring unreadable subkeys' {
        $script:classKeyNames = @('Properties', '0000', '0003')
        $script:classKeys = @{
            '0000' = @{ DriverDesc = 'Storage Spaces Controller'; MatchingDeviceId = 'root\spaceport' }
            '0003' = @{ DriverDesc = 'Initiateur iSCSI Microsoft'; MatchingDeviceId = 'ROOT\ISCSIPRT' }
        }
        @(Find-EsanIscsiInitiatorKey) -join ',' | Should Be 'Registry::HKLM\Class\0003'
    }

    It 'selects the initiator by DriverDesc when MatchingDeviceId is missing' {
        $script:classKeyNames = @('0000', '0007')
        $script:classKeys = @{
            '0000' = @{ DriverDesc = 'Storage Spaces Controller' }
            '0007' = @{ DriverDesc = 'Microsoft iSCSI Initiator' }
        }
        @(Find-EsanIscsiInitiatorKey) -join ',' | Should Be 'Registry::HKLM\Class\0007'
    }

    It 'collapses duplicate matches of one key and applies the registry values to it' {
        $script:classKeyNames = @('0004', '0004')
        $script:classKeys = @{ '0004' = @{ DriverDesc = 'Microsoft iSCSI Initiator'; MatchingDeviceId = 'root\iscsiprt' } }
        @(Set-EsanRecommendedSettings -MpioActive $false).Count | Should Be 8
        Assert-MockCalled New-ItemProperty -Times 8 -Exactly -Scope It -ParameterFilter { $LiteralPath -eq 'Registry::HKLM\Class\0004\Parameters' }
    }

    It 'warns and skips the registry values when two different keys match' {
        $script:classKeyNames = @('0002', '0004')
        $script:classKeys = @{
            '0002' = @{ DriverDesc = 'Initiateur iSCSI Microsoft'; MatchingDeviceId = 'ROOT\ISCSIPRT' }
            '0004' = @{ DriverDesc = 'Microsoft iSCSI Initiator' }
        }
        @(Set-EsanRecommendedSettings -MpioActive $false).Count | Should Be 0
        $script:hostLines -join "`n" | Should Match 'Warning: found 2 iSCSI initiator registry instances instead of 1\. Skipped'
        Assert-MockCalled New-ItemProperty -Times 0 -Exactly -Scope It
    }
}

Describe 'Hardened default flow' {
    BeforeEach {
        Reset-TestHost
        Mock Write-Host { $script:hostLines.Add("$Object") }
        Mock Test-EsanAdministrator { $script:elevated }
        Mock Get-EsanVolumeData { $script:volumes }
        Mock Get-CimInstance { [pscustomobject]@{ ProductType = $script:productType } } -ParameterFilter { $ClassName -eq 'Win32_OperatingSystem' }
        Mock Get-CimInstance {
            if ($script:persistentError) { throw 'Generic failure' }
            $script:persistent
        } -ParameterFilter { $ClassName -eq 'MSiSCSIInitiator_PersistentLoginClass' -and $Namespace -eq 'root\wmi' }
        Mock Get-Service { $script:service }
        Mock Set-Service { $script:service.StartType = $StartupType }
        Mock Start-Service { if ($script:serviceStarts) { $script:service.Status = 'Running' } }
        Mock Get-WindowsFeature { [pscustomobject]@{ Name = 'Multipath-IO'; InstallState = $script:serverFeature } }
        Mock Install-WindowsFeature {
            $script:serverFeature = if ($script:serverRestart -eq 'No') { 'Installed' } else { 'InstallPending' }
            [pscustomobject]@{ Success = $true; RestartNeeded = $script:serverRestart; ExitCode = 'Success' }
        }
        Mock Get-WindowsOptionalFeature {
            if ($script:clientFeatureThrows) { throw 'Feature name MultiPathIO is unknown.' }
            if ($script:clientFeature) { [pscustomobject]@{ FeatureName = 'MultiPathIO'; State = $script:clientFeature } }
        }
        Mock Enable-WindowsOptionalFeature {
            $script:clientFeature = if ($script:clientRestart) { 'EnablePending' } else { 'Enabled' }
            [pscustomobject]@{ RestartNeeded = $script:clientRestart }
        }
        Mock Get-MSDSMAutomaticClaimSettings { @{ iSCSI = $script:claim; SAS = $false } }
        Mock Enable-MSDSMAutomaticClaim { if ($script:claimSticks) { $script:claim = $true } }
        Mock Get-MSDSMGlobalDefaultLoadBalancePolicy { $script:policy }
        Mock Set-MSDSMGlobalDefaultLoadBalancePolicy { $script:policy = $Policy }
        Mock Get-MPIOSetting { [pscustomobject]@{ DiskTimeoutValue = $script:diskTimeout } }
        Mock Set-MPIOSetting { $script:diskTimeout = $NewDiskTimeout }
        Mock Find-EsanIscsiInitiatorKey { $script:initiatorKeys }
        Mock Get-ItemProperty { New-Object psobject -Property $script:registry }
        Mock New-ItemProperty { $script:registry[$Name] = $Value }
        Mock Get-IscsiSession { $script:sessions }
    }

    Context 'Preconditions' {
        It 'stops before any Azure or host call when the session is not elevated' {
            $script:elevated = $false
            { Invoke-TestConnect } | Should Throw 'elevated PowerShell session'
            Assert-MockCalled Get-EsanVolumeData -Times 0 -Exactly -Scope It
            Assert-MockCalled Get-CimInstance -Times 0 -Exactly -Scope It
            Assert-MockCalled Get-Service -Times 0 -Exactly -Scope It
            $script:nativeCalls.Count | Should Be 0
        }

        It 'rejects unknown Windows product types' {
            $script:productType = 4
            { Invoke-TestConnect } | Should Throw 'Unsupported Windows product type'
            Assert-MockCalled Get-EsanVolumeData -Times 0 -Exactly -Scope It
        }

        It 'enables MPIO with the optional-feature cmdlets on Windows 10/11' {
            $script:productType = 1
            $script:clientFeature = 'Disabled'
            Invoke-TestConnect
            Assert-MockCalled Enable-WindowsOptionalFeature -Times 1 -Exactly -Scope It -ParameterFilter { $Online -and $NoRestart -and $FeatureName -eq 'MultiPathIO' }
            Assert-MockCalled Get-WindowsFeature -Times 0 -Exactly -Scope It
            Assert-MockCalled Install-WindowsFeature -Times 0 -Exactly -Scope It
            @(Get-TestLogins).Count | Should Be 32
        }

        It 'installs MPIO with the server-feature cmdlets on Windows Server and connects when no restart is needed' {
            $script:serverFeature = 'Available'
            Invoke-TestConnect
            Assert-MockCalled Install-WindowsFeature -Times 1 -Exactly -Scope It -ParameterFilter { $Name -eq 'Multipath-IO' }
            Assert-MockCalled Get-WindowsOptionalFeature -Times 0 -Exactly -Scope It
            Assert-MockCalled Enable-WindowsOptionalFeature -Times 0 -Exactly -Scope It
            @(Get-TestLogins).Count | Should Be 32
        }

        It 'stops for a reboot before any session work when enabling MPIO needs a restart' {
            foreach ($case in @(@{ ProductType = 1; Restart = $true }, @{ ProductType = 3; Restart = 'Yes' }, @{ ProductType = 3; Restart = 'Maybe' })) {
                Reset-TestHost
                $script:productType = $case.ProductType
                if ($case.ProductType -eq 1) {
                    $script:clientFeature = 'Disabled'
                    $script:clientRestart = $case.Restart
                } else {
                    $script:serverFeature = 'Available'
                    $script:serverRestart = $case.Restart
                }
                $script:claim = $false
                $script:registry.MaxTransferLength = 65536
                { Invoke-TestConnect } | Should Throw 'Reboot the VM, then re-run this script'
                $script:nativeCalls.Count | Should Be 0
                $script:claim | Should Be $false
                $script:registry.MaxTransferLength | Should Be 262144
            }
            Assert-MockCalled Enable-MSDSMAutomaticClaim -Times 0 -Exactly -Scope It
            Assert-MockCalled Get-IscsiSession -Times 0 -Exactly -Scope It
        }

        It 'requires -NumSession 1 when Multipath I/O is not available' {
            $script:productType = 1
            $script:clientFeature = $null
            { Invoke-TestConnect } | Should Throw '-NumSession 1'
            $script:nativeCalls.Count | Should Be 0
        }

        It 'connects one session per volume without Multipath I/O and warns in validation' {
            $script:productType = 1
            $script:clientFeatureThrows = $true
            $script:volumes = @(New-TestVolume 'vol1' 'iqn.test:vol1' 1)
            Invoke-TestConnect
            @(Get-TestLogins).Count | Should Be 1
            Assert-MockCalled Get-MSDSMAutomaticClaimSettings -Times 0 -Exactly -Scope It
            Assert-MockCalled Set-MPIOSetting -Times 0 -Exactly -Scope It
            Get-TestOutput | Should Match '\[WARN\] Multipath I/O: Unavailable'
        }

        It 'sets MSiSCSI to start automatically, starts it, and stops if it does not run' {
            $script:service = [pscustomobject]@{ Status = 'Stopped'; StartType = 'Manual' }
            Invoke-TestConnect
            Assert-MockCalled Set-Service -Times 1 -Exactly -Scope It -ParameterFilter { $Name -eq 'MSiSCSI' -and $StartupType -eq 'Automatic' }
            Assert-MockCalled Start-Service -Times 1 -Exactly -Scope It -ParameterFilter { $Name -eq 'MSiSCSI' }

            Reset-TestHost
            $script:service = [pscustomobject]@{ Status = 'Stopped'; StartType = 'Automatic' }
            $script:serviceStarts = $false
            { Invoke-TestConnect } | Should Throw 'MSiSCSI) is not running'
            $script:nativeCalls.Count | Should Be 0
        }

        It 'enables MSDSM iSCSI claiming and stops if it does not take effect' {
            $script:claim = $false
            Invoke-TestConnect
            Assert-MockCalled Enable-MSDSMAutomaticClaim -Times 1 -Exactly -Scope It -ParameterFilter { $BusType -eq 'iSCSI' }

            Reset-TestHost
            $script:claim = $false
            $script:claimSticks = $false
            { Invoke-TestConnect } | Should Throw 'MSDSM still does'
            $script:nativeCalls.Count | Should Be 0
        }
    }

    Context 'Recommended settings' {
        It 'changes only values that differ and asks for a reboot for them' {
            $script:policy = 'None'
            $script:diskTimeout = 60
            $script:registry.MaxTransferLength = 65536
            $script:registry.Remove('LinkDownTime')
            Invoke-TestConnect
            Assert-MockCalled Set-MSDSMGlobalDefaultLoadBalancePolicy -Times 1 -Exactly -Scope It -ParameterFilter { $Policy -eq 'RR' }
            Assert-MockCalled Set-MPIOSetting -Times 1 -Exactly -Scope It -ParameterFilter { $NewDiskTimeout -eq 30 }
            Assert-MockCalled New-ItemProperty -Times 2 -Exactly -Scope It
            Assert-MockCalled New-ItemProperty -Times 1 -Exactly -Scope It -ParameterFilter {
                $Name -eq 'MaxTransferLength' -and $Value -eq 262144 -and $PropertyType -eq 'DWord' -and $LiteralPath -like '*\0004\Parameters'
            }
            $script:hostLines[-1] | Should Match '^Reboot required: changed MPIO disk timeout, MaxTransferLength, LinkDownTime, which take effect only after the VM restarts\.$'
        }

        It 'changes nothing and prints no reboot notice when every value already matches' {
            Invoke-TestConnect
            Assert-MockCalled Set-MSDSMGlobalDefaultLoadBalancePolicy -Times 0 -Exactly -Scope It
            Assert-MockCalled Set-MPIOSetting -Times 0 -Exactly -Scope It
            Assert-MockCalled New-ItemProperty -Times 0 -Exactly -Scope It
            Get-TestOutput | Should Not Match 'Reboot required'
        }

        It 'leaves settings alone and skips their validation with -SkipRecommendedSettings' {
            $script:policy = 'None'
            $script:diskTimeout = 60
            $script:registry.MaxTransferLength = 65536
            Invoke-TestConnect -SkipRecommendedSettings
            Assert-MockCalled Set-MSDSMGlobalDefaultLoadBalancePolicy -Times 0 -Exactly -Scope It
            Assert-MockCalled Set-MPIOSetting -Times 0 -Exactly -Scope It
            Assert-MockCalled New-ItemProperty -Times 0 -Exactly -Scope It
            Assert-MockCalled Find-EsanIscsiInitiatorKey -Times 0 -Exactly -Scope It
            Get-TestOutput | Should Not Match 'load balance policy|disk timeout|registry'
            @(Get-TestLogins).Count | Should Be 32
        }

        It 'skips registry values with a warning unless exactly one iSCSI initiator instance is found' {
            foreach ($keys in @(@(), @('Registry::HKLM\Class\0001', 'Registry::HKLM\Class\0004'))) {
                Reset-TestHost
                $script:initiatorKeys = $keys
                $script:registry.MaxTransferLength = 65536
                Invoke-TestConnect
                Get-TestOutput | Should Match "Warning: found $($keys.Count) iSCSI initiator registry instances instead of 1"
                Get-TestOutput | Should Match '\[WARN\] iSCSI initiator registry'
            }
            Assert-MockCalled New-ItemProperty -Times 0 -Exactly -Scope It
        }
    }

    Context 'Existing sessions and persistent logins' {
        It 'connects only volumes without sessions or persistent logins and explains every skip' {
            $script:volumes = @(
                New-TestVolume 'v-new' 'iqn.test:v-new' 4
                New-TestVolume 'v-conn' 'iqn.test:v-conn' 4
                New-TestVolume 'v-pers' 'iqn.test:v-pers' 4
                New-TestVolume 'v-live' 'iqn.test:v-live' 4
            )
            Add-TestState 'iqn.test:v-conn' 4 4
            Add-TestState 'IQN.TEST:V-PERS' 0 4
            Add-TestState 'iqn.test:v-live' 4 0 -LivePersistent $false
            Invoke-TestConnect
            @(Get-TestLogins).Count | Should Be 4
            @(Get-TestLogins | Where-Object { $_[1] -ne 'iqn.test:v-new' }).Count | Should Be 0
            @($script:nativeCalls | Where-Object { $_[0] -eq 'AddTarget' }).Count | Should Be 1
            $output = Get-TestOutput
            $output | Should Match 'v-conn \[iqn\.test:v-conn\]: Skipped: already connected \(4 live / 4 persistent\)'
            $output | Should Match 'v-pers \[iqn\.test:v-pers\]: Warning: persistent configuration exists but no live sessions\. Reboot the VM .* run disconnect\.ps1'
            $output | Should Match 'v-live \[iqn\.test:v-live\]: Skipped: already connected \(4 live / 0 persistent\)'
            $output | Should Match 'v-live \[iqn\.test:v-live\]: Warning: the live sessions are not persistent'
            $output | Should Match 'Validation: \d+ passed, \d+ warnings, 0 failed'
        }

        It 'matches IQNs exactly, so vol10 does not hide vol1' {
            $script:volumes = @(New-TestVolume 'vol1' 'iqn.test:vol1' 2; New-TestVolume 'vol10' 'iqn.test:vol10' 2)
            Add-TestState 'iqn.test:vol10' 2 2
            Invoke-TestConnect
            @(Get-TestLogins | Where-Object { $_[1] -eq 'iqn.test:vol1' }).Count | Should Be 2
            @(Get-TestLogins | Where-Object { $_[1] -eq 'iqn.test:vol10' }).Count | Should Be 0
        }

        It 'connects a volume selected more than once only once' {
            # The lookup mock returns one entry per selection, as Get-AzElasticSanVolume would.
            $script:volumes = @(New-TestVolume 'vol1' 'iqn.test:vol1' 4; New-TestVolume 'VOL1' 'IQN.TEST:VOL1' 4)
            Invoke-EsanConnect -ResourceGroupName 'rg' -ElasticSanName 'san' -VolumeGroupName 'vg' -VolumeName 'vol1', 'VOL1', 'vol1'
            Assert-MockCalled Get-EsanVolumeData -Times 1 -Exactly -Scope It -ParameterFilter { ($VolumeName -join ',') -ceq 'vol1' }
            @($script:nativeCalls | Where-Object { $_[0] -eq 'AddTarget' }).Count | Should Be 1
            @(Get-TestLogins).Count | Should Be 4
            @($script:hostLines | Where-Object { $_ -like '*persistent logins: *' }).Count | Should Be 1
            $script:hostLines[-1] | Should Be 'Validation: 9 passed, 0 warnings, 0 failed'
        }

        It 'fails closed before any change when persistent logins cannot be read' {
            $script:persistentError = $true
            { Invoke-TestConnect } | Should Throw 'Could not read the live iSCSI sessions or persistent logins'
            $script:nativeCalls.Count | Should Be 0
        }

        It 'stops before any change when the batch would exceed 256 persistent logins' {
            $script:volumes = @(New-TestVolume 'a' 'iqn.test:a' 16; New-TestVolume 'b' 'iqn.test:b' 16)
            Add-TestState 'iqn.test:other' 0 225
            { Invoke-TestConnect } | Should Throw '(225 existing + 32 new), above the Windows limit of 256'
            $script:nativeCalls.Count | Should Be 0

            Reset-TestHost
            $script:volumes = @(New-TestVolume 'a' 'iqn.test:a' 16; New-TestVolume 'b' 'iqn.test:b' 16)
            Add-TestState 'iqn.test:other' 0 224
            Invoke-TestConnect
            @(Get-TestLogins).Count | Should Be 32
        }
    }

    Context 'Connect and validate' {
        It 'keeps the existing iscsicli command lines and never adds a separate LoginTarget' {
            $script:volumes = @(New-TestVolume 'vol1' 'iqn.test:Vol1' 2 'Portal.Example.Net' 3260)
            Invoke-TestConnect
            $script:nativeCalls.Count | Should Be 3
            $script:nativeCalls[0] -join ' ' | Should BeExactly 'AddTarget iqn.test:Vol1 * Portal.Example.Net 3260 * 0 * * * * * * * * * 0'
            foreach ($call in $script:nativeCalls[1..2]) {
                $call -join ' ' | Should BeExactly 'PersistentLoginTarget iqn.test:vol1 t portal.example.net 3260 Root\ISCSIPRT\0000_0 -1 * 0x00000002 1 1 * * * * * * * 0'
            }
        }

        It 'passes validation and prints the summary when everything is configured' {
            Invoke-TestConnect
            $output = Get-TestOutput
            foreach ($check in 'iSCSI service', 'Multipath I/O', 'MSDSM iSCSI claim', 'MSDSM load balance policy', 'MPIO disk timeout', 'iSCSI initiator registry') {
                $output | Should Match "\[PASS\] $check`:"
            }
            $output | Should Match '\[PASS\] vol1 \[\S+\] live sessions: 32 of 32 requested'
            $output | Should Match '\[PASS\] vol1 \[\S+\] persistent logins: 32 of 32 requested'
            $output | Should Match '\[PASS\] vol1 \[\S+\] session state'
            $script:hostLines[-1] | Should Be 'Validation: 9 passed, 0 warnings, 0 failed'
        }

        It 'fails when a volume connected in this run is short of persistent logins or digests' {
            $script:maxNewSessions = 30
            { Invoke-TestConnect } | Should Throw 'Validation failed: 1 check(s) failed'
            Get-TestOutput | Should Match '\[FAIL\] vol1 \[\S+\] persistent logins: 30 of 32 requested'
            Get-TestOutput | Should Match '\[WARN\] vol1 \[\S+\] live sessions: 30 of 32 requested; the saved persistent logins establish the rest at boot'

            Reset-TestHost
            $script:newSessionDigest = $false
            { Invoke-TestConnect } | Should Throw 'Validation failed: 1 check(s) failed'
            Get-TestOutput | Should Match '\[FAIL\] vol1 \[\S+\] session state: 32 of 32 sessions'
        }

        It 'treats saved persistent logins without live sessions as pending until the reboot' {
            $script:createLiveSessions = $false
            Invoke-TestConnect
            $output = Get-TestOutput
            $output | Should Match '\[WARN\] vol1 \[\S+\] live sessions: 0 of 32 requested; the saved persistent logins establish the rest at boot'
            $output | Should Match '\[PASS\] vol1 \[\S+\] persistent logins: 32 of 32 requested'
            $output | Should Not Match 'session state'
            $script:hostLines[-1] | Should Be 'Reboot required: the saved persistent logins for vol1 establish their sessions at boot.'
        }

        It 'reports pre-existing mismatches and extra sessions on skipped volumes as warnings only' {
            $script:volumes = @(New-TestVolume 'v-old' 'iqn.test:v-old' 32; New-TestVolume 'v-extra' 'iqn.test:v-extra' 32)
            Add-TestState 'iqn.test:v-old' 8 8
            Add-TestState 'iqn.test:v-extra' 40 40
            Invoke-TestConnect
            $script:nativeCalls.Count | Should Be 0
            $output = Get-TestOutput
            $output | Should Match '\[WARN\] v-old \[\S+\] live sessions: 8 of 32 requested'
            $output | Should Match '\[WARN\] v-extra \[\S+\] persistent logins: 40 of 32 requested'
            $script:hostLines[-1] | Should Match 'Validation: \d+ passed, 4 warnings, 0 failed'
        }
    }

    Context 'Zonal and VIP modes: entry routing' {
        It 'routes both switches through the shared steps and validates the IQN each mode connected' {
            $script:modeCalls = New-Object 'System.Collections.Generic.List[object]'
            Mock Connect-ElasticSanVolumes {
                $script:modeCalls.Add([pscustomobject]@{ Count = $SessionCount; Zonal = [bool]$ZonalAffinity; Vip = [bool]$VipDistribution; Edition = $Edition; Skip = [bool]$SkipRecommendedSettings })
                $iqn = if ($ZonalAffinity) { 'iqn.test:one:az-eastus-az3' } else { 'iqn.test:one' }
                Add-TestState $iqn 0 $SessionCount
                [pscustomobject]@{ Plans = @([pscustomobject]@{ Name = 'one'; Iqn = $iqn; Skip = $false }); RebootChanges = @(if (!$SkipRecommendedSettings) { 'MaxTransferLength' }) }
            }
            Invoke-EsanConnect -ResourceGroupName 'rg' -ElasticSanName 'san' -VolumeGroupName 'vg' -VolumeName 'one' -EnableZonalAffinity -EnableVipDistribution -SkipRecommendedSettings
            Invoke-EsanConnect -ResourceGroupName 'rg' -ElasticSanName 'san' -VolumeGroupName 'vg' -VolumeName 'one' -EnableVipDistribution -NumSession 4
            Assert-MockCalled Get-EsanVolumeData -Times 0 -Exactly -Scope It
            ($script:modeCalls | ForEach-Object { '{0}/{1}/{2}/{3}/{4}' -f $_.Count, $_.Zonal, $_.Vip, $_.Edition, $_.Skip }) -join ' ' |
                Should Be '32/True/True/Server/True 4/False/True/Server/False'
            $output = Get-TestOutput
            $output | Should Match '\[PASS\] one \[iqn\.test:one:az-eastus-az3\] persistent logins: 32 of 32 requested'
            $output | Should Match '\[WARN\] one \[iqn\.test:one:az-eastus-az3\] live sessions: 0 of 32 requested'
            $output | Should Match '\[PASS\] one \[iqn\.test:one\] persistent logins: 4 of 4 requested'
            # The zonal/VIP path announces the reboot for its saved logins itself, so the shared line
            # only reports setting changes.
            $output | Should Not Match 'saved persistent logins for one'
            $script:hostLines[-1] | Should Be 'Reboot required: changed MaxTransferLength, which take effect only after the VM restarts.'
        }

        It 'passes a volume selected more than once to the zonal and VIP path once' {
            Mock Connect-ElasticSanVolumes {
                $script:modeNames = $Names -join ','
                [pscustomobject]@{ Plans = @(); RebootChanges = @() }
            }
            Invoke-EsanConnect -ResourceGroupName 'rg' -ElasticSanName 'san' -VolumeGroupName 'vg' -VolumeName 'one', 'ONE', 'two', 'one' -EnableVipDistribution
            $script:modeNames | Should Be 'one,two'
        }
    }

    # Mocks created inside an It last until the end of its Context in Pester 3.4, so the real
    # Connect-ElasticSanVolumes is exercised in a separate Context.
    Context 'Zonal and VIP modes: host preparation' {
        BeforeEach {
            $script:order = New-Object 'System.Collections.Generic.List[string]'
            Mock Get-ZonalComputeMetadata { throw 'IMDS must be mocked.' }
            Mock Get-VipHostAddresses { throw 'DNS must be mocked.' }
            Mock Get-AzContext { [pscustomobject]@{ Subscription = [pscustomobject]@{ Id = 'abcdef01-2345-6789-abcd-0123456789ab' } } }
            Mock Resolve-ZonalPhysicalZone { $script:order.Add('lookup'); 'eastus-az3' }
            Mock Resolve-VipPortals { @('10.0.0.1', '10.0.0.2') }
            Mock Invoke-ZonalProviderRequest {
                $script:order.Add('lookup')
                if ($Command -eq 'Get-AzElasticSanVolume') {
                    [pscustomobject]@{ StorageTargetIqn = 'iqn.test:one'; StorageTargetPortalHostname = 'one.example.net'; StorageTargetPortalPort = 3260 }
                }
            }
            Mock Get-ZonalPersistentTargets { $script:order.Add('inventory') }
            Mock Invoke-ZonalIscsiCli { $script:order.Add($Arguments[0]) }
        }

        It 'prepares the host after the read-only lookup and before reading existing state or changing anything' {
            Mock Initialize-EsanHost { $script:order.Add('prepare'); 'MaxTransferLength' }
            Mock Get-IscsiSession { $script:order.Add('inventory') }
            $result = Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one') 2 -ZonalAffinity -Edition 'Client' -SkipRecommendedSettings | Select-Object -Last 1
            @($script:order | Select-Object -Unique) -join ',' | Should Be 'lookup,prepare,inventory,AddTarget,PersistentLoginTarget'
            Assert-MockCalled Initialize-EsanHost -Times 1 -Exactly -Scope It -ParameterFilter { $Edition -eq 'Client' -and $SessionCount -eq 2 -and $SkipRecommendedSettings }
            $result.Plans[0].Iqn | Should Be 'iqn.test:one:az-eastus-az3'
            @($result.RebootChanges) -join ',' | Should Be 'MaxTransferLength'

            $script:order.Clear()
            Mock Initialize-EsanHost { $script:order.Add('prepare'); throw 'Reboot the VM, then re-run this script' }
            { Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one') 2 -VipDistribution } | Should Throw 'Reboot the VM'
            @($script:order | Select-Object -Unique) -join ',' | Should Be 'lookup,prepare'
        }

        It 'enforces the 256-login limit before any change' {
            Mock Initialize-EsanHost {}
            Mock Get-ZonalPersistentTargets { foreach ($i in 1..225) { 'iqn.test:other' } }
            { Connect-ElasticSanVolumes 'rg' 'san' 'vg' @('one') 32 -VipDistribution } | Should Throw '(225 existing + 32 new), above the Windows limit of 256'
            Assert-MockCalled Invoke-ZonalIscsiCli -Times 0 -Exactly -Scope It
        }
    }
}
