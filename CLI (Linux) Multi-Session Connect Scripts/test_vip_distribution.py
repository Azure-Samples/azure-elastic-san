import copy
import io
import socket
import unittest
from unittest import mock

from test_connect_for_documentation import connect


IQN = "iqn.example:volume"
HOST = "volume.example"
VIPS = ["10.0.0.1", "10.0.0.2", "10.0.0.10"]


def answers(addresses):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in addresses]


class NativeIscsi:
    """Offline argv model: login honors nr_sessions; redirected endpoints coincide."""

    def __init__(self):
        self.nodes, self.sessions = {}, {}
        self.commands, self.mutations, self.logins = [], [], []
        self.failed_portals = set()
        self.fail_clones = False
        self.hide_sessions = False

    def add_session(self, iqn, portal):
        sid = str(101 + len(self.sessions) * 13)
        self.sessions[sid] = (iqn, portal)
        return sid

    def __call__(self, command, **kwargs):
        assert command[:2] == ["sudo", "iscsiadm"], command
        assert not kwargs.get("shell")
        self.commands.append(command)
        args = command[2:]

        def result(out="", err="", code=0):
            process = mock.Mock(returncode=code)
            process.communicate.return_value = (out.encode(), err.encode())
            return process

        if args == ["-m", "session"]:
            if not self.sessions or self.hide_sessions:
                return result(err="iscsiadm: No active sessions.", code=21)
            return result("\n".join(
                "tcp:\t[{}] 192.0.2.50:3260,7 {} (non-flash)".format(sid, iqn)
                for sid, (iqn, _) in self.sessions.items()
            ))
        if args == ["-m", "node"]:
            if not self.nodes:
                return result(err="iscsiadm: No records found", code=21)
            return result("\n".join("{}:3260,-1 {}".format(portal, iqn) for iqn, portal in self.nodes))
        if args[:2] == ["-m", "node"] and "--op" not in args and "--login" not in args:
            # The pre-mutation plan reads existing records for persistent counts.
            key = (args[args.index("--targetname") + 1], args[args.index("--portal") + 1].rsplit(":", 1)[0])
            return result("\n".join("{} = {}".format(name, value) for name, value in self.nodes[key].items()))

        self.mutations.append(command)
        if args[:2] == ["-m", "session"]:
            assert args[-2:] == ["--op", "new"]
            if self.fail_clones:
                return result(err="clone failed", code=15)
            self.add_session(*self.sessions[args[3]])
            return result()
        iqn = args[args.index("--targetname") + 1]
        portal = args[args.index("--portal") + 1].rsplit(":", 1)[0]
        key = (iqn, portal)
        if args[-2:] == ["--op", "new"]:
            assert key not in self.nodes, "Must not replace an existing node"
            self.nodes[key] = {"node.session.nr_sessions": "8"}
        elif args[-2:] == ["--op", "delete"]:
            # A record created by a failed first login is removed so a re-run can connect.
            del self.nodes[key]
        elif args[-1] == "--login":
            self.logins.append(portal)
            if portal in self.failed_portals:
                return result(err="login failed", code=15)
            for _ in range(int(self.nodes[key]["node.session.nr_sessions"])):
                self.add_session(iqn, portal)
        else:
            assert args[args.index("--op") + 1] == "update"
            self.nodes[key][args[args.index("-n") + 1]] = args[args.index("-v") + 1]
        return result()


class PortalSelectionTests(unittest.TestCase):
    def test_zero_one_two_three_and_deduplicated_numeric_order(self):
        for addresses, count, expected in (
            ([VIPS[0]], 32, [(HOST, 32)]),
            ([VIPS[0], VIPS[0]], 4, [(HOST, 4)]),
            (VIPS[:2], 32, [(VIPS[0], 16), (VIPS[1], 16)]),
            ([VIPS[2], VIPS[1], VIPS[0], VIPS[1]], 32, list(zip(VIPS, [11, 11, 10]))),
            (VIPS, 1, list(zip(VIPS, [1, 0, 0]))),
        ):
            with self.subTest(addresses=addresses, count=count):
                with mock.patch.object(connect.socket, "getaddrinfo", return_value=answers(addresses)) as lookup:
                    self.assertEqual(expected, connect.get_portals(HOST, count, True))
                lookup.assert_called_once_with(HOST, None, socket.AF_INET, socket.SOCK_STREAM)
        with mock.patch.object(connect.socket, "getaddrinfo", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "DNS.*no IPv4.*volume.example"):
                connect.get_portals(HOST, 32, True)

    def test_dns_error_is_explicit_and_disabled_mode_does_not_resolve(self):
        with mock.patch.object(connect.socket, "getaddrinfo", side_effect=socket.gaierror("unavailable")) as lookup:
            self.assertEqual([(HOST, 4)], connect.get_portals(HOST, 4))
            lookup.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "DNS.*unavailable"):
                connect.get_portals(HOST, 4, True)


