import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# The disconnect script imports its sibling connect script by module name, as it does
# when run from this directory.
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
SPEC = importlib.util.spec_from_file_location(
    "freebsd_disconnect_for_documentation",
    os.path.join(SCRIPT_DIR, "disconnect_for_documentation.py"),
)
disconnect = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(disconnect)
connect = disconnect.connect

if not hasattr(os, "chown"):
    os.chown = lambda *_args, **_kwargs: None


class FakeFcntl(object):
    LOCK_EX = 2
    LOCK_UN = 8

    def flock(self, _fd, _operation):
        pass


def entry(nickname, target_name, address="portal.example:3260"):
    return connect.ManagedConfigEntry(nickname, target_name, address)


def session(target_name, state="Connected"):
    return {"Target name": target_name, "Target portal": "10.0.0.1:3260", "Session state": state}


class DisconnectVolumesTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tempdir, "iscsi.conf")
        self.device_node = os.path.join(self.tempdir, "iscsi")
        open(self.device_node, "wb").close()
        self.patches = [
            mock.patch.object(connect, "fcntl", FakeFcntl()),
            mock.patch.object(connect, "ISCSI_DEVICE_NODE", self.device_node),
        ]
        for patch in self.patches:
            patch.start()
        self.output = io.StringIO()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        for name in os.listdir(self.tempdir):
            os.remove(os.path.join(self.tempdir, name))
        os.rmdir(self.tempdir)

    def write_config(self, entries, prefix=b"# operator stanza\nmanual { TargetName = x }\n"):
        content = prefix + connect.render_managed_block(entries)
        with open(self.config_path, "wb") as handle:
            handle.write(content)
        return content

    def read_config(self):
        with open(self.config_path, "rb") as handle:
            return handle.read()

    def run_disconnect(self, volumes, sessions, run_command=None):
        with mock.patch.object(connect, "list_iscsi_sessions", return_value=sessions) as listing:
            with mock.patch.object(connect, "_run_command", run_command or mock.Mock()) as run:
                with contextlib.redirect_stdout(self.output):
                    disconnect.disconnect_volumes(volumes, self.config_path)
        return listing, run

    def test_removes_plain_and_zonal_sessions_then_their_entries_only(self):
        kept = entry("esan-vg-vol10-s1", "iqn.example:vol10")
        self.write_config([
            entry("esan-vg-vol1-s1", "iqn.example:vol1", "10.0.0.1:3260"),
            entry("esan-vg-vol1-s2", "iqn.example:vol1", "10.0.0.2:3260"),
            entry("esan-vg-vol1-zonal-s1", "iqn.example:vol1:az-eastus-az1"),
            kept,
        ])
        sessions = [
            session("iqn.example:vol1"),
            session("iqn.example:vol1"),
            session("iqn.example:vol1:az-eastus-az1", state="Disconnected"),
            session("iqn.example:vol10"),
        ]
        _, run = self.run_disconnect([("vol1", "IQN.example:vol1")], sessions)

        self.assertEqual(
            [["iscsictl", "-R", "-t", "iqn.example:vol1"],
             ["iscsictl", "-R", "-t", "iqn.example:vol1:az-eastus-az1"]],
            [call.args[0] for call in run.call_args_list],
        )
        content = self.read_config()
        self.assertTrue(content.startswith(b"# operator stanza\nmanual { TargetName = x }\n"))
        self.assertEqual(["esan-vg-vol10-s1"], [e.nickname for e in connect.read_managed_entries(content)])
        self.assertIn("Removed 3 session(s) and 3 managed entries", self.output.getvalue())
        backups = [name for name in os.listdir(self.tempdir) if ".bak.pre-esan-disconnect." in name]
        self.assertEqual(1, len(backups))

    def test_removing_the_last_entries_removes_the_markers(self):
        prefix = b"# operator stanza\n"
        self.write_config([entry("esan-vg-vol1-s1", "iqn.example:vol1")], prefix=prefix)
        self.run_disconnect([("vol1", "iqn.example:vol1")], [])
        self.assertEqual(prefix, self.read_config())

    def test_without_device_node_sessions_are_not_listed_but_entries_are_removed(self):
        os.remove(self.device_node)
        self.write_config([entry("esan-vg-vol1-s1", "iqn.example:vol1")])
        listing, run = self.run_disconnect([("vol1", "iqn.example:vol1")], [])
        listing.assert_not_called()
        run.assert_not_called()
        self.assertEqual([], connect.read_managed_entries(self.read_config()))

    def test_not_connected_volume_is_skipped_without_writing(self):
        original = self.write_config([entry("esan-vg-vol2-s1", "iqn.example:vol2")])
        _, run = self.run_disconnect([("vol1", "iqn.example:vol1")], [session("iqn.example:vol2")])
        run.assert_not_called()
        self.assertEqual(original, self.read_config())
        self.assertIn("Skipped as this volume is not connected", self.output.getvalue())
        self.assertFalse(any(".bak." in name for name in os.listdir(self.tempdir)))

    def test_malformed_block_stops_before_any_removal(self):
        with open(self.config_path, "wb") as handle:
            handle.write(
                connect.MANAGED_BLOCK_BEGIN.encode("utf-8") + b"\nunknown\n"
                + connect.MANAGED_BLOCK_END.encode("utf-8") + b"\n"
            )
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "unknown"):
            self.run_disconnect([("vol1", "iqn.example:vol1")], [session("iqn.example:vol1")])

    def test_session_removal_failure_keeps_the_volume_entries(self):
        original = self.write_config([entry("esan-vg-vol1-s1", "iqn.example:vol1")])
        failing = mock.Mock(side_effect=connect.ElasticSanConnectError("ESRCH"))
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "ESRCH"):
            self.run_disconnect([("vol1", "iqn.example:vol1")], [session("iqn.example:vol1")], failing)
        self.assertEqual(original, self.read_config())


