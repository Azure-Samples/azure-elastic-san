import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

try:
    from urllib.request import ProxyHandler, Request, build_opener
    from urllib.error import HTTPError, URLError
except ImportError:
    from urllib2 import HTTPError, ProxyHandler, Request, URLError, build_opener

try:
    string_types = (basestring,)
except NameError:
    string_types = (str,)

IMDS_COMPUTE_URL = "http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01"
IMDS_TIMEOUT_SECONDS = 5
AZ_CLI_TIMEOUT_SECONDS = 30
MAX_IQN_UTF8_BYTES = 223
PHYSICAL_ZONE_PATTERN = re.compile(r"^[a-z0-9.-]+\Z")
IQN_PATTERN = re.compile(r"^[a-z0-9.:-]+\Z")
SUBSCRIPTION_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class ZonalAffinityError(Exception):
    pass


class ConnectScriptError(RuntimeError):
    """A stop: __main__ prints one ERROR line and exits with code 1."""


# Host preparation and validation shared by every connect mode (default, VIP
# distribution and zonal affinity). The distro family comes from /etc/os-release.
DISTRO_FAMILIES = {
    "debian": "debian", "ubuntu": "debian",
    "rhel": "rhel", "centos": "rhel", "rocky": "rhel", "almalinux": "rhel", "ol": "rhel", "fedora": "rhel",
    "sles": "suse", "suse": "suse", "opensuse": "suse",
    "azurelinux": "azurelinux", "mariner": "azurelinux",
}
PREREQUISITE_PACKAGES = {
    "debian": ("open-iscsi", "multipath-tools"),
    "rhel": ("iscsi-initiator-utils", "device-mapper-multipath"),
    "suse": ("open-iscsi", "multipath-tools"),
    "azurelinux": ("iscsi-initiator-utils", "device-mapper-multipath"),
}
LOGIN_UNITS = ("open-iscsi", "iscsi")
ENABLED_UNIT_STATES = ("enabled", "enabled-runtime", "static", "indirect", "generated", "alias")
INITIATOR_NAME_FILE = "/etc/iscsi/initiatorname.iscsi"

# Client-side values from https://learn.microsoft.com/azure/storage/elastic-san/elastic-san-best-practices
RECOMMENDED_NODE_SETTINGS = (
    ("node.conn[0].iscsi.MaxXmitDataSegmentLength", "262144"),
    ("node.session.iscsi.MaxBurstLength", "262144"),
    ("node.session.iscsi.FirstBurstLength", "262144"),
    ("node.conn[0].iscsi.MaxRecvDataSegmentLength", "262144"),
    ("node.session.iscsi.InitialR2T", "No"),
    ("node.session.iscsi.ImmediateData", "Yes"),
    ("node.conn[0].timeo.login_timeout", "30"),
    ("node.conn[0].timeo.logout_timeout", "15"),
)
DIGEST_SETTINGS = (
    ("node.conn[0].iscsi.HeaderDigest", "CRC32C"),
    ("node.conn[0].iscsi.DataDigest", "CRC32C"),
)

MULTIPATH_CONF = "/etc/multipath.conf"
DEFAULT_MULTIPATH_CONFIG_DIR = "/etc/multipath/conf.d"
MULTIPATH_DROP_IN_NAME = "azure-elastic-san.conf"
# Device-scoped on purpose: the documented "defaults" section would change
# behavior for every other multipath device on the host.
MULTIPATH_DROP_IN = """# Written by the Azure Elastic SAN connect script (connect_for_documentation.py).
# Applies only to Elastic SAN volumes; global multipath defaults are left unchanged.
devices {
    device {
        vendor "MSFT"
        product "Virtual HD"
        path_grouping_policy "multibus"
        path_selector "round-robin 0"
        failback "immediate"
        no_path_retry 3
    }
}
"""
DOCUMENTED_MULTIPATH_DEFAULTS = (
    ("find_multipaths", "yes"),
    ("polling_interval", "5"),
    ("user_friendly_names", "yes"),
)
# Non-root PATHs often omit the sbin directories where these tools live.
TOOL_SEARCH_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
MULTIPATH_WAIT_ATTEMPTS = 10
MULTIPATH_WAIT_SECONDS = 2


def run_command(command, input_text=None):
    """Runs argv without a shell or terminal input; returns (exit code, stdout, stderr)."""
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        return 127, "", str(error)
    out, err = process.communicate(None if input_text is None else input_text.encode("utf-8"))
    return process.returncode, _decode_output(out), _decode_output(err).strip()


def find_tool(name):
    return shutil.which(name) or shutil.which(name, path=TOOL_SEARCH_PATH)


def _require_tool(name, guidance):
    if not find_tool(name):
        raise ConnectScriptError("{} was not found. {}".format(name, guidance))


def _run_required(command, action):
    code, out, err = run_command(command)
    if code != 0:
        raise ConnectScriptError("{} failed ({}): {}".format(
            action, " ".join(command), err or out.strip() or "exit code {}".format(code)
        ))
    return out


def _read_root_file(path):
    code, out, _ = run_command(["sudo", "cat", path])
    return out if code == 0 else None


def _write_root_file(path, content):
    _run_required(["sudo", "mkdir", "-p", path.rsplit("/", 1)[0]], "Creating the directory for " + path)
    code, _, err = run_command(["sudo", "tee", path], input_text=content)
    if code != 0:
        raise ConnectScriptError("Writing {} failed: {}".format(path, err or "exit code {}".format(code)))


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def check_privileges():
    # Native commands run through sudo, so root or passwordless sudo is required.
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        return
    _require_tool("sudo", "Run this script as root, or install sudo and configure passwordless sudo.")
    if run_command(["sudo", "-n", "true"])[0] != 0:
        raise ConnectScriptError(
            "This script configures iSCSI and multipath and must run as root or as a user with "
            "passwordless sudo. Configure passwordless sudo for this user (Azure CLI must be signed "
            "in as the user that runs the script), or run it as root."
        )


def read_os_release():
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(path) as handle:
                lines = handle.read().splitlines()
        except (IOError, OSError):
            continue
        values = {}
        for line in lines:
            key, separator, value = line.partition("=")
            if separator:
                values[key.strip()] = value.strip().strip("'\"")
        return values
    return {}


def detect_distro():
    os_release = read_os_release()
    distro_id = os_release.get("ID", "").lower()
    for candidate in [distro_id] + os_release.get("ID_LIKE", "").lower().split():
        if candidate in DISTRO_FAMILIES:
            return DISTRO_FAMILIES[candidate]
    raise ConnectScriptError(
        "Unsupported Linux distribution '{}'. Supported: Debian/Ubuntu, RHEL/CentOS/Rocky/AlmaLinux/"
        "Oracle Linux/Fedora, SLES/openSUSE and Azure Linux. On other distributions, install the "
        "open-iscsi and multipath tools and connect with the documented iscsiadm commands.".format(
            distro_id or "unknown"
        )
    )


