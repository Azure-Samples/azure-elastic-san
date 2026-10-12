"""Offline tests for the host preparation, existing-state plan and validation
that every connect mode shares. A FakeHost stands in for the Linux VM behind
subprocess.Popen, so the script's real command construction and parsing run."""
import io
import os
import socket
import subprocess
import sys
import unittest
from unittest import mock

from test_connect_for_documentation import SCRIPT_PATH, connect

REAL_FIND_TOOL = connect.find_tool


def iqn(volume):
    return "iqn.2023-01.net.windows.core.blob.elasticsan.es-test:" + volume


HOST = "es-test.z1.blob.storage.azure.net"
PORTAL = HOST + ":3260"
TARGETS = {name: (iqn(name), HOST, 3260) for name in ("volume1", "volume2", "volume10", "az-data")}
RECOMMENDED_KEYS = [key for key, _ in connect.RECOMMENDED_NODE_SETTINGS]
DIGEST_KEYS = [key for key, _ in connect.DIGEST_SETTINGS]
NEW_NODE_DEFAULTS = {
    "node.startup": "manual",
    "node.session.nr_sessions": "1",
    "node.conn[0].iscsi.HeaderDigest": "None",
    "node.conn[0].iscsi.DataDigest": "None",
    "node.conn[0].iscsi.MaxXmitDataSegmentLength": "0",
    "node.session.iscsi.MaxBurstLength": "16776192",
    "node.session.iscsi.FirstBurstLength": "262144",
    "node.conn[0].iscsi.MaxRecvDataSegmentLength": "262144",
    "node.session.iscsi.InitialR2T": "No",
    "node.session.iscsi.ImmediateData": "Yes",
    "node.conn[0].timeo.login_timeout": "15",
    "node.conn[0].timeo.logout_timeout": "15",
}


def connected_record(**overrides):
    """A node record as this script leaves it after a successful connect."""
    record = dict(NEW_NODE_DEFAULTS)
    record.update(connect.RECOMMENDED_NODE_SETTINGS + connect.DIGEST_SETTINGS)
    record.update({"node.startup": "automatic", "node.session.nr_sessions": "32"})
    record.update(overrides)
    return record