class DisconnectMainTests(unittest.TestCase):
    ARGV = ["-g", "rg", "-e", "san", "-v", "vg", "-n", "vol1", "VOL1", "vol2"]

    def run_main(self, answers):
        targets = {
            "vol1": ("iqn.example:vol1", "portal.example", 3260),
            "VOL1": ("IQN.example:VOL1", "portal.example", 3260),
            "vol2": ("iqn.example:vol2", "portal.example", 3260),
        }
        read_input = mock.Mock(side_effect=answers)
        output = io.StringIO()
        with mock.patch.object(disconnect, "check_disconnect_prerequisites"):
            with mock.patch.object(connect, "check_az_cli_available"):
                with mock.patch.object(
                    connect, "get_volume_storage_target",
                    side_effect=lambda *args: targets[args[-1]],
                ) as lookup:
                    with mock.patch.object(disconnect, "disconnect_volumes") as remove:
                        with contextlib.redirect_stdout(output):
                            code = disconnect.main(self.ARGV, read_input)
        return code, lookup, remove, read_input, output.getvalue()

    def test_lookups_finish_before_the_prompt_and_each_target_is_disconnected_once(self):
        code, lookup, remove, read_input, output = self.run_main(["maybe", "Yes"])
        self.assertEqual(0, code)
        self.assertEqual(3, lookup.call_count)
        self.assertEqual(2, read_input.call_count)
        remove.assert_called_once_with(
            [("vol1", "iqn.example:vol1"), ("vol2", "iqn.example:vol2")], "/etc/iscsi.conf"
        )
        self.assertIn("VOL1 [IQN.example:VOL1]: Ignored duplicate selection", output)

    def test_no_or_end_of_input_cancels_without_changes(self):
        for answers in (["n"], EOFError()):
            with self.subTest(answers=answers):
                code, _, remove, _, output = self.run_main(answers)
                self.assertEqual(1, code)
                remove.assert_not_called()
                self.assertIn("Cancelled; nothing was changed.", output)

    def test_missing_arguments_stop_before_any_check(self):
        with mock.patch.object(disconnect, "check_disconnect_prerequisites") as prerequisites:
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "Need to provide"):
                disconnect.main(["-g", "rg"])
        prerequisites.assert_not_called()

    def test_prerequisites_require_root_freebsd_and_iscsictl(self):
        uname = mock.Mock(return_value=mock.Mock(sysname="FreeBSD"))
        cases = (
            ({"geteuid": 1000}, "must be run as root"),
            ({"sysname": "Linux"}, "FreeBSD only"),
            ({"which": None}, "iscsictl"),
        )
        for overrides, expected in cases:
            with self.subTest(expected=expected):
                uname.return_value = mock.Mock(sysname=overrides.get("sysname", "FreeBSD"))
                with mock.patch.object(disconnect.os, "geteuid", create=True,
                                       return_value=overrides.get("geteuid", 0)):
                    with mock.patch.object(disconnect.os, "uname", uname, create=True):
                        with mock.patch.object(disconnect.shutil, "which",
                                               return_value=overrides.get("which", "/sbin/iscsictl")):
                            with self.assertRaisesRegex(connect.ElasticSanConnectError, expected):
                                disconnect.check_disconnect_prerequisites("/etc/iscsi.conf")


if __name__ == "__main__":
    unittest.main()
