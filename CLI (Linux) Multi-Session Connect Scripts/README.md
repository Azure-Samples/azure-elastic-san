# Linux standalone connection options

Use Python 3.5 or later and an authenticated Azure CLI with the `elastic-san`
extension. Run as root or as a user with passwordless sudo; the script prepares
the host itself (see below). Session count defaults
to 32; `-s` accepts smaller positive counts and caps larger values at 32.

| Options | IQN | Original portal selection |
|---|---|---|
| Neither opt-in | Plain | Volume FQDN |
| `--enable-zonal-affinity` | `:az-<physicalZone>` suffix | Volume FQDN |
| `--enable-vip-distribution` | Plain | IPv4 DNS selection below |
| Both | Decorated | IPv4 DNS selection below |

```sh
python3 connect_for_documentation.py \
  --elastic-san-subscription '<subscription>' -g rg -e san -v vg \
  -n volume1 volume2 -s 32 --enable-vip-distribution
```

Add `--enable-zonal-affinity` independently when supported by the service.
That option retains the mapping layer's all-selected-volume preflight using
IMDS and subscription/region availability-zone mappings; the VM must be zonal
and in the same subscription and region as the SAN. VIP distribution alone
does not query IMDS or zone mappings. `--subscription` remains an alias.

## VIP selection and redirects

VIP distribution resolves the FQDN with IPv4 `socket.getaddrinfo`, deduplicates
addresses, and sorts them numerically. No IPv4 answers produces a DNS error
before node creation or login. **One address keeps the original FQDN**, including
for LRS and private endpoints, so saved configuration remains migration-safe.
With **N addresses**, session shares are `count // N`, with one extra assigned
to each of the first `count % N` addresses: 32 over three gives 11/11/10; over two,
16/16. Zero-share addresses are only tried as single-session fallbacks if every
positive-share portal fails to log in.

Client pinning spreads **original saved portals**, not final session placement.
It complements the front end's ISID-hash redirect: the front end may redirect
any login, and this script neither controls nor verifies the resulting placement.
Spreading saved portals avoids depending on one zone for all initial connections;
it is not a guarantee of successful failover.

Each portal uses one seed login, SID-specific clones, CRC32C digest settings, the
recommended node values (unless `--skip-recommended-settings`), and the full
persistent session count. The seed temporarily uses `nr_sessions=1`
because native login already honors that setting. Login failures warn and allow
other portals to proceed; if a portal's first login fails and no new session for
the target appeared, the node record this run created for it is deleted so a
re-run can connect (it is kept, with a warning, if sessions can't be read). A volume that no portal
logs in to is reported and the script continues with the next volume. Clone or
persistence failures also warn, so a usable connection may have fewer sessions
or incomplete saved settings; validation reports them. Native commands use ordinary `sudo`.

## Host preparation, recommended settings and validation

Every mode runs the same steps: privilege check (root or passwordless sudo), a
distro guard from `/etc/os-release` (Debian/Ubuntu, RHEL family, SLES/openSUSE,
Azure Linux), the Azure/zone/DNS lookups for all volumes, then prerequisites:
missing `open-iscsi`/`iscsi-initiator-utils` and multipath packages are installed
non-interactively, an initiator name is generated if missing, `iscsid` and
`multipathd` are enabled and started, and the boot login unit (`open-iscsi` or
`iscsi`) is enabled but not started. On RHEL without `/etc/multipath.conf`,
`mpathconf --enable` creates the distro default.

By default the script applies the
[best-practice](https://learn.microsoft.com/azure/storage/elastic-san/elastic-san-best-practices)
node values and writes `azure-elastic-san.conf` into multipath's `config_dir`,
scoped to vendor `MSFT` / product `Virtual HD`. `/etc/multipath.conf` and global
defaults are never changed; differing `find_multipaths`, `polling_interval` or
`user_friendly_names` are reported as warnings. With `find_multipaths strict`,
only volumes connected in this run have their WWIDs registered.
`--skip-recommended-settings` skips the node values, the multipath file and WWID
registration; prerequisites and CRC32C digests are always configured.

A final validation prints `[PASS]`, `[WARN]` or `[FAIL]` lines for the host
checks `iscsid service`, `multipathd service`, `iSCSI login unit`,
`multipath drop-in` and `multipath defaults`, and for each volume as
`<volume> [<iqn>] <check>`: `live sessions`, `persistent records`
(`node.startup` and `nr_sessions`), `digests` (from the node record),
`recommended settings` and `multipath paths`. Problems on volumes connected in this run fail; pre-existing
state on skipped volumes and counts above the request only warn. Any failure,
or any stop before connecting, prints one `ERROR:` line and exits with code 1.

## Existing connections and limits

Every volume is checked before any volume changes. Any session or node record
for the plain IQN or its `:az-*` variants, on any portal and in any letter case,
skips the volume:
`Skipped: already connected (<live> live / <persistent> persistent)`. For
records without sessions, validation warns and shows the `iscsiadm ... -l`
command to log in.
Run `disconnect_for_documentation.py` first to change the layout. On skipped
volumes only differing recommended node values are updated (never digests,
session counts or startup); if sessions are live, the script asks for a
log out/in or reboot. No layout proof, repair, rebalance, or automatic cleanup is
performed. Review
warnings and use an approved disconnect/reconnect procedure; do not run competing
connection tools for the same volume.

Mocks do not establish native initiator, service redirect, reboot, or zone-failure
behavior. Zonal IQN routing requires front-end support. No cloud resources are
created by this change.

## Disconnect

Quiesce I/O and unmount the selected volumes before explicitly running
`disconnect_for_documentation.py` with its existing subscription/resource/volume
arguments. No zonal parameter is needed: it discovers both persistent node
records and live sessions, selecting the exact volume IQN and every target
starting with `<volume-IQN>:az-`. Each matching name is logged out and its node
records deleted **across all portals**, including FQDNs, VIP IPs, and old zones.
This removes automatic-startup records even when no sessions are active; other
target names are left alone. An empty inventory is harmless, while native
errors stop cleanup and can leave partial state. Inspect the selected targets
before retrying, and do not run concurrent connection tools. Turning off the
connect opt-in alone still does not clean up anything. Native disconnect and
reboot behavior require authorized Linux qualification.

## Offline tests

```powershell
python -B -W error::ResourceWarning -m unittest discover -s '.\CLI (Linux) Multi-Session Connect Scripts' -p 'test*.py'
```