class FakeHost(object):
    """Offline model of one Linux VM: packages, services, files, iSCSI and multipath."""

    def __init__(self, family="debian"):
        self.packages = set(connect.PREREQUISITE_PACKAGES[family])
        self.unit_files = {"iscsid", "multipathd", "open-iscsi" if family == "debian" else "iscsi"}
        self.active, self.enabled = set(), set()
        self.files = {
            connect.INITIATOR_NAME_FILE: "InitiatorName=iqn.2004-10.com.ubuntu:01:host\n",
            connect.MULTIPATH_CONF: "defaults {\n    user_friendly_names yes\n}\n",
        }
        self.multipath_defaults = {"find_multipaths": "yes", "polling_interval": "5", "user_friendly_names": "yes"}
        self.nodes = {}  # (target name, portal) -> settings
        self.sessions = []  # [sid, target name, portal]
        self.next_sid = 1
        self.sudo_allowed = True
        self.tools = {"sudo", "iscsi-iname", "mpathconf", "apt-get", "dnf", "yum", "zypper", "tdnf"}
        self.failed_logins = set()
        self.login_error_with_session = False
        self.session_reads_fail_after_login = False
        self.login_attempted = False
        self.failed_updates = set()
        self.fail_clones = False
        self.negotiated_data_digest = "CRC32C"
        self.registered = set()
        self.commands = []

    def __call__(self, command, **kwargs):
        assert not kwargs.get("shell")
        self.commands.append(list(command))
        host = self

        class Process(object):
            returncode = None

            def communicate(self, input=None):
                code, out, err = host.handle(list(command), input.decode("utf-8") if input else None)
                self.returncode = code
                return out.encode("utf-8"), err.encode("utf-8")

        return Process()

    def find_tool(self, name):
        return "/usr/sbin/" + name if name in self.tools else None

    def add_session(self, target_name, portal=PORTAL):
        self.sessions.append([str(self.next_sid), target_name, portal])
        self.next_sid += 1

    def ran(self, *prefix):
        return [command for command in self.commands if command[:len(prefix)] == list(prefix)]

    def index(self, predicate):
        return next(i for i, command in enumerate(self.commands) if predicate(command))

    def handle(self, command, stdin):
        if command[0] == "sudo":
            if command[1:] == ["-n", "true"]:
                return (0, "", "") if self.sudo_allowed else (1, "", "sudo: a password is required")
            command = command[1:]
        name, args = command[0], command[1:]
        if name == "iscsiadm":
            return self.iscsiadm(args)
        if name == "dpkg-query":
            return (0, "install ok installed", "") if args[-1] in self.packages else (1, "", "no packages found")
        if name == "rpm":
            return (0, args[-1], "") if args[-1] in self.packages else (1, "package is not installed", "")
        if name in ("env", "apt-get", "dnf", "yum", "zypper", "tdnf"):
            self.packages.update(arg for arg in args if not arg.startswith("-") and "=" not in arg
                                 and arg not in ("apt-get", "install", "update"))
            return 0, "", ""
        if name == "cat":
            return (0, self.files[args[0]], "") if args[0] in self.files else (1, "", "No such file")
        if name == "tee":
            self.files[args[0]] = stdin
            return 0, stdin, ""
        if name == "mkdir":
            return 0, "", ""
        if name == "iscsi-iname":
            return 0, "iqn.2004-10.com.example:generated\n", ""
        if name == "systemctl":
            return self.systemctl(args)
        if name == "mpathconf":
            self.files[connect.MULTIPATH_CONF] = "defaults {\n    find_multipaths yes\n}\n"
            return 0, "", ""
        if name == "multipathd":
            return self.multipathd(args)
        if name == "multipath" and args[0] == "-a":
            self.registered.update(target for _, target, _ in self.sessions_for_disk(args[1]))
            return 0, "", ""
        if name == "udevadm":
            return 0, "", ""
        raise AssertionError("unexpected command: {}".format(command))

    def systemctl(self, args):
        unit = args[-1].replace(".service", "")
        if args[0] == "list-unit-files":
            return (0, "{}.service enabled enabled\n".format(unit), "") if unit in self.unit_files else (1, "", "")
        if args[0] == "enable":
            self.enabled.add(unit)
            if "--now" in args:
                self.active.add(unit)
            return 0, "", ""
        if args[0] == "restart":
            self.active.add(unit)
            return 0, "", ""
        if args[0] == "is-active":
            return (0, "active", "") if unit in self.active else (3, "inactive", "")
        if args[0] == "is-enabled":
            return (0, "enabled", "") if unit in self.enabled else (1, "disabled", "")
        raise AssertionError("unexpected systemctl: {}".format(args))

    def disk(self, sid):
        return "sd{}".format(sid)

    def sessions_for_disk(self, device):
        return [session for session in self.sessions if "/dev/" + self.disk(session[0]) == device]

    def map_name(self, target_name):
        if self.multipath_defaults["find_multipaths"] == "strict" and target_name not in self.registered:
            return "[orphan]"
        return "map-" + target_name.rsplit(":", 1)[-1]

    def multipathd(self, args):
        if args == ["show", "config"]:
            lines = ["defaults {"] + ['\t{} "{}"'.format(k, v) for k, v in sorted(self.multipath_defaults.items())]
            return 0, "\n".join(lines + ["}", "blacklist {", "}"]) + "\n", ""
        if args[:3] == ["show", "paths", "raw"]:
            return 0, "".join("{} {} active ready\n".format(self.disk(sid), self.map_name(target))
                              for sid, target, _ in self.sessions), ""
        if args == ["reconfigure"]:
            return 0, "ok", ""
        raise AssertionError("unexpected multipathd: {}".format(args))

    def iscsiadm(self, args):
        if args == ["-m", "session"]:
            if self.session_reads_fail_after_login and self.login_attempted:
                return 1, "", "iscsiadm: permission denied"
            if not self.sessions:
                return 21, "", "iscsiadm: No active sessions."
            return 0, "".join("tcp: [{}] {},-1 {} (non-flash)\n".format(sid, portal, target)
                              for sid, target, portal in self.sessions), ""
        if args == ["-m", "session", "-P", "3"]:
            if not self.sessions:
                return 21, "", "iscsiadm: No active sessions."
            return 0, "".join(
                "Target: {} (non-flash)\n\tPersistent Portal: {},-1\n\t\tSID: {}\n"
                "\t\tHeaderDigest: CRC32C\n\t\tDataDigest: {}\n"
                "\t\t\tAttached scsi disk {}\t\tState: running\n".format(
                    target, portal, sid, self.negotiated_data_digest, self.disk(sid))
                for sid, target, portal in self.sessions), ""
        if args == ["-m", "node"]:
            if not self.nodes:
                return 21, "", "iscsiadm: No records found"
            return 0, "".join("{},-1 {}\n".format(portal, target) for target, portal in self.nodes), ""
        if args[:3] == ["-m", "session", "-r"]:
            if self.fail_clones:
                return 15, "", "iscsiadm: session clone failed"
            session = next(s for s in self.sessions if s[0] == args[3])
            self.add_session(session[1], session[2])
            return 0, "", ""
        key = (args[args.index("--targetname") + 1], args[args.index("--portal") + 1])
        if "--op" not in args and "--login" not in args:
            return 0, "".join("{} = {}\n".format(k, v) for k, v in sorted(self.nodes[key].items())), ""
        if args[-2:] == ["--op", "new"]:
            assert key not in self.nodes, "must not replace an existing node record"
            self.nodes[key] = dict(NEW_NODE_DEFAULTS)
            return 0, "New iSCSI node added", ""
        if args[-2:] == ["--op", "delete"]:
            assert not any(s[1:] == list(key) for s in self.sessions), "record has a live session"
            del self.nodes[key]
            return 0, "", ""
        if args[-1] == "--login":
            self.login_attempted = True
            if key[1] in self.failed_logins:
                if self.login_error_with_session:
                    self.add_session(*key)
                return 15, "", "iscsiadm: Could not login to [{}]".format(key[1])
            for _ in range(int(self.nodes[key]["node.session.nr_sessions"])):
                self.add_session(*key)
            return 0, "Login successful", ""
        assert args[args.index("--op") + 1] == "update", args
        if args[args.index("-n") + 1] in self.failed_updates:
            return 1, "", "iscsiadm: update failed"
        self.nodes[key][args[args.index("-n") + 1]] = args[args.index("-v") + 1]
        return 0, "", ""


