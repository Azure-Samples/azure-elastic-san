# Windows standalone connection scripts

`connect.ps1` uses the existing authenticated Azure PowerShell context; it does
not log in, change subscriptions, install modules or require Azure CLI.
`-NumSession` accepts 1-32 in every mode and defaults to 32.

| Switches | Target IQN | Original login portal |
| --- | --- | --- |
| Neither | Plain | FQDN |
| `-EnableZonalAffinity` | `:az-<physicalZone>` suffix | FQDN |
| `-EnableVipDistribution` | Plain; no IMDS or zone mapping | IPv4 VIPs, or FQDN for one address |
| Both | `:az-<physicalZone>` suffix | IPv4 VIPs, or FQDN for one address |

```powershell
.\connect.ps1 -ResourceGroupName rg -ElasticSanName san -VolumeGroupName vg `
    -VolumeName volume1,volume2 -NumSession 32 -EnableVipDistribution
```

Add `-EnableZonalAffinity` independently when zonal IQN routing is wanted and
supported by the front end. Mapping retains subscription/region checks, bounded
IMDS/provider reads and whole-batch decorated-IQN validation from ADO 39689651.

## Host preparation, validation and reboot (every mode)

Every mode first requires an elevated session and detects the Windows edition.
After the read-only volume lookup, it prepares the host: MSiSCSI starts
automatically and is running, Multipath I/O is enabled (`Enable-WindowsOptionalFeature`
on Windows 10/11, `Install-WindowsFeature` on Windows Server) and MSDSM claims
iSCSI disks. Unless `-SkipRecommendedSettings` is set, it also applies the
[recommended client settings](https://learn.microsoft.com/azure/storage/elastic-san/elastic-san-best-practices)
(round robin, 30-second MPIO disk timeout, iSCSI initiator registry values),
changing only values that differ. If enabling MPIO needs a restart, the script
stops before any iSCSI change; reboot and re-run it.

After connecting, a read-only validation prints `[PASS]`, `[WARN]` or `[FAIL]`
per check, then `Validation: N passed, N warnings, N failed`. It checks the
target each mode connected (the decorated IQN in zonal mode). Missing persistent
logins on a volume connected in this run fail; sessions that aren't live yet
only warn, and the final `Reboot required:` line names them. Every stop and any
FAIL ends the script with an error (non-zero exit code with `powershell.exe
-File`). `Get-Help .\connect.ps1 -Full` describes each step.

## VIP distribution and reboot

VIP mode resolves the volume FQDN with `System.Net.Dns.GetHostAddresses`, keeps
IPv4 only, removes duplicates and sorts numerically. No IPv4 addresses is a
clear DNS error before any iSCSI change. One distinct IPv4 keeps the **FQDN**
for LRS, private endpoints and migrations. With two or more addresses, session
`i` uses `VIP[i % N]`: 32 requests over three VIPs give 11/11/10 original
portals, and over two give 16/16. Only portals used by the requested session
count are registered.

The script calls `AddTarget` once per used portal and saves each login with
`PersistentLoginTarget`, preserving the multipath and digest options.
**Reboot is required to establish the saved logins.** It does not poll for
live sessions after saving them.

Client pinning spreads saved original portals across the returned VIPs so they
do not all depend on a single zone's portal. It complements the front end's
ISID-hash redirect; the front end may redirect after login. Original portal
distribution is not a guarantee or verification of final session placement.

## Existing configuration and failures

With either opt-in, any live session or persistent login for the volume's plain
IQN or `<plain IQN>:az-*` causes a skip:
`already configured; run disconnect.ps1 first to change the layout`.
No layout proof, automatic repair, rebalance or disconnect is attempted.
All selected-volume discovery and DNS checks finish before configuration
changes. The script also stops before any change if the new logins would take
the initiator past the Windows limit of 256 persistent logins.

Preflight is not a transaction. A native failure can leave saved entries for
this or earlier volumes. Inspect the selected targets before retrying, and do
not run concurrent connection tools against them.

## Disconnect

`disconnect.ps1` needs no zonal flag. After confirmation, it logs out every
live session matching a selected volume's plain IQN or `<plain IQN>:az-`
prefix (case-insensitive), removes each matching persistent login using its
recorded portal address and port, then removes each matching target name.
FQDN, IPv4 and IPv6 portals are supported, including persistent-only state
before reboot. The existing `ROOT\ISCSIPRT\0000_0` initiator and any-port
selection are retained. Similar-prefix unrelated targets are left alone.

Persistent inventory is parsed from English `iscsicli ListPersistentTargets`
output (`Total of ... persistent targets`, `Target Name`, `Address and Socket`).
Missing/malformed inventory or native command failure stops cleanup explicitly.
Tests cover the assumed text format only; real Windows output and native cleanup
still require authorized qualification. Cleanup is not transactional, so a
failure can leave some sessions or saved logins in place.

## Tests and limits

Run from this directory with Windows PowerShell 5.1 and Pester 3.4 (the version
that ships with Windows). Run each file in its own `Invoke-Pester` call: the
files define overlapping command stubs, so one multi-file run does not isolate
them.

```powershell
$failed = 0
foreach ($file in 'connect.Tests.ps1', 'connect.Vip.Tests.ps1', 'connect.Hardening.Tests.ps1', 'disconnect.Tests.ps1') {
    $failed += (Invoke-Pester -Script ".\$file" -PassThru).FailedCount
}
if ($failed) { throw "$failed Pester test(s) failed" }
```

Tests mock Azure, IMDS, DNS and native commands, including a
`PersistentLoginTarget` that never creates a live session. Persistent inventory
parsing is tested against an **assumed English `iscsicli ListPersistentTargets`
format**, not captured host output; unreadable or incomplete listings fail
explicitly. No native/reboot, backend redirect or zone-failure qualification is
claimed. Zonal routing still requires front-end suffix support.