def _package_installed(family, package):
    if family == "debian":
        code, out, _ = run_command(["dpkg-query", "-W", "-f=${Status}", package])
        return code == 0 and out.split() == ["install", "ok", "installed"]
    return run_command(["rpm", "-q", package])[0] == 0


def _install_command(family, packages):
    if family == "debian":
        return ["sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "-q"] + packages
    if family == "rhel":
        return ["sudo", "dnf" if find_tool("dnf") else "yum", "install", "-y"] + packages
    if family == "suse":
        return ["sudo", "zypper", "--non-interactive", "install"] + packages
    return ["sudo", "tdnf", "install", "-y"] + packages


def find_login_unit():
    """Returns the unit that logs in automatic node records at boot, if the distro has one."""
    for unit in LOGIN_UNITS:
        out = run_command(["systemctl", "list-unit-files", "--no-legend", "--no-pager", unit + ".service"])[1]
        if unit + ".service" in out.split():
            return unit
    return None


def _ensure_initiator_name():
    content = _read_root_file(INITIATOR_NAME_FILE) or ""
    if any(line.strip().startswith("InitiatorName=") and line.strip() != "InitiatorName="
           for line in content.splitlines()):
        return False
    _require_tool("iscsi-iname", "Set InitiatorName= in {} and re-run this script.".format(INITIATOR_NAME_FILE))
    name = _run_required(["sudo", "iscsi-iname"], "Generating an iSCSI initiator name").strip()
    _write_root_file(INITIATOR_NAME_FILE, "InitiatorName={}\n".format(name))
    print("Created {}".format(INITIATOR_NAME_FILE))
    return True


def ensure_prerequisites(family):
    missing = [package for package in PREREQUISITE_PACKAGES[family] if not _package_installed(family, package)]
    if missing:
        command = _install_command(family, missing)
        _require_tool("apt-get" if family == "debian" else command[1],
                      "Install {} with the distro's package manager and re-run this script.".format(" ".join(missing)))
        print("Installing missing packages: {}".format(" ".join(missing)))
        if family == "debian":
            _run_required(["sudo", "apt-get", "update", "-q"], "Refreshing package lists")
        _run_required(command, "Installing " + " ".join(missing))
    created_initiator_name = _ensure_initiator_name()
    _run_required(["sudo", "systemctl", "enable", "--now", "iscsid"], "Enabling iscsid")
    if created_initiator_name:
        # iscsid reads the initiator name only when it starts.
        _run_required(["sudo", "systemctl", "restart", "iscsid"], "Restarting iscsid")
    login_unit = find_login_unit()
    if login_unit:
        # Enable only. Starting it runs --loginall=automatic, which could log in
        # targets that are not part of this run's plan.
        _run_required(["sudo", "systemctl", "enable", login_unit], "Enabling " + login_unit)
    if family == "rhel" and _read_root_file(MULTIPATH_CONF) is None:
        # RHEL's multipathd does not start without a configuration file;
        # mpathconf writes the distro's standard minimal one.
        _require_tool("mpathconf", "Create {} (for example with 'mpathconf --enable') and re-run this script.".format(
            MULTIPATH_CONF))
        _run_required(["sudo", "mpathconf", "--enable", "--with_multipathd", "y"], "Creating " + MULTIPATH_CONF)
    _run_required(["sudo", "systemctl", "enable", "--now", "multipathd"], "Enabling multipathd")


def multipath_drop_in_path():
    config_dir = DEFAULT_MULTIPATH_CONFIG_DIR
    for line in (_read_root_file(MULTIPATH_CONF) or "").splitlines():
        words = line.split("#", 1)[0].split("!", 1)[0].split(None, 1)
        if len(words) == 2 and words[0] == "config_dir":
            config_dir = words[1].strip().strip('"')
    return config_dir.rstrip("/") + "/" + MULTIPATH_DROP_IN_NAME


def configure_multipath():
    path = multipath_drop_in_path()
    if _read_root_file(path) == MULTIPATH_DROP_IN:
        return
    _write_root_file(path, MULTIPATH_DROP_IN)
    _run_required(["sudo", "multipathd", "reconfigure"], "Reloading multipathd")
    print("Wrote Elastic SAN multipath device settings to {}".format(path))


def prepare_iscsi_host(family, apply_recommended):
    ensure_prerequisites(family)
    if apply_recommended:
        configure_multipath()


def get_multipath_defaults():
    code, out, _ = run_command(["sudo", "multipathd", "show", "config"])
    if code != 0:
        return None
    values, in_defaults = {}, False
    for line in out.splitlines():
        words = line.split()
        if words[:2] == ["defaults", "{"]:
            in_defaults = True
        elif in_defaults and words[:1] == ["}"]:
            break
        elif in_defaults and len(words) > 1:
            values[words[0]] = " ".join(words[1:]).strip('"')
    return values


def get_session_details():
    """Maps each target name (lowercase) to its attached disks and negotiated digests."""
    details, current, header_digest = {}, None, None
    for line in _iscsiadm(["-m", "session", "-P", "3"], allow_empty=True).splitlines():
        words = line.split()
        if words[:1] == ["Target:"] and len(words) > 1:
            current = details.setdefault(words[1].lower(), {"disks": [], "digests": set()})
        elif current is None:
            continue
        elif words[:1] == ["HeaderDigest:"] and len(words) > 1:
            header_digest = words[1]
        elif words[:1] == ["DataDigest:"] and len(words) > 1:
            current["digests"].add("{}/{}".format(header_digest, words[1]))
        elif words[:3] == ["Attached", "scsi", "disk"] and len(words) > 3:
            current["disks"].append(words[3])
    return details


def _volume_session_details(details, volume_iqn):
    disks, digests = set(), set()
    for name, info in details.items():
        if _belongs_to_volume(name, volume_iqn):
            disks.update(info["disks"])
            digests.update(info["digests"])
    return sorted(disks), sorted(digests)


def get_multipath_paths():
    """Maps each path device to (map name, dm state, checker state), or None if multipathd is unavailable."""
    code, out, _ = run_command(["sudo", "multipathd", "show", "paths", "raw", "format", "%d %m %t %T"])
    if code != 0:
        return None
    return {words[0]: words[1:] for words in (line.split() for line in out.splitlines()) if len(words) == 4}


def register_multipath_devices(outcomes):
    # find_multipaths "strict" only builds maps for known WWIDs. Register just the
    # volumes this run connected instead of changing the host-wide policy.
    details = get_session_details()
    for outcome in outcomes:
        disks = _volume_session_details(details, outcome.volume_iqn)[0]
        if not disks:
            continue
        code, _, err = run_command(["sudo", "multipath", "-a", "/dev/" + disks[0]])
        if code != 0:
            print("Warning: {} [{}]: could not register the multipath WWID: {}".format(
                outcome.volume_name, outcome.target_iqn, err or "exit code {}".format(code)
            ), file=sys.stderr)
    code, _, err = run_command(["sudo", "multipathd", "reconfigure"])
    if code != 0:
        print("Warning: multipathd reconfigure failed: {}".format(err or "exit code {}".format(code)), file=sys.stderr)


def _multipath_check(volume_iqn, live, details, paths):
    if live < 2:
        return True, "single session; no multipath map required"
    disks = _volume_session_details(details, volume_iqn)[0]
    if not disks:
        return False, "no SCSI disks are attached to the {} sessions".format(live)
    if paths is None:
        return False, "could not read paths from multipathd"
    maps = sorted({paths[disk][0] for disk in disks if disk in paths and not paths[disk][0].startswith("[")})
    active = [disk for disk in disks if disk in paths and paths[disk][1:] == ["active", "ready"]]
    if not maps:
        return False, "no multipath map for {} disks; check find_multipaths and the multipath blacklist".format(
            len(disks))
    if len(maps) > 1:
        return False, "paths are split across maps {}".format(", ".join(maps))
    if len(active) != live:
        return False, "map {} has {} active paths (expected {})".format(maps[0], len(active), live)
    return True, "map {} with {} active paths".format(maps[0], len(active))


class ValidationReport(object):
    def __init__(self):
        self.counts = {"PASS": 0, "WARN": 0, "FAIL": 0}

    def add(self, status, check, detail):
        self.counts[status] += 1
        print("[{}] {}: {}".format(status, check, detail))


def _count_status(actual, requested, problem):
    if actual == requested:
        return "PASS"
    # Extra sessions are never removed by this script, so they only warn.
    return "WARN" if actual > requested else problem


def _validate_host(report, apply_recommended):
    for unit in ("iscsid", "multipathd"):
        active = run_command(["systemctl", "is-active", unit])[1].strip() or "unknown"
        enabled = run_command(["systemctl", "is-enabled", unit])[1].strip() or "unknown"
        ok = active == "active" and enabled in ENABLED_UNIT_STATES
        report.add("PASS" if ok else "FAIL", unit + " service", "{}, {}".format(active, enabled))
    login_unit = find_login_unit()
    if login_unit:
        enabled = run_command(["systemctl", "is-enabled", login_unit])[1].strip() or "unknown"
        report.add("PASS" if enabled in ENABLED_UNIT_STATES else "FAIL", "iSCSI login unit",
                   "{} is {}; it logs in automatic node records at boot".format(login_unit, enabled))
    else:
        report.add("WARN", "iSCSI login unit", "neither open-iscsi.service nor iscsi.service exists; "
                   "automatic node records may not log in at boot")
    if not apply_recommended:
        return
    path = multipath_drop_in_path()
    report.add("PASS" if _read_root_file(path) == MULTIPATH_DROP_IN else "FAIL", "multipath drop-in",
               "{} (vendor MSFT, product Virtual HD)".format(path))
    defaults = get_multipath_defaults()
    if defaults is None:
        report.add("WARN", "multipath defaults", "could not read 'multipathd show config'")
        return
    differing = []
    for key, documented in DOCUMENTED_MULTIPATH_DEFAULTS:
        value = defaults.get(key, "unset")
        accepted = (documented, "on") if documented == "yes" else (documented,)
        if value.lower() not in accepted:
            differing.append("{} is {} (documented: {})".format(key, value, documented))
    if differing:
        report.add("WARN", "multipath defaults", "; ".join(differing) +
                   "; left unchanged because they apply to all multipath storage on this host")
    else:
        report.add("PASS", "multipath defaults", "documented values are in effect")


def _persistent_records_check(state, requested, problem):
    if not state.records:
        return problem, "none; sessions will not return after a reboot"
    persistent = sum(_to_int(settings.get("node.session.nr_sessions")) for _, _, settings in state.records)
    manual = [portal for _, portal, settings in state.records if settings.get("node.startup") != "automatic"]
    if manual:
        return problem, "node.startup is not automatic for {}; nr_sessions total {} (requested {})".format(
            ", ".join(manual), persistent, requested)
    return (_count_status(persistent, requested, problem),
            "nr_sessions total {} across {} node record(s), node.startup automatic (requested {})".format(
                persistent, len(state.records), requested))


def _validate_volume(report, outcome, state, details, paths, requested, apply_recommended):
    # Problems caused by this run fail; pre-existing state on skipped volumes only
    # warns, because this script never disconnects anything.
    problem = "FAIL" if outcome.connected_this_run else "WARN"
    label = "{} [{}] ".format(outcome.volume_name, outcome.target_iqn)
    live = len(state.sessions)
    detail = "{} (requested {})".format(live, requested)
    if not state:
        detail += "; not connected, see the errors above"
    elif state.records and not live:
        target_name, portal, _ = state.records[0]
        detail += ("; persistent configuration exists but no live sessions: log in with "
                   "'sudo iscsiadm -m node -T {} -p {} -l', or run disconnect_for_documentation.py "
                   "and re-run this script".format(target_name, portal))
    report.add(_count_status(live, requested, problem), label + "live sessions", detail)
    status, detail = _persistent_records_check(state, requested, problem)
    report.add(status, label + "persistent records", detail)
    if not state:
        return
    if state.records:
        # The node record is authoritative; some kernels do not negotiate DataDigest.
        negotiated = _volume_session_details(details, outcome.volume_iqn)[1]
        wrong = [portal for _, portal, settings in state.records
                 if any(settings.get(key, "").upper() != value for key, value in DIGEST_SETTINGS)]
        report.add(problem if wrong else "PASS", label + "digests", "{}; negotiated header/data: {}".format(
            "not CRC32C for " + ", ".join(wrong) if wrong else "CRC32C in the node records",
            ", ".join(negotiated) or "none"))
        if apply_recommended:
            wrong = [portal for _, portal, settings in state.records
                     if any(settings.get(key, "").lower() != value.lower() for key, value in RECOMMENDED_NODE_SETTINGS)]
            report.add(problem if wrong else "PASS", label + "recommended settings",
                       "differ for " + ", ".join(wrong) if wrong else "applied")
    ok, detail = _multipath_check(outcome.volume_iqn, live, details, paths)
    report.add("PASS" if ok else problem, label + "multipath paths", detail)


def validate_connections(outcomes, requested, apply_recommended):
    print("Validating host and volume configuration:")
    report = ValidationReport()
    _validate_host(report, apply_recommended)
    states = [check_connection(outcome.target_iqn, None, None, volume_iqn=outcome.volume_iqn)
              for outcome in outcomes]
    connected = [(outcome, state) for outcome, state in zip(outcomes, states) if outcome.connected_this_run]
    # multipathd adds paths asynchronously after login; give new volumes a short, bounded wait.
    for attempt in range(MULTIPATH_WAIT_ATTEMPTS if connected else 1):
        details, paths = get_session_details(), get_multipath_paths()
        if all(_multipath_check(outcome.volume_iqn, len(state.sessions), details, paths)[0]
               for outcome, state in connected if state):
            break
        if attempt + 1 < MULTIPATH_WAIT_ATTEMPTS:
            time.sleep(MULTIPATH_WAIT_SECONDS)
    for outcome, state in zip(outcomes, states):
        _validate_volume(report, outcome, state, details, paths, requested, apply_recommended)
    print("Validation: {} passed, {} warnings, {} failed".format(
        report.counts["PASS"], report.counts["WARN"], report.counts["FAIL"]
    ))
    return report.counts["FAIL"]


def finish_connections(outcomes, requested, apply_recommended):
    connected = [outcome for outcome in outcomes if outcome.connected_this_run and not outcome.failed]
    if connected:
        # Disks appear asynchronously after login; wait for udev before reading them.
        run_command(["sudo", "udevadm", "settle"])
        if apply_recommended and (get_multipath_defaults() or {}).get("find_multipaths", "").lower() == "strict":
            register_multipath_devices(connected)
    failures = validate_connections(outcomes, requested, apply_recommended)
    changed = [outcome.volume_name for outcome in outcomes if outcome.settings_changed_while_live]
    if changed:
        print("Notice: recommended iSCSI settings changed on {} while sessions were live; "
              "log out/in or reboot for updated iSCSI settings to take effect.".format(", ".join(changed)))
    if failures:
        raise ConnectScriptError("Validation found {} failed check(s); see the [FAIL] lines above".format(failures))


# check if azure cli is installed
def check_azcli():
    # az may come from a distro package, pip or the install script, so look for it on PATH.
    if not any(os.access(os.path.join(directory, "az"), os.X_OK)
               for directory in os.environ.get("PATH", "").split(os.pathsep) if directory):
        raise ConnectScriptError(
            "Azure CLI is not installed or not on PATH. It is required for successful execution of this "
            "connect script. Install it by following "
            "https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-linux, then run "
            "'az extension add -n elastic-san' and 'az login'."
        )


# get iqn info from the ElasticSAN
def get_iqns(elastic_san_subscription, resource_group_name, elastic_san_name, volume_group_name, volume_name):
    check_azcli()
    subscription_argument = " --subscription "+elastic_san_subscription if elastic_san_subscription is not None else ""
    command = "az elastic-san volume show -g {} -e {} -v {} -n {} --query storageTarget{}".format(resource_group_name, elastic_san_name, volume_group_name, volume_name, subscription_argument).split(' ')
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # timeout in case the extension is not installed and prompts the user to install
    timeout = 10
    while p.poll() is None and timeout > 0:
     time.sleep(1)
     timeout -= 1
    if timeout<=0:
        raise Exception('Command took longer than 10s')
    out, err = p.communicate()
    if "error" in err.decode("utf-8").lower():
        raise Exception(err)
    out = out.decode("utf-8")
    storage_target = json.loads(out)
    target_iqn = storage_target["targetIqn"]
    target_portal_hostname = storage_target["targetPortalHostname"]
    target_portal_port = storage_target["targetPortalPort"]
    return target_iqn, target_portal_hostname, target_portal_port


def _decode_output(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _run_az_command(command, description):
    # File-backed output cannot deadlock on a full pipe or a descendant retaining
    # a pipe handle. No background reader survives a timeout.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                command, stdout=stdout, stderr=stderr, start_new_session=(os.name == "posix")
            )
        except OSError as error:
            raise ZonalAffinityError("{} failed to start: {}".format(description, error))
        try:
            process.wait(timeout=AZ_CLI_TIMEOUT_SECONDS)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            try:
                if os.name == "posix":
                    # az may be a shell wrapper; terminate its own group, not
                    # just the wrapper, without affecting the caller's group.
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass  # The process exited between the deadline and termination.
            except OSError as cleanup_error:
                raise ZonalAffinityError(
                    "{} interrupted; Azure CLI termination failed: {}".format(
                        description, cleanup_error
                    )
                )
            try:
                process.wait(timeout=AZ_CLI_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                raise ZonalAffinityError(
                    "{} timed out; Azure CLI did not exit after termination".format(description)
                )
            if isinstance(error, KeyboardInterrupt):
                raise
            raise ZonalAffinityError(
                "{} timed out after {} seconds".format(description, AZ_CLI_TIMEOUT_SECONDS)
            )
        try:
            # Snapshot each length so a surviving descendant cannot extend a read
            # indefinitely by continuing to append output after the CLI exits.
            stdout.seek(0)
            stderr.seek(0)
            out = _decode_output(stdout.read(os.fstat(stdout.fileno()).st_size))
            err = _decode_output(stderr.read(os.fstat(stderr.fileno()).st_size)).strip()
        except (OSError, UnicodeError) as error:
            raise ZonalAffinityError(
                "{} failed while collecting output: {}".format(
                    description, error
                )
            )
    if process.returncode != 0:
        detail = err if err else "exit code {}".format(process.returncode)
        raise ZonalAffinityError("{} failed: {}".format(description, detail))
    return out


def get_vm_compute_metadata():
    request = Request(IMDS_COMPUTE_URL, headers={"Metadata": "true"})
    opener = build_opener(ProxyHandler({}))
    try:
        response = opener.open(request, timeout=IMDS_TIMEOUT_SECONDS)
        try:
            payload = _decode_output(response.read())
        finally:
            response.close()
    except HTTPError as error:
        raise ZonalAffinityError("IMDS request failed with HTTP status {}".format(error.code))
    except URLError as error:
        raise ZonalAffinityError("IMDS request failed: {}".format(error.reason))
    except socket.timeout:
        raise ZonalAffinityError(
            "IMDS request timed out after {} seconds".format(IMDS_TIMEOUT_SECONDS)
        )

    try:
        compute = json.loads(payload)
    except ValueError as error:
        raise ZonalAffinityError("IMDS returned invalid JSON: {}".format(error))
    if not isinstance(compute, dict):
        raise ZonalAffinityError("IMDS compute metadata must be a JSON object")
    return compute


def _required_metadata_value(compute, field_name):
    value = compute.get(field_name)
    if not isinstance(value, string_types) or not value.strip():
        raise ZonalAffinityError(
            "IMDS compute metadata is missing a valid '{}' value".format(field_name)
        )
    return value.strip()


def canonicalize_subscription_id(subscription_id, source):
    if not isinstance(subscription_id, string_types):
        raise ZonalAffinityError("{} subscription ID must be a GUID".format(source))
    canonical_subscription_id = subscription_id.strip().lower()
    if SUBSCRIPTION_ID_PATTERN.match(canonical_subscription_id) is None:
        raise ZonalAffinityError("{} subscription ID must be a GUID".format(source))
    return canonical_subscription_id


def resolve_elastic_san_subscription_id(elastic_san_subscription):
    command = ["az", "account", "show"]
    if elastic_san_subscription is not None:
        command.extend(["--subscription", elastic_san_subscription])
    command.extend(["--query", "id", "--output", "tsv"])
    subscription_id = _run_az_command(
        command, "Elastic SAN subscription resolution"
    ).strip()
    if not subscription_id:
        raise ZonalAffinityError("Elastic SAN subscription resolution returned an empty ID")
    return canonicalize_subscription_id(subscription_id, "Elastic SAN")


def get_elastic_san_location(
    elastic_san_subscription_id, resource_group_name, elastic_san_name
):
    command = [
        "az",
        "elastic-san",
        "show",
        "-g",
        resource_group_name,
        "--elastic-san-name",
        elastic_san_name,
        "--subscription",
        elastic_san_subscription_id,
        "--query",
        "location",
        "--output",
        "tsv",
    ]
    location = _run_az_command(
        command, "Elastic SAN location lookup"
    ).strip()
    if not location:
        raise ZonalAffinityError("Elastic SAN location lookup returned an empty location")
    return location


def get_azure_locations(elastic_san_subscription_id):
    elastic_san_subscription_id = canonicalize_subscription_id(
        elastic_san_subscription_id, "Elastic SAN"
    )
    url = (
        "https://management.azure.com/subscriptions/{}/locations"
        "?api-version=2022-12-01"
    ).format(elastic_san_subscription_id)
    command = [
        "az",
        "rest",
        "--method",
        "get",
        "--url",
        url,
        "--output",
        "json",
    ]
    payload = _run_az_command(command, "Azure location REST query")
    try:
        response = json.loads(payload)
    except ValueError as error:
        raise ZonalAffinityError("Azure location REST query returned invalid JSON: {}".format(error))
    if not isinstance(response, dict):
        raise ZonalAffinityError("Azure location REST response must be a JSON object")
    locations = response.get("value")
    if not isinstance(locations, list):
        raise ZonalAffinityError(
            "Azure location REST response must contain a top-level 'value' array"
        )
    return locations


def map_logical_to_physical_zone(locations, location_name, logical_zone):
    normalized_location = location_name.strip().lower()
    region = None
    for candidate in locations:
        if not isinstance(candidate, dict):
            raise ZonalAffinityError("Azure location REST response contains a malformed region entry")
        candidate_name = candidate.get("name")
        if not isinstance(candidate_name, string_types) or not candidate_name.strip():
            raise ZonalAffinityError("Azure location REST response contains a malformed region entry")
        if candidate_name.strip().lower() == normalized_location:
            if region is not None:
                raise ZonalAffinityError(
                    "Azure location REST response contains duplicate region '{}'".format(
                        normalized_location
                    )
                )
            region = candidate

    if region is None:
        raise ZonalAffinityError(
            "Azure location REST response has no region matching Elastic SAN location '{}'".format(
                location_name
            )
        )

    if "availabilityZoneMappings" not in region:
        raise ZonalAffinityError(
            "Region '{}' is missing availabilityZoneMappings".format(region.get("name"))
        )

    mappings = region["availabilityZoneMappings"]
    if not isinstance(mappings, list) or not mappings:
        raise ZonalAffinityError(
            "Region '{}' has malformed availabilityZoneMappings".format(region.get("name"))
        )

    normalized_logical_zone = logical_zone.strip()
    zone_map = {}
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise ZonalAffinityError("availabilityZoneMappings contains a malformed entry")
        mapped_logical_zone = mapping.get("logicalZone")
        mapped_physical_zone = mapping.get("physicalZone")
        if (
            not isinstance(mapped_logical_zone, string_types)
            or not mapped_logical_zone.strip()
            or not isinstance(mapped_physical_zone, string_types)
            or not mapped_physical_zone.strip()
        ):
            raise ZonalAffinityError("availabilityZoneMappings contains a malformed entry")
        mapped_logical_zone = mapped_logical_zone.strip()
        mapped_physical_zone = normalize_physical_zone(mapped_physical_zone)
        if mapped_logical_zone in zone_map:
            raise ZonalAffinityError(
                "availabilityZoneMappings contains duplicate logical zone '{}'".format(
                    mapped_logical_zone
                )
            )
        zone_map[mapped_logical_zone] = mapped_physical_zone

    if normalized_logical_zone not in zone_map:
        raise ZonalAffinityError(
            "Region '{}' has no availability-zone mapping for logical zone '{}'".format(
                region.get("name"), logical_zone
            )
        )
    return zone_map[normalized_logical_zone]


def resolve_zonal_affinity_context(
    elastic_san_subscription, resource_group_name, elastic_san_name
):
    compute = get_vm_compute_metadata()
    logical_zone = compute.get("zone")
    if not isinstance(logical_zone, string_types) or not logical_zone.strip():
        raise ZonalAffinityError(
            "VM is not availability-zone pinned; IMDS compute.zone is empty or missing"
        )
    logical_zone = logical_zone.strip()
    imds_subscription_id = canonicalize_subscription_id(
        _required_metadata_value(compute, "subscriptionId"), "IMDS"
    )
    location_name = _required_metadata_value(compute, "location")

    elastic_san_subscription_id = resolve_elastic_san_subscription_id(
        elastic_san_subscription
    )
    if elastic_san_subscription_id != imds_subscription_id:
        raise ZonalAffinityError(
            "Elastic SAN subscription '{}' does not match VM subscription '{}'".format(
                elastic_san_subscription_id, imds_subscription_id
            )
        )

    elastic_san_location = get_elastic_san_location(
        elastic_san_subscription_id, resource_group_name, elastic_san_name
    )
    if elastic_san_location.strip().lower() != location_name.strip().lower():
        raise ZonalAffinityError(
            "The VM and Elastic SAN must be in the same region."
        )

    locations = get_azure_locations(elastic_san_subscription_id)
    physical_zone = map_logical_to_physical_zone(
        locations, elastic_san_location, logical_zone
    )
    return elastic_san_subscription_id, physical_zone


def resolve_physical_zone(
    elastic_san_subscription, resource_group_name, elastic_san_name
):
    return resolve_zonal_affinity_context(
        elastic_san_subscription, resource_group_name, elastic_san_name
    )[1]


def normalize_physical_zone(physical_zone):
    if not isinstance(physical_zone, string_types):
        raise ZonalAffinityError("Physical zone must be a nonempty string")
    normalized_physical_zone = physical_zone.strip().lower()
    if (
        not normalized_physical_zone
        or PHYSICAL_ZONE_PATTERN.match(normalized_physical_zone) is None
    ):
        raise ZonalAffinityError(
            "Physical zone '{}' contains characters that are unsafe for an IQN suffix".format(
                physical_zone
            )
        )
    return normalized_physical_zone


def decorate_target_iqn(target_iqn, physical_zone):
    normalized_physical_zone = normalize_physical_zone(physical_zone)
    if not isinstance(target_iqn, string_types) or not target_iqn:
        raise ZonalAffinityError("Target IQN must be a nonempty string")
    # Provisional contract: the Elastic SAN front end must parse and strip this suffix
    # before zonal-affinity routing can be used in production.
    decorated_iqn = "{}:az-{}".format(target_iqn, normalized_physical_zone)
    try:
        iqn_byte_length = len(decorated_iqn.encode("utf-8"))
    except UnicodeEncodeError:
        raise ZonalAffinityError("Decorated target IQN is not valid UTF-8")
    if iqn_byte_length > MAX_IQN_UTF8_BYTES:
        raise ZonalAffinityError(
            "Decorated target IQN exceeds the {}-byte UTF-8 limit".format(
                MAX_IQN_UTF8_BYTES
            )
        )
    if IQN_PATTERN.match(decorated_iqn) is None:
        raise ZonalAffinityError(
            "Decorated target IQN contains unsafe or non-lowercase characters; "
            "the service IQN must not be rewritten"
        )
    return decorated_iqn


def get_mapped_volume_target(
    elastic_san_subscription_id, resource_group_name, elastic_san_name,
    volume_group_name, volume_name,
):
    command = [
        "az", "elastic-san", "volume", "show",
        "-g", resource_group_name, "-e", elastic_san_name,
        "-v", volume_group_name, "-n", volume_name,
        "--subscription", elastic_san_subscription_id,
        "--query", "storageTarget", "--output", "json",
    ]
    payload = _run_az_command(command, "Volume '{}' target lookup".format(volume_name))
    try:
        target = json.loads(payload)
    except ValueError as error:
        raise ZonalAffinityError(
            "Volume '{}' target lookup returned invalid JSON: {}".format(volume_name, error)
        )
    if not isinstance(target, dict):
        raise ZonalAffinityError("Volume '{}' storageTarget must be a JSON object".format(volume_name))
    for field in ("targetIqn", "targetPortalHostname"):
        if not isinstance(target.get(field), string_types) or not target[field]:
            raise ZonalAffinityError(
                "Volume '{}' storageTarget is missing a valid '{}'".format(volume_name, field)
            )
    hostname = target["targetPortalHostname"]
    # Reject unsafe discovery data before DNS or native inventory.
    if re.match(r"^[a-zA-Z0-9][a-zA-Z0-9.-]*\Z", hostname) is None:
        raise ZonalAffinityError("Volume '{}' has an unsafe target portal hostname".format(volume_name))
    port = target.get("targetPortalPort")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ZonalAffinityError("Volume '{}' has an invalid target portal port".format(volume_name))
    return target["targetIqn"], hostname, port


def preflight_zonal_affinity(
    elastic_san_subscription, resource_group_name, elastic_san_name,
    volume_group_name, volume_names,
):
    if sys.version_info < (3, 5):
        raise ZonalAffinityError("Zonal affinity requires Python 3.5 or later")
    selected_volumes = tuple(volume_names)
    subscription_id, physical_zone = resolve_zonal_affinity_context(
        elastic_san_subscription, resource_group_name, elastic_san_name
    )
    targets = []
    for volume_name in selected_volumes:
        target_iqn, hostname, port = get_mapped_volume_target(
            subscription_id, resource_group_name, elastic_san_name,
            volume_group_name, volume_name,
        )
        targets.append((
            volume_name,
            (decorate_target_iqn(target_iqn, physical_zone), hostname, port),
        ))
    return targets


def get_portals(hostname, number_of_sessions, enable_vip_distribution=False):
    if not enable_vip_distribution:
        return [(hostname, number_of_sessions)]
    from ipaddress import IPv4Address
    try:
        addresses = sorted({IPv4Address(entry[4][0]) for entry in socket.getaddrinfo(
            hostname, None, socket.AF_INET, socket.SOCK_STREAM
        )})
    except (OSError, ValueError) as error:
        raise RuntimeError("DNS lookup failed for '{}': {}".format(hostname, error))
    if not addresses:
        raise RuntimeError("DNS lookup returned no IPv4 addresses for '{}'".format(hostname))
    if len(addresses) == 1:
        return [(hostname, number_of_sessions)]
    count, remainder = divmod(number_of_sessions, len(addresses))
    return [(str(address), count + (index < remainder)) for index, address in enumerate(addresses)]


def _iscsiadm(arguments, allow_empty=False):
    process = subprocess.Popen(
        ["sudo", "iscsiadm"] + arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    out, err = process.communicate()
    out, err = _decode_output(out), _decode_output(err).strip()
    if allow_empty and process.returncode == 21 and (out + "\n" + err).strip() in (
        "No active sessions.", "iscsiadm: No active sessions.",
        "No records found", "iscsiadm: No records found",
    ):
        return ""
    if process.returncode != 0 or err:
        raise RuntimeError("iscsiadm {} failed: {}".format(
            " ".join(arguments), err or "exit code {}".format(process.returncode)
        ))
    return out


def _belongs_to_volume(target_name, volume_iqn):
    # The volume's service IQN itself, or that IQN with one :az-<zone> zonal
    # decoration; exact and case-insensitive, so no other volume ever matches.
    target_name, volume_iqn = target_name.lower(), volume_iqn.lower()
    if target_name == volume_iqn:
        return True
    zone = target_name[len(volume_iqn + ":az-"):] if target_name.startswith(volume_iqn + ":az-") else ""
    return bool(zone) and ":" not in zone


def read_node_record(target_name, portal):
    settings = {}
    for line in _iscsiadm(["-m", "node", "--targetname", target_name, "--portal", portal]).splitlines():
        key, separator, value = line.partition(" = ")
        if separator:
            settings[key.strip()] = value.strip()
    return settings


class ExistingState(object):
    """Node records and live sessions that already belong to one volume; falsy when there are none."""

    def __init__(self, records, sessions):
        self.records = records  # [(target name, portal, {setting: value})]
        self.sessions = sessions  # [(sid, target name, portal)]

    def __bool__(self):
        return bool(self.records or self.sessions)

    def persistent_sessions(self):
        return sum(_to_int(settings.get("node.session.nr_sessions")) for _, _, settings in self.records
                   if settings.get("node.startup") in ("automatic", "onboot"))

    def describe(self):
        live = len(self.sessions)
        if self.records and live:
            return ("Skipped: already connected ({} live / {} persistent); run disconnect_for_documentation.py "
                    "first to change the layout".format(live, self.persistent_sessions()))
        if self.records:
            target_name, portal, _ = self.records[0]
            return ("Skipped: persistent configuration exists but no live sessions; log in with "
                    "'sudo iscsiadm -m node -T {} -p {} -l', or run disconnect_for_documentation.py and "
                    "re-run this script".format(target_name, portal))
        return ("Skipped: already connected ({} live / 0 persistent); these sessions are not persistent "
                "and will not return after a reboot".format(live))


def check_connection(target_iqn, target_portal_hostname, target_portal_port, volume_iqn=None):
    # Every node record and session for the volume counts, whatever its portal.
    volume_iqn = volume_iqn or target_iqn
    sessions = []
    for line in _iscsiadm(["-m", "session"], allow_empty=True).splitlines():
        words = line.split()
        if len(words) >= 4 and _belongs_to_volume(words[3], volume_iqn):
            sessions.append((words[1].strip("[]"), words[3], words[2].rsplit(",", 1)[0]))
    records = []
    for line in _iscsiadm(["-m", "node"], allow_empty=True).splitlines():
        words = line.split()
        if len(words) >= 2 and _belongs_to_volume(words[1], volume_iqn):
            portal = words[0].rsplit(",", 1)[0]
            if not any(record[:2] == (words[1], portal) for record in records):
                records.append((words[1], portal, read_node_record(words[1], portal)))
    return ExistingState(records, sessions)


def update_recommended_settings(volume_name, target_iqn, state, recommended_settings):
    """Applies only differing recommended values to existing records; True if live sessions need a re-login."""
    changed = False
    for target_name, portal, settings in state.records:
        for key, value in recommended_settings:
            if settings.get(key, "").lower() == value.lower():
                continue
            try:
                _iscsiadm(["-m", "node", "--targetname", target_name, "--portal", portal,
                           "--op", "update", "-n", key, "-v", value])
            except (OSError, RuntimeError) as error:
                # Keep going so values that did change still trigger the re-login notice.
                print("Warning: {} [{}], portal {}: setting {} failed: {}".format(
                    volume_name, target_iqn, portal, key, error), file=sys.stderr)
                continue
            settings[key] = value
            changed = True
    return changed and bool(state.sessions)


def _session_ids(target_iqn):
    output = _iscsiadm(["-m", "session"], allow_empty=True)
    return {sid for sid, iqn in re.findall(
        r"^\s*\S+:\s+\[([0-9]+)\]\s+\S+\s+(\S+)", output, re.M
    ) if iqn == target_iqn}


def _delete_created_node(volume_name, target_iqn, node, sessions_before):
    # A record created by this run that never got a session would make every
    # re-run skip the volume, so remove exactly that record. Keep it whenever a
    # session may be using it: a failed login can still bring one up.
    portal = node[node.index("--portal") + 1]
    reason = None
    if sessions_before is None:
        reason = "the sessions could not be read before the login"
    else:
        try:
            if _session_ids(target_iqn) - sessions_before:
                reason = "a new session for the target appeared"
        except (OSError, RuntimeError) as error:
            reason = "the sessions could not be read: {}".format(error)
    if reason:
        print("Warning: {} [{}], portal {}: kept the node record created by this run because {}. Check "
              "'sudo iscsiadm -m session'; if no session uses it, remove it with "
              "'sudo iscsiadm -m node -T {} -p {} -o delete' before re-running.".format(
                  volume_name, target_iqn, portal, reason, target_iqn, portal), file=sys.stderr)
        return
    try:
        _iscsiadm(node + ["--op", "delete"])
    except (OSError, RuntimeError) as error:
        print("Warning: {} [{}], portal {}: could not remove the node record created by this run: {}. "
              "Remove it with 'sudo iscsiadm -m node -T {} -p {} -o delete' before re-running.".format(
                  volume_name, target_iqn, portal, error, target_iqn, portal), file=sys.stderr)
        return
    print("{} [{}]: removed the node record created for portal {}; no session was established".format(
        volume_name, target_iqn, portal), file=sys.stderr)


def connect_volume(volume_name, target_iqn, portals, target_portal_port, recommended_settings=()):
    print("{} [{}]: Connecting to this volume".format(volume_name, target_iqn))
    connected = False
    for portal, count in portals:
        if not count:
            if connected:
                continue
            count = 1  # Try spare addresses only if no allocated portal logged in.
        node = ["-m", "node", "--targetname", target_iqn,
                "--portal", "{}:{}".format(portal, target_portal_port)]
        created = logged_in = False
        sessions_before = None
        try:
            _iscsiadm(node + ["--op", "new"])
            created = True
            sessions_before = _session_ids(target_iqn)
            # Native --login honors nr_sessions: seed once, then clone by SID.
            for key, value in (
                ("node.session.nr_sessions", "1"), ("node.startup", "automatic"),
                ("node.conn[0].iscsi.HeaderDigest", "CRC32C"),
                ("node.conn[0].iscsi.DataDigest", "CRC32C"),
            ) + tuple(recommended_settings):
                _iscsiadm(node + ["--op", "update", "-n", key, "-v", value])
            previous = _session_ids(target_iqn) if count > 1 else set()
            _iscsiadm(node + ["--login"])
            connected = logged_in = True
            if count > 1:
                added = _session_ids(target_iqn) - previous
                if len(added) != 1:
                    raise RuntimeError("Cannot identify a single new seed SID; no clones were attempted")
                sid = added.pop()
                for _ in range(count - 1):
                    _iscsiadm(["-m", "session", "-r", sid, "--op", "new"])
            _iscsiadm(node + ["--op", "update", "-n", "node.session.nr_sessions", "-v", str(count)])
        except (OSError, RuntimeError) as error:
            print("Warning: {} [{}], portal {}:{}: {}. Partial state may remain.".format(
                volume_name, target_iqn, portal, target_portal_port, error
            ), file=sys.stderr)
            if created and not logged_in:
                _delete_created_node(volume_name, target_iqn, node, sessions_before)
    if not connected:
        raise RuntimeError("{} [{}]: Login failed through every target portal".format(volume_name, target_iqn))


class VolumeOutcome(object):
    """What this run did for one selected volume; validation re-reads the native state."""

    def __init__(self, volume_name, target_iqn, volume_iqn, connected_this_run):
        self.volume_name = volume_name
        self.target_iqn = target_iqn  # What this run connects to (zonally decorated when requested).
        self.volume_iqn = volume_iqn  # The volume's service IQN that identifies its existing state.
        self.connected_this_run = connected_this_run
        self.failed = False
        self.settings_changed_while_live = False


def _lookup_volume(elastic_san_subscription, resource_group_name, elastic_san_name, volume_group_name, volume_name):
    try:
        return get_iqns(
            elastic_san_subscription, resource_group_name, elastic_san_name,
            volume_group_name, volume_name,
        )
    except ConnectScriptError:
        raise
    except Exception as error:
        detail = _decode_output(error.args[0]) if error.args else error
        raise ConnectScriptError("Volume '{}' lookup failed: {}".format(volume_name, str(detail).strip()))


def connect_volumes(
    elastic_san_subscription,
    resource_group_name,
    elastic_san_name,
    volume_group_name,
    volume_names,
    number_of_sessions,
    enable_zonal_affinity=False,
    enable_vip_distribution=False,
    recommended_settings=(),
    prepare_host=None,
):
    if number_of_sessions < 1:
        raise ValueError("The number of sessions must be at least 1")
    # Read-only lookups (Azure, zone mapping, VIP DNS) for every volume come
    # first, so any failure stops before the host or any volume changes.
    if enable_zonal_affinity:
        targets = preflight_zonal_affinity(
            elastic_san_subscription, resource_group_name, elastic_san_name,
            volume_group_name, volume_names,
        )
    else:
        targets = [
            (volume_name, _lookup_volume(
                elastic_san_subscription, resource_group_name, elastic_san_name,
                volume_group_name, volume_name,
            ))
            for volume_name in volume_names
        ]
    targets = [
        (volume_name, target, get_portals(target[1], number_of_sessions, enable_vip_distribution))
        for volume_name, target in targets
    ]
    if prepare_host is not None:
        prepare_host()

    # Plan every volume before changing any of them.
    plans, planned = [], set()
    for volume_name, (target_iqn, target_hostname, target_port), portals in targets:
        # Zonal preflight appends exactly one :az-<zone> suffix to the service IQN.
        volume_iqn = target_iqn.rsplit(":az-", 1)[0] if enable_zonal_affinity else target_iqn
        if volume_iqn.lower() in planned:
            print("{} [{}]: Skipped: listed more than once".format(volume_name, target_iqn))
            continue
        planned.add(volume_iqn.lower())
        existing = check_connection(target_iqn, target_hostname, target_port, volume_iqn=volume_iqn)
        plans.append((volume_name, target_iqn, volume_iqn, target_port, portals, existing))

    outcomes = []
    for volume_name, target_iqn, volume_iqn, target_port, portals, existing in plans:
        outcome = VolumeOutcome(volume_name, target_iqn, volume_iqn, not existing)
        try:
            if existing:
                print("{} [{}]: {}".format(volume_name, target_iqn, existing.describe()))
                # Only tuning values change on connected volumes; digests,
                # nr_sessions, startup and sessions are left as they are.
                outcome.settings_changed_while_live = update_recommended_settings(
                    volume_name, target_iqn, existing, recommended_settings)
            else:
                connect_volume(volume_name, target_iqn, portals, target_port, recommended_settings)
        except (OSError, RuntimeError) as error:
            # Report and continue; validation decides PASS/WARN/FAIL.
            print("{} [{}]: Failed: {}".format(volume_name, target_iqn, error), file=sys.stderr)
            outcome.failed = True
        outcomes.append(outcome)
    return outcomes


def create_argument_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--elastic-san-subscription",
        "--subscription",
        dest="elastic_san_subscription",
        help=(
            "Elastic SAN resource subscription name or ID. "
            "--subscription is retained as a compatibility alias."
        ),
    )
    parser.add_argument("-g", "--resource-group")
    parser.add_argument("-e", "--elastic-san")
    parser.add_argument("-v", "--volume-group")
    parser.add_argument("-n", "--volumes", nargs='+')
    parser.add_argument("-s", "--num-of-sessions")
    parser.add_argument(
        "--enable-zonal-affinity",
        action="store_true",
        help=(
            "Opt in to provisional logical-to-physical availability-zone routing. "
            "Requires Elastic SAN front-end support for parsing the IQN suffix."
        ),
    )
    parser.add_argument(
        "--enable-vip-distribution", action="store_true",
        help="Distribute original session portals across the target's IPv4 VIPs, independently of zonal affinity.",
    )
    parser.add_argument(
        "--skip-recommended-settings", action="store_true",
        help=(
            "Do not apply the Elastic SAN best-practice client settings: the recommended iSCSI node "
            "values, the device-scoped multipath settings file and multipath WWID registration. "
            "Prerequisite packages, services and CRC32C digests are always configured."
        ),
    )
    return parser


def main(argv=None):
    # get command line arguments
    parser = create_argument_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    
    # parameters
    elastic_san_subscription = args.elastic_san_subscription
    resource_group_name = args.resource_group
    elastic_san_name = args.elastic_san
    volume_group_name = args.volume_group
    volume_names = args.volumes
    number_of_sessions = min(32, int(args.num_of_sessions)) if args.num_of_sessions is not None else 32 # default is 32, also the maximum allowed number of sessions
    
    if None in [resource_group_name, elastic_san_name, volume_group_name, volume_names]:
        raise ConnectScriptError('Need to provide resource_group_name, elastic_san_name, volume_group_name, volume_names to connect to the ElasticSAN volume')

    check_privileges()
    distro = detect_distro()
    apply_recommended = not args.skip_recommended_settings
    outcomes = connect_volumes(
        elastic_san_subscription,
        resource_group_name,
        elastic_san_name,
        volume_group_name,
        volume_names,
        number_of_sessions,
        args.enable_zonal_affinity,
        args.enable_vip_distribution,
        recommended_settings=RECOMMENDED_NODE_SETTINGS if apply_recommended else (),
        prepare_host=lambda: prepare_iscsi_host(distro, apply_recommended),
    )
    finish_connections(outcomes, number_of_sessions, apply_recommended)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, ZonalAffinityError) as error:
        # Stops are expected outcomes, not crashes: one line and a non-zero exit.
        print("ERROR: {}".format(error), file=sys.stderr)
        sys.exit(1)