class HardeningTestCase(unittest.TestCase):
    family = "debian"

    def setUp(self):
        self.host = FakeHost(self.family)
        self.lookups = []

        def lookup(*args):
            self.lookups.append(args[-1])
            return TARGETS[args[-1]]

        patches = (
            mock.patch.object(connect.subprocess, "Popen", side_effect=self.host),
            mock.patch.object(connect.os, "geteuid", create=True, return_value=1000),
            mock.patch.object(connect, "find_tool", side_effect=lambda name: self.host.find_tool(name)),
            mock.patch.object(connect, "read_os_release", return_value={"ID": "ubuntu", "ID_LIKE": "debian"}),
            mock.patch.object(connect, "get_iqns", side_effect=lookup),
            mock.patch.object(connect.time, "sleep"),
            mock.patch.object(connect.sys, "stdout", new_callable=io.StringIO),
            mock.patch.object(connect.sys, "stderr", new_callable=io.StringIO),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    @property
    def stdout(self):
        return connect.sys.stdout.getvalue()

    @property
    def stderr(self):
        return connect.sys.stderr.getvalue()

    def run_main(self, *extra, **kwargs):
        volumes = kwargs.get("volumes", ["volume1"])
        count = kwargs.get("count", 4)
        connect.main(["-g", "rg", "-e", "san", "-v", "vg", "-n"] + volumes + ["-s", str(count)] + list(extra))

    def updates(self, key):
        return [c for c in self.host.ran("sudo", "iscsiadm") if "update" in c and c[c.index("-n") + 1] == key]


class PlatformTests(HardeningTestCase):
    def test_missing_privileges_stop_before_any_other_command(self):
        self.host.sudo_allowed = False
        with self.assertRaisesRegex(connect.ConnectScriptError, "passwordless sudo"):
            self.run_main()
        self.assertEqual([["sudo", "-n", "true"]], self.host.commands)
        self.assertEqual([], self.lookups)

    def test_root_does_not_probe_sudo(self):
        with mock.patch.object(connect.os, "geteuid", create=True, return_value=0):
            connect.check_privileges()
        self.assertEqual([], self.host.commands)

    def test_distro_family_comes_from_id_then_id_like(self):
        for os_release, family in (
            ({"ID": "ubuntu", "ID_LIKE": "debian"}, "debian"),
            ({"ID": "debian"}, "debian"),
            ({"ID": "rhel", "ID_LIKE": "fedora"}, "rhel"),
            ({"ID": "rocky", "ID_LIKE": "rhel centos fedora"}, "rhel"),
            ({"ID": "ol", "ID_LIKE": "fedora"}, "rhel"),
            ({"ID": "fedora"}, "rhel"),
            ({"ID": "sles", "ID_LIKE": "suse"}, "suse"),
            ({"ID": "opensuse-leap", "ID_LIKE": "suse opensuse"}, "suse"),
            ({"ID": "azurelinux"}, "azurelinux"),
            ({"ID": "mariner"}, "azurelinux"),
        ):
            with self.subTest(os_release=os_release):
                with mock.patch.object(connect, "read_os_release", return_value=os_release):
                    self.assertEqual(family, connect.detect_distro())
        for os_release, name in (({"ID": "arch"}, "arch"), ({}, "unknown")):
            with mock.patch.object(connect, "read_os_release", return_value=os_release):
                with self.assertRaisesRegex(connect.ConnectScriptError, "Unsupported Linux distribution '{}'".format(name)):
                    connect.detect_distro()

    def test_missing_packages_are_installed_without_prompts(self):
        for family, has_dnf, expected in (
            ("debian", False, ["sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "-q",
                               "open-iscsi", "multipath-tools"]),
            ("rhel", True, ["sudo", "dnf", "install", "-y", "iscsi-initiator-utils", "device-mapper-multipath"]),
            ("rhel", False, ["sudo", "yum", "install", "-y", "iscsi-initiator-utils", "device-mapper-multipath"]),
            ("suse", False, ["sudo", "zypper", "--non-interactive", "install", "open-iscsi", "multipath-tools"]),
            ("azurelinux", False, ["sudo", "tdnf", "install", "-y", "iscsi-initiator-utils",
                                   "device-mapper-multipath"]),
        ):
            with self.subTest(family=family, has_dnf=has_dnf):
                self.host = host = FakeHost(family)
                host.packages.clear()
                if not has_dnf:
                    host.tools.discard("dnf")
                with mock.patch.object(connect.subprocess, "Popen", side_effect=host):
                    connect.ensure_prerequisites(family)
                self.assertIn(expected, host.commands)
                self.assertEqual(family == "debian", ["sudo", "apt-get", "update", "-q"] in host.commands)
                self.assertEqual(set(connect.PREREQUISITE_PACKAGES[family]), host.packages)

    def test_prerequisites_are_idempotent_and_never_start_the_login_unit(self):
        connect.ensure_prerequisites("debian")
        commands = self.host.commands
        for changing in ("apt-get", "env", "tee", "mkdir", "iscsi-iname", "mpathconf", "restart"):
            self.assertFalse(any(changing in command for command in commands), changing)
        self.assertIn(["sudo", "systemctl", "enable", "--now", "iscsid"], commands)
        self.assertIn(["sudo", "systemctl", "enable", "--now", "multipathd"], commands)
        # Starting the login unit would run --loginall=automatic outside this run's plan.
        self.assertEqual([["sudo", "systemctl", "enable", "open-iscsi"]],
                         [c for c in self.host.ran("sudo", "systemctl") if "open-iscsi" in c])

    def test_rhel_creates_missing_initiator_name_and_multipath_conf(self):
        self.host = host = FakeHost("rhel")
        host.files.clear()
        with mock.patch.object(connect.subprocess, "Popen", side_effect=host):
            connect.ensure_prerequisites("rhel")
        self.assertEqual("InitiatorName=iqn.2004-10.com.example:generated\n",
                         host.files[connect.INITIATOR_NAME_FILE])
        self.assertIn(["sudo", "systemctl", "restart", "iscsid"], host.commands)
        self.assertLess(host.index(lambda c: c[1:2] == ["mpathconf"]),
                        host.index(lambda c: c[-1] == "multipathd" and "--now" in c))
        self.assertIn(connect.MULTIPATH_CONF, host.files)

    def test_tools_are_found_outside_a_non_root_path(self):
        def which(name, path=None):
            return None if path is None else "/usr/sbin/" + name

        with mock.patch.object(connect.shutil, "which", side_effect=which) as probe:
            self.assertEqual("/usr/sbin/mpathconf", REAL_FIND_TOOL("mpathconf"))
        self.assertEqual([mock.call("mpathconf"), mock.call("mpathconf", path=connect.TOOL_SEARCH_PATH)],
                         probe.call_args_list)

    def test_missing_tools_stop_with_guidance_before_running_them(self):
        def missing_sudo(host):
            connect.check_privileges()

        def missing_iname(host):
            del host.files[connect.INITIATOR_NAME_FILE]
            connect.ensure_prerequisites("debian")

        def missing_mpathconf(host):
            del host.files[connect.MULTIPATH_CONF]
            connect.ensure_prerequisites("rhel")

        def missing_tdnf(host):
            host.packages.clear()
            connect.ensure_prerequisites("azurelinux")

        for tool, family, act in (("sudo", "debian", missing_sudo), ("iscsi-iname", "debian", missing_iname),
                                  ("mpathconf", "rhel", missing_mpathconf), ("tdnf", "azurelinux", missing_tdnf)):
            with self.subTest(tool=tool):
                self.host = FakeHost(family)
                self.host.tools.discard(tool)
                with mock.patch.object(connect.subprocess, "Popen", side_effect=self.host):
                    with self.assertRaisesRegex(connect.ConnectScriptError, "^{} was not found".format(tool)):
                        act(self.host)
                self.assertFalse(any(tool in command for command in self.host.commands))

    def test_missing_login_unit_warns(self):
        self.host.unit_files = {"iscsid", "multipathd"}
        self.run_main()
        self.assertIn("[WARN] iSCSI login unit: neither open-iscsi.service nor iscsi.service exists; "
                      "automatic node records may not log in at boot", self.stdout)
        self.assertEqual([], [c for c in self.host.ran("sudo", "systemctl", "enable") if "--now" not in c])

    def test_help_documents_the_opt_out_and_keeps_existing_arguments(self):
        parser = connect.create_argument_parser()
        self.assertIn("--skip-recommended-settings", parser.format_help())
        args = parser.parse_args(["--subscription", "sub", "-g", "rg", "-e", "san", "-v", "vg", "-n", "a", "b",
                                  "-s", "8", "--skip-recommended-settings"])
        self.assertEqual(("sub", "rg", "san", "vg", ["a", "b"], "8", True), (
            args.elastic_san_subscription, args.resource_group, args.elastic_san, args.volume_group,
            args.volumes, args.num_of_sessions, args.skip_recommended_settings))
        self.assertFalse(parser.parse_args([]).skip_recommended_settings)


class EntryPointTests(unittest.TestCase):
    def test_missing_arguments_print_one_error_line_without_traceback(self):
        result = subprocess.run([sys.executable, SCRIPT_PATH], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True)
        self.assertEqual(1, result.returncode)
        self.assertEqual("ERROR: Need to provide resource_group_name, elastic_san_name, volume_group_name, "
                         "volume_names to connect to the ElasticSAN volume\n", result.stderr)


class ConnectFlowTests(HardeningTestCase):
    def test_steps_run_in_order_and_every_volume_is_planned_before_any_change(self):
        self.host.multipath_defaults["find_multipaths"] = "strict"
        self.run_main(volumes=["volume1", "volume2"])
        first_enable = self.host.index(lambda c: c[1:3] == ["systemctl", "enable"])
        drop_in = self.host.index(lambda c: c[1:2] == ["tee"] and c[2].endswith(connect.MULTIPATH_DROP_IN_NAME))
        first_inventory = self.host.index(lambda c: c[1:] == ["iscsiadm", "-m", "session"])
        first_change = self.host.index(lambda c: c[-2:] == ["--op", "new"])
        registration = self.host.index(lambda c: c[1:3] == ["multipath", "-a"])
        validation = self.host.index(lambda c: c[1:2] == ["is-active"])
        self.assertEqual(["volume1", "volume2"], self.lookups)
        self.assertLess(first_enable, drop_in)
        self.assertLess(drop_in, first_inventory)
        self.assertEqual(2, len([c for c in self.host.commands[:first_change] if c[1:] == ["iscsiadm", "-m", "node"]]))
        self.assertLess(first_change, registration)
        self.assertLess(registration, validation)
        self.assertIn("Validation: ", self.stdout)
        self.assertNotIn("[FAIL]", self.stdout)

    def test_lookup_failure_stops_before_prerequisites_or_iscsi(self):
        connect.get_iqns.side_effect = [TARGETS["volume1"], Exception(b"ERROR: volume not found")]
        with self.assertRaisesRegex(connect.ConnectScriptError, "Volume 'volume2' lookup failed: ERROR: volume not found"):
            self.run_main(volumes=["volume1", "volume2"])
        self.assertEqual([["sudo", "-n", "true"]], self.host.commands)

    def test_recommended_values_precede_the_single_login_in_every_mode(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in ("10.0.0.1", "10.0.0.2", "10.0.0.3")]
        for zonal in (False, True):
            for vip in (False, True):
                with self.subTest(zonal=zonal, vip=vip):
                    self.host = FakeHost()
                    connect.sys.stdout.seek(0)
                    connect.sys.stdout.truncate()
                    flags = (["--enable-zonal-affinity"] if zonal else []) + (["--enable-vip-distribution"] if vip else [])
                    with mock.patch.object(connect.subprocess, "Popen", side_effect=self.host), \
                            mock.patch.object(connect, "resolve_zonal_affinity_context", return_value=("s", "eastus-az3")), \
                            mock.patch.object(connect, "get_mapped_volume_target", return_value=TARGETS["volume1"]), \
                            mock.patch.object(connect.socket, "getaddrinfo", return_value=addresses):
                        self.run_main(*flags, count=32)
                    target = iqn("volume1") + (":az-eastus-az3" if zonal else "")
                    logins = [c for c in self.host.commands if c[-1] == "--login"]
                    self.assertEqual(3 if vip else 1, len(logins))
                    for login in logins:
                        portal = login[login.index("--portal") + 1]
                        earlier = [c for c in self.host.commands[:self.host.commands.index(login)] if portal in c]
                        for key in DIGEST_KEYS + RECOMMENDED_KEYS:
                            self.assertTrue(any(key in c for c in earlier), (portal, key))
                    self.assertEqual({target}, {name for name, _ in self.host.nodes})
                    self.assertEqual(32, len(self.host.sessions))
                    self.assertIn("[PASS] volume1 [{}] persistent records: nr_sessions total 32 across {} node "
                                  "record(s), node.startup automatic (requested 32)".format(target, 3 if vip else 1),
                                  self.stdout)

    def test_opt_out_keeps_digests_but_skips_every_recommended_change(self):
        self.host.multipath_defaults["find_multipaths"] = "strict"
        with self.assertRaises(connect.ConnectScriptError):
            self.run_main("--skip-recommended-settings")
        for key in RECOMMENDED_KEYS:
            self.assertEqual([], self.updates(key), key)
        for key in DIGEST_KEYS:
            self.assertEqual(1, len(self.updates(key)), key)
        self.assertEqual([], self.host.ran("sudo", "tee"))
        self.assertEqual([], self.host.ran("sudo", "multipath", "-a"))
        self.assertNotIn("recommended settings", self.stdout)
        # Without WWID registration, strict find_multipaths leaves the volume unmapped.
        self.assertIn("[FAIL] volume1 [{}] multipath paths: no multipath map".format(iqn("volume1")), self.stdout)


class ExistingStateTests(HardeningTestCase):
    def connect_volumes(self, volumes, recommended=()):
        return connect.connect_volumes(None, "rg", "san", "vg", volumes, 4, recommended_settings=recommended)

    def test_any_record_or_session_for_the_iqn_skips_the_volume(self):
        other_portal = "10.1.2.3:3260"
        for description, arrange, message in (
            ("records and sessions on another portal, other case",
             lambda h: (h.nodes.update({(iqn("volume1").upper(), other_portal): connected_record(
                 **{"node.session.nr_sessions": "2"})}), h.add_session(iqn("volume1").upper(), other_portal),
                 h.add_session(iqn("volume1").upper(), other_portal)),
             "Skipped: already connected (2 live / 2 persistent)"),
            ("record only", lambda h: h.nodes.update({(iqn("volume1"), PORTAL): connected_record()}),
             "Skipped: persistent configuration exists but no live sessions; log in with "
             "'sudo iscsiadm -m node -T {} -p {} -l'".format(iqn("volume1"), PORTAL)),
            ("session only", lambda h: h.add_session(iqn("volume1")),
             "Skipped: already connected (1 live / 0 persistent); these sessions are not persistent"),
            ("zonal decoration", lambda h: h.nodes.update({(iqn("volume1") + ":az-eastus-az1", PORTAL): {}}),
             "Skipped: persistent configuration exists"),
        ):
            with self.subTest(description):
                self.host.nodes.clear()
                self.host.sessions[:] = []
                self.host.commands[:] = []
                arrange(self.host)
                before = len(self.host.sessions)
                outcomes = self.connect_volumes(["volume1"])
                self.assertIn("volume1 [{}]: {}".format(iqn("volume1"), message), self.stdout)
                self.assertFalse(outcomes[0].connected_this_run)
                self.assertEqual([], [c for c in self.host.commands if "--op" in c or "--login" in c])
                self.assertEqual(before, len(self.host.sessions))

    def test_iqn_is_never_matched_as_a_prefix_or_substring(self):
        # volume10 shares a prefix with volume1; "az-" in a volume name is not a zonal decoration.
        for selected, others in (("volume1", ["volume10"]), ("az-data", ["az-logs", "az-logs:az-eastus-az1"])):
            with self.subTest(selected=selected):
                self.host.nodes.clear()
                self.host.sessions[:] = []
                for other in others:
                    self.host.nodes[(iqn(other), PORTAL)] = connected_record()
                    self.host.add_session(iqn(other))
                outcomes = self.connect_volumes([selected])
                self.assertTrue(outcomes[0].connected_this_run)
                for other in others:
                    self.assertEqual(connected_record(), self.host.nodes[(iqn(other), PORTAL)])
                self.assertEqual(len(others) + 4, len(self.host.sessions))

    def test_volume_listed_twice_connects_once(self):
        self.connect_volumes(["volume1", "volume1"])
        self.assertEqual(1, len(self.host.ran("sudo", "iscsiadm", "-m", "node", "--targetname", iqn("volume1"),
                                              "--portal", PORTAL, "--op", "new")))
        self.assertIn("volume1 [{}]: Skipped: listed more than once".format(iqn("volume1")), self.stdout)

    def test_skipped_volume_gets_only_differing_recommended_values(self):
        for live, notice in ((True, True), (False, False)):
            with self.subTest(live=live):
                self.setUp()
                record = connected_record(**{
                    "node.session.iscsi.MaxBurstLength": "16776192",
                    "node.conn[0].timeo.login_timeout": "15",
                    "node.conn[0].iscsi.DataDigest": "None",
                    "node.session.nr_sessions": "8",
                })
                self.host.nodes[(iqn("volume1"), PORTAL)] = record
                if live:
                    for _ in range(8):
                        self.host.add_session(iqn("volume1"))
                self.run_main(count=8)
                self.assertEqual(["node.session.iscsi.MaxBurstLength", "node.conn[0].timeo.login_timeout"],
                                 [c[c.index("-n") + 1] for c in self.host.commands if "update" in c])
                self.assertEqual("None", record["node.conn[0].iscsi.DataDigest"])
                # Pre-existing problems on a skipped volume only warn.
                self.assertIn("[WARN] volume1 [{}] digests: not CRC32C for {}".format(iqn("volume1"), PORTAL), self.stdout)
                self.assertEqual(notice, "log out/in or reboot for updated iSCSI settings" in self.stdout)
                if not live:
                    self.assertIn("[WARN] volume1 [{0}] live sessions: 0 (requested 8); persistent configuration "
                                  "exists but no live sessions: log in with 'sudo iscsiadm -m node -T {0} -p {1} -l'".format(
                                      iqn("volume1"), PORTAL), self.stdout)


class FailureAndValidationTests(HardeningTestCase):
    def test_failed_first_login_removes_only_its_own_record_and_the_run_continues(self):
        targets = dict(TARGETS, volume1=(iqn("volume1"), "bad.example", 3260))
        connect.get_iqns.side_effect = lambda *args: targets[args[-1]]
        self.host.failed_logins.add("bad.example:3260")
        self.host.nodes[(iqn("volume10"), "bad.example:3260")] = connected_record()
        with self.assertRaisesRegex(connect.ConnectScriptError, "Validation found 2 failed check"):
            self.run_main(volumes=["volume1", "volume2"])
        self.assertNotIn((iqn("volume1"), "bad.example:3260"), self.host.nodes)
        self.assertIn((iqn("volume10"), "bad.example:3260"), self.host.nodes)
        self.assertIn((iqn("volume2"), PORTAL), self.host.nodes)
        self.assertIn("removed the node record created for portal bad.example:3260", self.stderr)
        self.assertIn("[FAIL] volume1 [{}] live sessions: 0 (requested 4); not connected, see the errors "
                      "above".format(iqn("volume1")), self.stdout)
        self.assertIn("[FAIL] volume1 [{}] persistent records: none".format(iqn("volume1")), self.stdout)
        self.assertIn("[PASS] volume2 [{}] live sessions: 4 (requested 4)".format(iqn("volume2")), self.stdout)

    def test_failed_login_keeps_the_record_if_a_session_appeared_or_sessions_are_unreadable(self):
        for flag, reason in (("login_error_with_session", "a new session for the target appeared"),
                             ("session_reads_fail_after_login", "the sessions could not be read")):
            with self.subTest(flag=flag):
                self.setUp()
                self.host.failed_logins.add(PORTAL)
                setattr(self.host, flag, True)
                with self.assertRaisesRegex(RuntimeError, "Login failed through every target portal"):
                    connect.connect_volume("volume1", iqn("volume1"), [(HOST, 4)], 3260)
                self.assertIn((iqn("volume1"), PORTAL), self.host.nodes)
                self.assertEqual([], [c for c in self.host.commands if c[-2:] == ["--op", "delete"]])
                self.assertIn("kept the node record created by this run because " + reason, self.stderr)

    def test_failed_tuning_update_on_a_skipped_volume_warns_and_keeps_the_notice(self):
        self.host.nodes[(iqn("volume1"), PORTAL)] = connected_record(**{
            "node.session.iscsi.MaxBurstLength": "16776192",
            "node.conn[0].timeo.login_timeout": "15",
            "node.session.nr_sessions": "4",
        })
        for _ in range(4):
            self.host.add_session(iqn("volume1"))
        self.host.failed_updates.add("node.session.iscsi.MaxBurstLength")
        self.run_main()
        self.assertIn("Warning: volume1 [{}], portal {}: setting node.session.iscsi.MaxBurstLength failed: "
                      "iscsiadm".format(iqn("volume1"), PORTAL), self.stderr)
        self.assertEqual("30", self.host.nodes[(iqn("volume1"), PORTAL)]["node.conn[0].timeo.login_timeout"])
        self.assertIn("log out/in or reboot for updated iSCSI settings", self.stdout)
        self.assertIn("[WARN] volume1 [{}] recommended settings: differ for {}".format(iqn("volume1"), PORTAL),
                      self.stdout)

    def test_same_shortfall_fails_when_connected_and_warns_when_skipped(self):
        self.host.fail_clones = True
        with self.assertRaises(connect.ConnectScriptError):
            self.run_main()
        self.assertIn("[FAIL] volume1 [{}] live sessions: 1 (requested 4)".format(iqn("volume1")), self.stdout)

        self.setUp()
        self.host.nodes[(iqn("volume1"), PORTAL)] = connected_record(**{"node.session.nr_sessions": "1"})
        self.host.add_session(iqn("volume1"))
        self.run_main(volumes=["volume1"])
        self.assertIn("[WARN] volume1 [{}] live sessions: 1 (requested 4)".format(iqn("volume1")), self.stdout)
        self.assertIn("Validation: ", self.stdout)
        self.assertNotIn("[FAIL]", self.stdout)

    def test_counts_above_requested_only_warn(self):
        self.host.nodes[(iqn("volume1"), PORTAL)] = connected_record()
        for _ in range(32):
            self.host.add_session(iqn("volume1"))
        self.run_main(count=4)
        self.assertIn("[WARN] volume1 [{}] live sessions: 32 (requested 4)".format(iqn("volume1")), self.stdout)
        self.assertIn("[WARN] volume1 [{}] persistent records: nr_sessions total 32 across 1 node record(s), "
                      "node.startup automatic (requested 4)".format(iqn("volume1")), self.stdout)

    def test_check_names_match_the_shared_linux_validation_contract(self):
        self.run_main()
        labels = [line.split("] ", 1)[1].split(": ", 1)[0] for line in self.stdout.splitlines()
                  if line.startswith(("[PASS]", "[WARN]", "[FAIL]"))]
        volume = "volume1 [{}] ".format(iqn("volume1"))
        self.assertEqual(
            ["iscsid service", "multipathd service", "iSCSI login unit", "multipath drop-in", "multipath defaults"]
            + [volume + check for check in ("live sessions", "persistent records", "digests",
                                            "recommended settings", "multipath paths")],
            labels,
        )
        self.assertIn("Validation: 10 passed, 0 warnings, 0 failed", self.stdout)

    def test_digests_are_validated_from_the_node_record(self):
        self.host.negotiated_data_digest = "None"
        self.run_main()
        self.assertIn("[PASS] volume1 [{}] digests: CRC32C in the node records; negotiated header/data: "
                      "CRC32C/None".format(iqn("volume1")), self.stdout)


class MultipathTests(HardeningTestCase):
    def test_drop_in_is_device_scoped_idempotent_and_uses_config_dir(self):
        self.host.files[connect.MULTIPATH_CONF] = 'defaults {\n    config_dir "/etc/custom/multipath/"\n}\n'
        original = self.host.files[connect.MULTIPATH_CONF]
        connect.configure_multipath()
        content = self.host.files["/etc/custom/multipath/azure-elastic-san.conf"]
        self.assertIn('vendor "MSFT"', content)
        self.assertIn('product "Virtual HD"', content)
        self.assertNotIn("defaults", content.replace("global multipath defaults", ""))
        self.assertEqual(original, self.host.files[connect.MULTIPATH_CONF])
        self.assertEqual(1, len(self.host.ran("sudo", "multipathd", "reconfigure")))
        self.host.commands[:] = []
        connect.configure_multipath()
        self.assertEqual([], self.host.ran("sudo", "tee"))
        self.assertEqual([], self.host.ran("sudo", "multipathd", "reconfigure"))

    def test_strict_mode_registers_only_volumes_connected_in_this_run(self):
        self.host.multipath_defaults["find_multipaths"] = "strict"
        self.host.nodes[(iqn("volume2"), PORTAL)] = connected_record(**{"node.session.nr_sessions": "4"})
        for _ in range(4):
            self.host.add_session(iqn("volume2"))
        self.run_main(volumes=["volume1", "volume2"])
        self.assertEqual({iqn("volume1")}, self.host.registered)
        self.assertIn("[PASS] volume1 [{}] multipath paths: map map-volume1 with 4 active paths".format(
            iqn("volume1")), self.stdout)
        self.assertIn("[WARN] volume2 [{}] multipath paths: no multipath map".format(iqn("volume2")), self.stdout)
        self.assertIn("[WARN] multipath defaults: find_multipaths is strict (documented: yes)", self.stdout)


if __name__ == "__main__":
    unittest.main()