class VipConnectionTests(unittest.TestCase):
    def setUp(self):
        self.native = NativeIscsi()
        patches = (
            mock.patch.object(connect.subprocess, "Popen", side_effect=self.native),
            mock.patch.object(connect, "get_iqns", return_value=(IQN, HOST, 3260)),
            mock.patch.object(connect, "get_mapped_volume_target", return_value=(IQN, HOST, 3260)),
            mock.patch.object(connect, "resolve_zonal_affinity_context", return_value=("sub-id", "eastus-az3")),
            mock.patch.object(connect.socket, "getaddrinfo", return_value=answers(VIPS)),
            mock.patch.object(connect.sys, "stdout", new_callable=io.StringIO),
            mock.patch.object(connect.sys, "stderr", new_callable=io.StringIO),
        )
        mocks = []
        for patcher in patches:
            mocks.append(patcher.start())
            self.addCleanup(patcher.stop)
        self.popen, self.legacy, self.mapped, self.mapping, self.dns, self.stdout, self.stderr = mocks

    def run_connect(self, count=32, zonal=False, vip=True):
        connect.connect_volumes(None, "rg", "san", "vg", ["volume"], count, zonal, vip)

    def test_original_portal_counts_and_seed_specific_clones(self):
        self.run_connect()
        self.assertEqual(VIPS, self.native.logins)
        self.assertEqual([11, 11, 10], [
            sum(portal == host for _, portal in self.native.sessions.values()) for host in VIPS
        ])
        self.assertEqual(["11", "11", "10"], [
            self.native.nodes[(IQN, host)]["node.session.nr_sessions"] for host in VIPS
        ])
        for host in VIPS:
            node = self.native.nodes[(IQN, host)]
            self.assertEqual("automatic", node["node.startup"])
            self.assertEqual("CRC32C", node["node.conn[0].iscsi.HeaderDigest"])
            self.assertEqual("CRC32C", node["node.conn[0].iscsi.DataDigest"])
        clone_commands = [c for c in self.native.mutations if c[2:4] == ["-m", "session"]]
        seeds = [next(sid for sid, value in self.native.sessions.items() if value == (IQN, host)) for host in VIPS]
        self.assertEqual([seeds[0]] * 10 + [seeds[1]] * 10 + [seeds[2]] * 9,
                         [c[c.index("-r") + 1] for c in clone_commands])
        for login in [c for c in self.native.commands if "--login" in c]:
            earlier = self.native.commands[:self.native.commands.index(login)]
            portal = login[login.index("--portal") + 1]
            for name in ("node.startup", "node.conn[0].iscsi.HeaderDigest", "node.conn[0].iscsi.DataDigest"):
                self.assertTrue(any(name in c and portal in c for c in earlier))
        self.assertNotIn("verified", self.stdout.getvalue().lower())

    def test_single_address_retains_fqdn(self):
        self.dns.return_value = answers([VIPS[0]])
        self.run_connect(count=3)
        self.assertEqual([HOST], self.native.logins)
        self.assertEqual({(IQN, HOST)}, set(self.native.nodes))
        self.assertEqual(3, len(self.native.sessions))

    def test_one_vip_failure_warns_and_continues(self):
        self.native.failed_portals.add(VIPS[0])
        self.run_connect()
        self.assertEqual(VIPS, self.native.logins)
        self.assertEqual(21, len(self.native.sessions))
        self.assertIn("10.0.0.1:3260", self.stderr.getvalue())
        self.assertIn("login failed", self.stderr.getvalue())

    def test_all_portals_failing_is_reported_after_trying_every_address(self):
        # A failed volume is reported inline and left to validation instead of
        # stopping the remaining volumes.
        self.native.failed_portals.update(VIPS)
        outcomes = connect.connect_volumes(None, "rg", "san", "vg", ["volume"], 32, False, True)
        self.assertTrue(outcomes[0].failed)
        self.assertIn("Login failed through every target portal", self.stderr.getvalue())
        self.assertEqual(VIPS, self.native.logins)
        self.assertEqual(3, self.stderr.getvalue().count("Warning:"))
        self.assertEqual({}, self.native.nodes)

    def test_one_session_uses_spare_addresses_only_after_allocated_login_failure(self):
        self.run_connect(count=1)
        self.assertEqual([VIPS[0]], self.native.logins)
        self.assertEqual(1, len(self.native.nodes))
        # Exercise the alternative on an empty machine, not an existing layout.
        self.native.nodes.clear()
        self.native.sessions.clear()
        self.native.logins.clear()
        self.native.failed_portals.add(VIPS[0])
        self.run_connect(count=1)
        self.assertEqual(VIPS[:2], self.native.logins)
        self.assertEqual(1, len(self.native.sessions))
        self.assertEqual("1", self.native.nodes[(IQN, VIPS[1])]["node.session.nr_sessions"])

    def test_all_spare_addresses_are_tried_when_they_also_fail(self):
        self.native.failed_portals.update(VIPS)
        outcomes = connect.connect_volumes(None, "rg", "san", "vg", ["volume"], 1, False, True)
        self.assertTrue(outcomes[0].failed)
        self.assertEqual(VIPS, self.native.logins)

    def test_dns_failure_never_creates_nodes_or_sessions(self):
        self.dns.return_value = []
        with self.assertRaisesRegex(RuntimeError, "DNS"):
            self.run_connect()
        self.assertEqual([], self.native.mutations)

    def test_existing_plain_or_decorated_session_or_node_is_skipped(self):
        for iqn in (IQN, IQN + ":az-eastus-az3", IQN + ":az-westus-az1"):
            for mode in ("session", "node"):
                with self.subTest(iqn=iqn, mode=mode):
                    self.native.nodes.clear()
                    self.native.sessions.clear()
                    if mode == "session":
                        self.native.add_session(iqn, "old.example")
                    else:
                        self.native.nodes[(iqn, "old.example")] = {}
                    self.run_connect()
        self.assertEqual([], self.native.mutations)
        # VIP DNS is part of the read-only lookup that completes before any change.
        self.assertEqual(6, self.dns.call_count)
        self.assertIn("Skipped: already connected (1 live / 0 persistent)", self.stdout.getvalue())
        self.assertIn("Skipped: persistent configuration exists but no live sessions", self.stdout.getvalue())

    def test_unrelated_target_is_preserved_and_not_used_as_seed(self):
        sid = self.native.add_session(IQN + "-other", "old.example")
        self.native.nodes[(IQN + "-other", "old.example")] = {}
        old = copy.deepcopy(self.native.nodes)
        self.run_connect(count=4)
        self.assertEqual(5, len(self.native.sessions))
        self.assertEqual(old[(IQN + "-other", "old.example")],
                         self.native.nodes[(IQN + "-other", "old.example")])
        self.assertFalse(any(sid in command for command in self.native.mutations))

    def test_both_opt_ins_are_independent(self):
        for zonal in (False, True):
            for vip in (False, True):
                with self.subTest(zonal=zonal, vip=vip):
                    self.native.nodes.clear()
                    self.native.sessions.clear()
                    self.native.logins.clear()
                    self.mapping.reset_mock()
                    self.dns.reset_mock()
                    self.run_connect(count=4, zonal=zonal, vip=vip)
                    target = IQN + ":az-eastus-az3" if zonal else IQN
                    self.assertEqual({target}, {iqn for iqn, _ in self.native.nodes})
                    self.assertEqual(zonal, self.mapping.called)
                    self.assertEqual(vip, self.dns.called)
                    self.assertEqual(VIPS if vip else [HOST], self.native.logins)
                    self.assertEqual(4, len(self.native.sessions))

    def test_clone_failure_keeps_seed_connection_and_warns(self):
        self.native.fail_clones = True
        self.run_connect()
        self.assertEqual(3, len(self.native.sessions))
        self.assertIn("clone failed", self.stderr.getvalue())

    def test_missing_seed_sid_is_not_guessed_from_a_redirected_endpoint(self):
        self.native.hide_sessions = True
        self.run_connect()
        self.assertEqual(3, len(self.native.sessions))
        self.assertIn("Cannot identify a single new seed SID", self.stderr.getvalue())
        self.assertFalse(any(c[2:4] == ["-m", "session"] for c in self.native.mutations))

    def test_unexpected_inventory_error_is_not_treated_as_empty(self):
        process = mock.Mock(returncode=1)
        process.communicate.return_value = (b"", b"permission denied")
        self.popen.side_effect = None
        self.popen.return_value = process
        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            self.run_connect()
        self.assertEqual([], self.native.mutations)

    def test_nonpositive_count_is_rejected_before_discovery(self):
        for count in (0, -1):
            with self.subTest(count=count):
                with self.assertRaisesRegex(ValueError, "at least 1"):
                    self.run_connect(count=count)
        self.legacy.assert_not_called()
        self.popen.assert_not_called()


class ArgumentTests(unittest.TestCase):
    def test_flags_and_normal_session_counts(self):
        for zonal in (False, True):
            for vip in (False, True):
                for requested, expected in (("1", 1), ("31", 31), ("32", 32), ("64", 32)):
                    with self.subTest(zonal=zonal, vip=vip, requested=requested):
                        args = ["-g", "rg", "-e", "san", "-v", "vg", "-n", "volume", "-s", requested]
                        if zonal:
                            args.append("--enable-zonal-affinity")
                        if vip:
                            args.append("--enable-vip-distribution")
                        with mock.patch.object(connect, "check_privileges"), \
                                mock.patch.object(connect, "detect_distro"), \
                                mock.patch.object(connect, "finish_connections"):
                            with mock.patch.object(connect, "connect_volumes") as run:
                                connect.main(args)
                        self.assertEqual((expected, zonal, vip), run.call_args.args[-3:])


if __name__ == "__main__":
    unittest.main()
