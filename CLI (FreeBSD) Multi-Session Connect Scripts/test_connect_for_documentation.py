import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "connect_for_documentation.py")
SPEC = importlib.util.spec_from_file_location("freebsd_connect_for_documentation", SCRIPT_PATH)
connect = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(connect)

# The script targets FreeBSD/POSIX, where os.chown always exists. This test
# suite is also run on non-POSIX developer machines (e.g. Windows), which
# have no os.chown at all. Rather than weaken the production ownership-
# preservation behavior, shim in a no-op chown purely for the integration
# tests below that exercise the real filesystem end-to-end -- on FreeBSD,
# chowning a file back to its own current owner (the only thing these tests
# do) is always a no-op success anyway.
if not hasattr(os, "chown"):
    os.chown = lambda *_args, **_kwargs: None

def locations_payload(region_name="eastus", mappings=None):
    if mappings is None:
        mappings = [{"logicalZone": "2", "physicalZone": "eastus-az3"}]
    return json.dumps(
        {
            "value": [
                {
                    "name": region_name,
                    "metadata": {"regionType": "Physical"},
                    "availabilityZoneMappings": mappings,
                }
            ]
        }
    )


def verbose_session_block(target_name, target_portal, state="Connected"):
    return (
        "Session ID:               1\n"
        "Initiator name:            iqn.1994-09.org.freebsd:host\n"
        "Initiator portal:          0.0.0.0\n"
        "Initiator alias:           \n"
        "Target name:               {target_name}\n"
        "Target portal:             {target_portal}\n"
        "Target alias:              \n"
        "User:                      \n"
        "Secret:                    \n"
        "Mutual user:               \n"
        "Mutual secret:             \n"
        "Session type:              Normal\n"
        "Enable:                    Yes\n"
        "Session state:             {state}\n"
        "Failure reason:            \n"
        "Header digest:             CRC32C\n"
        "Data digest:               CRC32C\n"
    ).format(target_name=target_name, target_portal=target_portal, state=state)


class ArgumentParsingTests(unittest.TestCase):
    def test_elastic_san_subscription_options_parse_identically(self):
        parser = connect.create_argument_parser()
        preferred = parser.parse_args(["--elastic-san-subscription", "san-subscription"])
        compatibility = parser.parse_args(["--subscription", "san-subscription"])

        self.assertEqual("san-subscription", preferred.elastic_san_subscription)
        self.assertEqual(
            preferred.elastic_san_subscription, compatibility.elastic_san_subscription
        )
        self.assertFalse(hasattr(preferred, "subscription"))

    def test_short_and_long_flags_parse_to_expected_dests(self):
        parser = connect.create_argument_parser()
        args = parser.parse_args(
            [
                "-g",
                "rg",
                "-e",
                "san",
                "-v",
                "vg",
                "-n",
                "vol1",
                "vol2",
                "-s",
                "1",
                "--enable-zonal-affinity",
                "--dry-run",
            ]
        )
        self.assertEqual("rg", args.resource_group)
        self.assertEqual("san", args.elastic_san)
        self.assertEqual("vg", args.volume_group)
        self.assertEqual(["vol1", "vol2"], args.volumes)
        self.assertEqual("1", args.num_of_sessions)
        self.assertTrue(args.enable_zonal_affinity)
        self.assertTrue(args.dry_run)

    def test_dry_run_and_opt_ins_default_to_false_and_sessions_to_32(self):
        parser = connect.create_argument_parser()
        args = parser.parse_args(["-g", "rg", "-e", "san", "-v", "vg", "-n", "vol1"])
        self.assertFalse(args.dry_run)
        self.assertFalse(args.enable_zonal_affinity)
        self.assertFalse(args.skip_recommended_settings)
        self.assertFalse(args.enable_vip_distribution)
        self.assertEqual("32", args.num_of_sessions)

    def test_skip_recommended_settings_is_accepted_for_cli_parity(self):
        parser = connect.create_argument_parser()
        args = parser.parse_args(["--skip-recommended-settings"])
        self.assertTrue(args.skip_recommended_settings)

    def test_missing_required_arguments_raise_before_any_azure_or_freebsd_call(self):
        with mock.patch.object(connect, "check_az_cli_available") as check_az:
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "Need to provide"):
                connect.main(["-g", "rg"])
        check_az.assert_not_called()

    def test_session_count_outside_1_to_32_is_rejected_before_discovery(self):
        for value in ("0", "-1", "33", "not-a-number"):
            with self.subTest(value=value):
                with mock.patch.object(connect, "check_az_cli_available") as check_az:
                    with self.assertRaisesRegex(
                        connect.ElasticSanConnectError, "must be an integer from 1 to 32"
                    ):
                        connect.main(
                            ["-g", "rg", "-e", "san", "-v", "vg", "-n", "vol1", "-s", value]
                        )
                check_az.assert_not_called()

    def test_session_counts_1_to_32_reach_planning(self):
        for value in ("1", "2", "24", "32"):
            with self.subTest(value=value):
                with mock.patch.object(connect, "check_az_cli_available"):
                    with mock.patch.object(
                        connect, "build_connection_plan", side_effect=RuntimeError("planned")
                    ) as plan:
                        with self.assertRaisesRegex(RuntimeError, "planned"):
                            connect.main(
                                ["-g", "rg", "-e", "san", "-v", "vg", "-n", "vol1", "-s", value,
                                 "--dry-run"]
                            )
                self.assertEqual(int(value), plan.call_args[0][5])


class ZonalAffinityTests(unittest.TestCase):
    def test_imds_query_bypasses_proxy_uses_metadata_header_and_timeout(self):
        response = mock.Mock()
        response.read.return_value = json.dumps(
            {
                "zone": "2",
                "subscriptionId": "00000000-0000-0000-0000-000000000001",
                "location": "eastus",
            }
        ).encode("utf-8")
        opener = mock.Mock()
        opener.open.return_value = response
        proxy_handler = object()

        with mock.patch.object(connect, "ProxyHandler", return_value=proxy_handler) as proxy:
            with mock.patch.object(connect, "build_opener", return_value=opener) as build:
                compute = connect.get_vm_compute_metadata()

        self.assertEqual("2", compute["zone"])
        proxy.assert_called_once_with({})
        build.assert_called_once_with(proxy_handler)
        request = opener.open.call_args.args[0]
        self.assertEqual("true", request.get_header("Metadata"))
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(
            connect.IMDS_TIMEOUT_SECONDS, opener.open.call_args.kwargs["timeout"]
        )
        response.close.assert_called_once_with()

    def test_explicit_subscription_must_match_imds_subscription(self):
        cli_subscription_id = "00000000-0000-0000-0000-000000000001"
        compute = {
            "zone": "1",
            "subscriptionId": "00000000-0000-0000-0000-000000000002",
            "location": "eastus",
        }

        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with mock.patch.object(
                connect, "_run_az_command", return_value=cli_subscription_id
            ):
                with self.assertRaisesRegex(
                    connect.ElasticSanConnectError,
                    "Elastic SAN subscription .* does not match VM subscription",
                ):
                    connect.resolve_physical_zone("production-subscription", "rg", "san")

    def test_non_zonal_vm_is_rejected(self):
        compute = {"zone": "", "subscriptionId": "00000000-0000-0000-0000-000000000001", "location": "eastus"}
        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "not availability-zone pinned"):
                connect.resolve_physical_zone(None, "rg", "san")

    def test_different_vm_and_elastic_san_regions_are_rejected(self):
        subscription_id = "00000000-0000-0000-0000-000000000001"
        compute = {"zone": "2", "subscriptionId": subscription_id, "location": "eastus"}
        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with mock.patch.object(connect, "_run_az_command", side_effect=[subscription_id, "westus"]):
                with self.assertRaisesRegex(
                    connect.ElasticSanConnectError,
                    r"^The VM and Elastic SAN must be in the same region\.$",
                ):
                    connect.resolve_physical_zone(None, "rg", "san")

    def test_missing_malformed_and_unmatched_zone_mappings_are_rejected(self):
        cases = [
            (
                [{"name": "eastus", "metadata": {"regionType": "Physical"}}],
                "missing availabilityZoneMappings",
            ),
            (
                json.loads(locations_payload(mappings=[{"logicalZone": "2", "physicalZone": ""}]))[
                    "value"
                ],
                "malformed entry",
            ),
            (
                json.loads(
                    locations_payload(mappings=[{"logicalZone": "1", "physicalZone": "eastus-az1"}])
                )["value"],
                "no availability-zone mapping",
            ),
        ]
        for locations, error_pattern in cases:
            with self.subTest(error_pattern=error_pattern):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, error_pattern):
                    connect.map_logical_to_physical_zone(locations, "eastus", "2")

    def test_duplicate_trimmed_logical_zones_are_rejected(self):
        locations = json.loads(
            locations_payload(
                mappings=[
                    {"logicalZone": " 2 ", "physicalZone": " eastus-az2 "},
                    {"logicalZone": "2", "physicalZone": "eastus-az3"},
                ]
            )
        )["value"]
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "duplicate logical zone '2'"):
            connect.map_logical_to_physical_zone(locations, " eastus ", " 2 ")

    def test_mapping_trims_logical_and_physical_zones(self):
        locations = json.loads(
            locations_payload(mappings=[{"logicalZone": " 2 ", "physicalZone": " eastus-az3 "}])
        )["value"]
        self.assertEqual(
            "eastus-az3", connect.map_logical_to_physical_zone(locations, " EASTUS ", " 2 ")
        )

    def test_duplicate_matching_regions_are_rejected_regardless_of_order(self):
        regions = [
            json.loads(locations_payload())["value"][0],
            json.loads(locations_payload(
                region_name=" EASTUS ",
                mappings=[{"logicalZone": "2", "physicalZone": "eastus-az1"}],
            ))["value"][0],
        ]
        for locations in (regions, list(reversed(regions)), [regions[0], regions[0]]):
            with self.subTest(locations=locations):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "duplicate.*region"):
                    connect.map_logical_to_physical_zone(locations, "eastus", "2")

    def test_iqn_suffix_is_lowercase_and_rejects_unsafe_characters(self):
        self.assertEqual(
            "iqn.2024-01.com.microsoft:volume:az-eastus-az3",
            connect.decorate_target_iqn("iqn.2024-01.com.microsoft:volume", "EASTUS-AZ3"),
        )
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "unsafe"):
            connect.decorate_target_iqn("iqn.2024-01.com.microsoft:volume", "eastus_az3")

    def test_iqn_length_is_enforced_in_utf8_bytes(self):
        suffix = ":az-eastus-az3"
        prefix = "iqn.2024-01.com.microsoft:"
        allowed_iqn = prefix + "v" * (
            connect.MAX_IQN_UTF8_BYTES - len((prefix + suffix).encode("utf-8"))
        )
        self.assertEqual(
            connect.MAX_IQN_UTF8_BYTES,
            len(connect.decorate_target_iqn(allowed_iqn, "eastus-az3").encode("utf-8")),
        )
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "223-byte"):
            connect.decorate_target_iqn(allowed_iqn + "x", "eastus-az3")

    def test_complete_iqn_rejects_unsafe_characters_without_normalizing_identity(self):
        prefix = "iqn.2024-01.com.microsoft:"
        invalid_iqns = [
            prefix.upper() + "volume",
            prefix + "Volume",
            prefix + "volume_name",
            prefix + "volume/name",
            prefix + "volume@name",
            " " + prefix + "volume",
            prefix + "volume ",
            prefix + "volume\n",
            prefix + "volume\x00",
            prefix + "volume\x7f",
            prefix + "volume\u00e9",
            prefix + "volume\ud800",
        ]
        for iqn in invalid_iqns:
            with self.subTest(iqn=repr(iqn)):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "unsafe"):
                    connect.decorate_target_iqn(iqn, "eastus-az3")

    def test_target_iqn_must_be_a_nonempty_string_before_decoration(self):
        for iqn in (None, "", 123, {}, [], b"iqn.example:volume"):
            with self.subTest(iqn=iqn):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "non-empty string"):
                    connect.decorate_target_iqn(iqn, "eastus-az3")

    def test_multibyte_iqn_is_rejected_even_when_character_count_fits(self):
        prefix = "iqn.2024-01.com.microsoft:"
        suffix = ":az-eastus-az3"
        target = prefix + "\u00e9" * 100
        self.assertLess(len(target + suffix), connect.MAX_IQN_UTF8_BYTES)
        self.assertGreater(
            len((target + suffix).encode("utf-8")), connect.MAX_IQN_UTF8_BYTES
        )
        with self.assertRaises(connect.ElasticSanConnectError):
            connect.decorate_target_iqn(target, "eastus-az3")

    def test_azure_cli_timeout_kills_and_reaps_process(self):
        process = mock.Mock()
        process.wait.side_effect = [
            connect.subprocess.TimeoutExpired(["az", "account", "show"], 0.01), -9,
        ]
        with mock.patch.object(connect.subprocess, "Popen", return_value=process):
            with mock.patch.object(connect, "AZ_CLI_TIMEOUT_SECONDS", 0.01):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "timed out"):
                    connect._run_az_command(["az", "account", "show"], "Azure CLI test")
        process.kill.assert_called_once_with()
        self.assertEqual([
            mock.call(timeout=0.01), mock.call(timeout=connect.PROCESS_REAP_TIMEOUT_SECONDS),
        ], process.wait.call_args_list)


class AzCliAvailabilityTests(unittest.TestCase):
    def test_missing_az_binary_raises_with_freebsd_specific_guidance(self):
        with mock.patch.object(connect.shutil, "which", return_value=None):
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "FreeBSD Ports"):
                connect.check_az_cli_available()

    def test_missing_elastic_san_extension_raises(self):
        with mock.patch.object(connect.shutil, "which", return_value="/usr/local/bin/az"):
            with mock.patch.object(
                connect, "_run_az_command", return_value="storage-preview\nresource-graph\n"
            ):
                with self.assertRaisesRegex(
                    connect.ElasticSanConnectError, "elastic-san' extension is not installed"
                ):
                    connect.check_az_cli_available()

    def test_present_extension_passes_without_raising(self):
        with mock.patch.object(connect.shutil, "which", return_value="/usr/local/bin/az"):
            with mock.patch.object(
                connect, "_run_az_command", return_value="elastic-san\n"
            ):
                connect.check_az_cli_available()  # should not raise


class BoundedSubprocessTests(unittest.TestCase):
    def test_descendant_output_handle_does_not_block_timeout_cleanup(self):
        inherited_handles = []
        process = mock.Mock()
        process.wait.side_effect = [
            connect.subprocess.TimeoutExpired(["az"], 0.01), -9,
        ]

        def start(command, stdout, stderr):
            # A descendant may retain these handles after the direct child dies.
            # Regular-file capture must not wait for that descendant to close them.
            self.assertNotEqual(connect.subprocess.PIPE, stdout)
            self.assertNotEqual(connect.subprocess.PIPE, stderr)
            inherited_handles.extend([os.dup(stdout.fileno()), os.dup(stderr.fileno())])
            return process

        try:
            with mock.patch.object(connect.subprocess, "Popen", side_effect=start):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "timed out"):
                    connect._run_subprocess(["az"], "Azure CLI test", 0.01)
            process.kill.assert_called_once_with()
            self.assertEqual(2, process.wait.call_count)
            self.assertTrue(all(call.kwargs["timeout"] > 0 for call in process.wait.call_args_list))
            process.communicate.assert_not_called()
        finally:
            for descriptor in inherited_handles:
                os.close(descriptor)

    def test_post_kill_reaping_is_bounded_and_reported(self):
        process = mock.Mock()
        process.wait.side_effect = connect.subprocess.TimeoutExpired(["az"], 0.01)
        with mock.patch.object(connect.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "could not be reaped"):
                connect._run_subprocess(["az"], "Azure CLI test", 0.01)
        process.kill.assert_called_once_with()
        self.assertEqual(2, process.wait.call_count)
        self.assertTrue(all(call.kwargs["timeout"] > 0 for call in process.wait.call_args_list))

    def test_output_exit_status_and_handle_cleanup_are_preserved(self):
        captured_handles = []
        process = mock.Mock(returncode=7)

        def start(command, stdout, stderr):
            captured_handles.extend([stdout, stderr])
            stdout.write(b"output\n")
            stderr.write(b"problem\n")
            stdout.flush()
            stderr.flush()
            return process

        with mock.patch.object(connect.subprocess, "Popen", side_effect=start):
            self.assertEqual(
                (7, "output\n", "problem"),
                connect._run_subprocess(["az"], "Azure CLI test", 1),
            )
        self.assertTrue(all(handle.closed for handle in captured_handles))


class FreeBsdMutationPreflightTests(unittest.TestCase):
    def _host(self, sysname="FreeBSD", euid=0, which="/usr/sbin/tool", version="14.1-RELEASE-p5\n"):
        uname = mock.Mock(return_value=mock.Mock(sysname=sysname))
        return [
            mock.patch.object(connect.os, "geteuid", return_value=euid, create=True),
            mock.patch.object(connect.os, "uname", uname, create=True),
            mock.patch.object(connect.shutil, "which", return_value=which),
            mock.patch.object(connect, "_run_command", return_value=version),
        ]

    def _check(self, **host):
        patches = self._host(**host)
        for patch in patches:
            patch.start()
        try:
            connect.check_freebsd_mutation_prerequisites()
            return connect._run_command
        finally:
            for patch in reversed(patches):
                patch.stop()

    def test_requires_root_before_any_platform_probe(self):
        with mock.patch.object(connect.os, "geteuid", return_value=1000, create=True):
            with mock.patch.object(connect.os, "uname", create=True) as uname:
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "must be run as root"):
                    connect.check_freebsd_mutation_prerequisites()
        uname.assert_not_called()

    def test_rejects_non_freebsd_host_before_tool_or_version_checks(self):
        patches = self._host(sysname="Linux", which=None)
        for patch in patches:
            patch.start()
        try:
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "reports 'Linux'"):
                connect.check_freebsd_mutation_prerequisites()
            connect._run_command.assert_not_called()
            connect.shutil.which.assert_not_called()
        finally:
            for patch in reversed(patches):
                patch.stop()

    def test_requires_freebsd_tools_on_path(self):
        with self.assertRaisesRegex(
            connect.ElasticSanConnectError, "iscsictl, service, sysrc, kldstat, kldload, freebsd-version"
        ):
            self._check(which=None)

    def test_rejects_freebsd_older_than_13_and_unparseable_versions(self):
        for version, message in (("12.4-RELEASE-p9\n", "requires FreeBSD 13"), ("garbage\n", "Could not determine")):
            with self.subTest(version=version):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, message):
                    self._check(version=version)

    def test_accepts_supported_versions_using_userland_version(self):
        for version in ("13.0-RELEASE\n", "14.1-RELEASE-p5\n", "15.0-CURRENT\n"):
            with self.subTest(version=version):
                run_command = self._check(version=version)
                run_command.assert_called_once_with(
                    ["freebsd-version", "-u"], "freebsd-version -u", connect.SHORT_COMMAND_TIMEOUT_SECONDS
                )


class KernelDriverTests(unittest.TestCase):
    def _ensure(self, kldstat_returncode, device_exists, iscsi_load, kldload_error=None):
        """Run ensure_iscsi_kernel_driver against mocked host state.

        Returns (mutating argv list, raised error or None) so failure cases can
        still assert what was, and was not, run before the stop.
        """
        exists_results = list(device_exists)

        def run_subprocess(command, description, timeout):
            if command[0] == "kldstat":
                return kldstat_returncode, "", ""
            if command == ["sysrc", "-f", "/boot/loader.conf", "-n", "iscsi_load"]:
                return (0, iscsi_load + "\n", "") if iscsi_load else (1, "", "unknown variable")
            raise AssertionError("unexpected read {}".format(command))

        run_command = mock.Mock(side_effect=kldload_error)
        error = None
        with mock.patch.object(connect, "_run_subprocess", side_effect=run_subprocess):
            with mock.patch.object(connect, "_run_command", run_command):
                with mock.patch.object(connect.os.path, "exists", side_effect=lambda _: exists_results.pop(0)):
                    try:
                        connect.ensure_iscsi_kernel_driver()
                    except connect.ElasticSanConnectError as caught:
                        error = caught
        return [call.args[0] for call in run_command.call_args_list], error

    def test_loaded_module_is_persisted_in_loader_conf(self):
        self.assertEqual(
            ([["sysrc", "-f", "/boot/loader.conf", "iscsi_load=YES"]], None),
            self._ensure(0, [], ""),
        )

    def test_loaded_module_already_persisted_changes_nothing(self):
        self.assertEqual(([], None), self._ensure(0, [], "YES"))

    def test_compiled_in_driver_is_accepted_without_kldload_or_loader_change(self):
        self.assertEqual(([], None), self._ensure(1, [True], ""))

    def test_missing_driver_is_loaded_then_persisted(self):
        self.assertEqual(
            ([["kldload", "iscsi"], ["sysrc", "-f", "/boot/loader.conf", "iscsi_load=YES"]], None),
            self._ensure(1, [False, True], ""),
        )

    def test_kldload_failure_stops_without_persisting(self):
        calls, error = self._ensure(
            1, [False], "", connect.ElasticSanConnectError("kldload iscsi failed: denied")
        )
        self.assertEqual([["kldload", "iscsi"]], calls)
        self.assertRegex(str(error), "could not be loaded: kldload iscsi failed")

    def test_missing_device_after_kldload_stops_without_persisting(self):
        calls, error = self._ensure(1, [False, False], "")
        self.assertEqual([["kldload", "iscsi"]], calls)
        self.assertRegex(str(error), "/dev/iscsi does not exist")


class BootPersistenceTests(unittest.TestCase):
    def _ensure(self, iscsictl_flags, iscsictl_enable):
        values = {"iscsictl_flags": iscsictl_flags, "iscsictl_enable": iscsictl_enable}

        def run_subprocess(command, description, timeout):
            value = values[command[-1]]
            return (0, value + "\n", "") if value is not None else (1, "", "unknown variable")

        with mock.patch.object(connect, "_run_subprocess", side_effect=run_subprocess):
            with mock.patch.object(connect, "_run_command") as run_command:
                connect.ensure_iscsictl_boot_persistence()
        return [call.args[0] for call in run_command.call_args_list]

    def test_stock_or_undefined_flags_enable_boot_relogin(self):
        for flags in ("-Aa", None):
            with self.subTest(flags=flags):
                self.assertEqual([["sysrc", "iscsictl_enable=YES"]], self._ensure(flags, "NO"))

    def test_already_enabled_changes_nothing(self):
        self.assertEqual([], self._ensure("-Aa", "YES"))

    def test_customized_or_explicitly_empty_flags_are_left_untouched(self):
        # An empty value makes rc.d/iscsictl run a bare 'iscsictl', which only lists sessions.
        for flags in ("-A -n mytarget", ""):
            with self.subTest(flags=flags):
                self.assertEqual([], self._ensure(flags, "NO"))


class ConfigValueValidationTests(unittest.TestCase):
    def test_rejects_quotes_braces_hash_semicolon(self):
        for bad_value in ['bad"value', "bad{value", "bad}value", "bad#value", "bad;value"]:
            with self.subTest(bad_value=bad_value):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "not permitted"):
                    connect.validate_config_scalar(bad_value, "field")

    def test_rejects_embedded_newline_and_control_characters(self):
        for bad_value in ["line1\nline2", "tab\tvalue", "cr\rvalue"]:
            with self.subTest(bad_value=bad_value):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "control character"):
                    connect.validate_config_scalar(bad_value, "field")

    def test_rejects_empty_value(self):
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "non-empty"):
            connect.validate_config_scalar("", "field")

    def test_accepts_typical_iqn_and_address(self):
        self.assertEqual(
            "iqn.2005-03.com.microsoft:san.volume",
            connect.validate_config_scalar("iqn.2005-03.com.microsoft:san.volume", "field"),
        )
        self.assertEqual(
            "10.0.0.4:3260", connect.validate_config_scalar("10.0.0.4:3260", "field")
        )


class NicknameTests(unittest.TestCase):
    def test_deterministic_across_calls(self):
        first = connect.build_nickname("vg-1", "vol-1", "iqn.example:target", 1)
        second = connect.build_nickname("vg-1", "vol-1", "iqn.example:target", 1)
        self.assertEqual(first, second)

    def test_session_index_must_be_1_to_32_and_sets_the_suffix(self):
        for index in (0, 33, True, "2"):
            with self.subTest(index=index):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "from 1 to 32"):
                    connect.build_nickname("vg-1", "vol-1", "iqn.example:target", index)
        first = connect.build_nickname("vg-1", "vol-1", "iqn.example:target", 1)
        last = connect.build_nickname("vg-1", "vol-1", "iqn.example:target", 32)
        self.assertRegex(first, r"^esan-vg-1-vol-1-[0-9a-f]{16}-s1$")
        self.assertRegex(last, r"^esan-vg-1-vol-1-[0-9a-f]{16}-s32$")
        self.assertNotEqual(first[:-3], last[:-4])

    def test_distinct_for_different_volumes(self):
        self.assertNotEqual(
            connect.build_nickname("vg-1", "vol-1", "iqn.example:target", 1),
            connect.build_nickname("vg-1", "vol-2", "iqn.example:target", 1),
        )

    def test_sanitizes_unsafe_characters_and_lowercases(self):
        nickname = connect.build_nickname(
            "VG_1", "Volume One!", "iqn.example:target", 1
        )
        self.assertRegex(nickname, r"^[a-z0-9][a-z0-9_-]*$")
        self.assertEqual(
            "esan-vg-1-volume-one-929b9d9f44aabac7-s1",
            nickname,
        )

    def test_raises_when_no_usable_characters_remain(self):
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "no \\[a-z0-9\\] characters"):
            connect.build_nickname("!!!", "vol", "iqn.example:target", 1)

    def test_maximum_resource_names_produce_bounded_ascii_nickname(self):
        nickname = connect.build_nickname(
            "g" * 63, "v" * 63, "iqn.example:target", 1
        )
        self.assertLessEqual(len(nickname), 128)
        self.assertRegex(nickname, r"^esan-[a-z0-9-]+-[0-9a-f]{16}-s1$")
        self.assertEqual(
            nickname,
            connect.build_nickname("g" * 63, "v" * 63, "iqn.example:target", 1),
        )

    def test_long_identities_with_same_readable_prefix_do_not_collide(self):
        first = connect.build_nickname(
            "g" * 63, "v" * 62 + "a", "iqn.example:target", 1
        )
        second = connect.build_nickname(
            "g" * 63, "v" * 62 + "b", "iqn.example:target", 1
        )
        self.assertNotEqual(first, second)
        self.assertEqual(first[:100], second[:100])

    def test_group_volume_boundary_is_unambiguous(self):
        self.assertNotEqual(
            connect.build_nickname("prod-a", "data", "iqn.example:target", 1),
            connect.build_nickname("prod", "a-data", "iqn.example:target", 1),
        )

    def test_sanitization_collision_is_disambiguated(self):
        self.assertNotEqual(
            connect.build_nickname("prod", "data-a", "iqn.example:target", 1),
            connect.build_nickname("prod", "data--a", "iqn.example:target", 1),
        )

    def test_target_iqn_is_part_of_identity(self):
        self.assertNotEqual(
            connect.build_nickname("prod", "data", "iqn.example:first", 1),
            connect.build_nickname("prod", "data", "iqn.example:second", 1),
        )


class ManagedBlockRenderTests(unittest.TestCase):
    def test_render_contains_required_fields_and_markers(self):
        entries = [
            connect.ManagedConfigEntry("esan-vg-vol-s1", "iqn.example:target", "10.0.0.1:3260")
        ]
        block = connect.render_managed_block(entries).decode("utf-8")
        self.assertIn(connect.MANAGED_BLOCK_BEGIN, block)
        self.assertIn(connect.MANAGED_BLOCK_END, block)
        self.assertIn("esan-vg-vol-s1 {", block)
        self.assertIn('TargetName    = "iqn.example:target"', block)
        self.assertIn('TargetAddress = "10.0.0.1:3260"', block)
        self.assertIn("HeaderDigest  = CRC32C", block)
        self.assertIn("DataDigest    = CRC32C", block)
        self.assertIn("Enable        = On", block)


class ConfigSplicingTests(unittest.TestCase):
    def test_appends_block_when_no_markers_present(self):
        existing = b"# unrelated stanza\nfoo { targetname = bar; }\n"
        new_block = connect.render_managed_block(
            [connect.ManagedConfigEntry("esan-vg-new-s1", "iqn.example:new", "new:3260")]
        )
        result = connect.compute_new_config_content(existing, new_block)
        self.assertTrue(result.startswith(existing))
        self.assertTrue(result.endswith(new_block))

    def test_idempotent_entry_preserves_surrounding_content_byte_for_byte(self):
        existing_block = connect.render_managed_block(
            [connect.ManagedConfigEntry("esan-vg-vol-s1", "iqn.example:target", "target:3260")]
        )
        existing = (
            b"# my manual stanza\n"
            b"manual { targetname = manual.target; }\n"
            + existing_block
            + b"# trailing manual comment\n"
        )
        selected_block = connect.render_managed_block(
            [connect.ManagedConfigEntry("esan-vg-vol-s1", "iqn.example:target", "target:3260")]
        )
        result = connect.compute_new_config_content(existing, selected_block)

        self.assertIn(b"# my manual stanza\nmanual { targetname = manual.target; }\n", result)
        self.assertIn(b"# trailing manual comment\n", result)
        self.assertIn(b"iqn.example:target", result)
        self.assertEqual(
            b"# trailing manual comment\n",
            result[result.index(b"# trailing manual comment\n") :],
        )

    def test_corrupted_single_marker_raises(self):
        begin = connect.MANAGED_BLOCK_BEGIN.encode("utf-8")
        existing = b"prefix\n" + begin + b"\nstray content\n"
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "only one of"):
            connect.compute_new_config_content(existing, b"anything")

    def test_end_before_begin_raises(self):
        begin = connect.MANAGED_BLOCK_BEGIN.encode("utf-8")
        end = connect.MANAGED_BLOCK_END.encode("utf-8")
        existing = end + b"\nstuff\n" + begin + b"\n"
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "before its BEGIN marker"):
            connect.compute_new_config_content(existing, b"anything")

    def test_duplicate_marker_pair_raises(self):
        begin = connect.MANAGED_BLOCK_BEGIN.encode("utf-8")
        end = connect.MANAGED_BLOCK_END.encode("utf-8")
        existing = (
            begin + b"\nfirst-nickname { }\n" + end + b"\n"
            b"# accidental duplicate below\n"
            + begin + b"\nsecond-nickname { }\n" + end + b"\n"
        )
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "more than one managed-block"):
            connect.compute_new_config_content(existing, b"anything")

    def test_idempotent_rerender_of_same_plan_is_byte_identical(self):
        entries = [
            connect.ManagedConfigEntry(
                "esan-vg-vol-s1", "iqn.example:target", "10.0.0.1:3260"
            )
        ]
        first = connect.render_managed_block(entries)
        second = connect.render_managed_block(entries)
        self.assertEqual(first, second)

    def test_selecting_b_preserves_a_and_selecting_a_preserves_b(self):
        entry_a = connect.ManagedConfigEntry(
            "esan-vg-a-s1", "iqn.example:a", "a.example:3260"
        )
        entry_b = connect.ManagedConfigEntry(
            "esan-vg-b-s1", "iqn.example:b", "b.example:3260"
        )
        original = b"# exact prefix\xff\n" + connect.render_managed_block([entry_a]) + b"\x00tail"

        with_b = connect.compute_new_config_content(
            original, connect.render_managed_block([entry_b])
        )
        with_a_again = connect.compute_new_config_content(
            with_b, connect.render_managed_block([entry_a])
        )

        self.assertIn(b"esan-vg-a-s1", with_b)
        self.assertIn(b"esan-vg-b-s1", with_b)
        self.assertEqual(with_b, with_a_again)
        self.assertTrue(with_b.startswith(b"# exact prefix\xff\n"))
        self.assertTrue(with_b.endswith(b"\x00tail"))

        a_then_b = connect.compute_new_config_content(
            connect.compute_new_config_content(
                b"", connect.render_managed_block([entry_a])
            ),
            connect.render_managed_block([entry_b]),
        )
        b_then_a = connect.compute_new_config_content(
            connect.compute_new_config_content(
                b"", connect.render_managed_block([entry_b])
            ),
            connect.render_managed_block([entry_a]),
        )
        self.assertEqual(a_then_b, b_then_a)

    def test_malformed_or_unknown_managed_content_is_rejected(self):
        malformed = (
            connect.MANAGED_BLOCK_BEGIN.encode("utf-8")
            + b"\n# unknown content\n"
            + connect.MANAGED_BLOCK_END.encode("utf-8")
            + b"\n"
        )
        selected = connect.render_managed_block(
            [connect.ManagedConfigEntry("esan-vg-vol-s1", "iqn.example:target", "p:3260")]
        )
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "unknown content"):
            connect.compute_new_config_content(malformed, selected)

    def test_empty_selected_block_is_rejected(self):
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "empty"):
            connect.compute_new_config_content(b"", connect.render_managed_block([]))

    def test_conflicting_existing_nickname_is_not_overwritten(self):
        nickname = "esan-vg-vol-0123456789abcdef-s1"
        existing = connect.render_managed_block(
            [connect.ManagedConfigEntry(nickname, "iqn.example:old", "old:3260")]
        )
        selected = connect.render_managed_block(
            [connect.ManagedConfigEntry(nickname, "iqn.example:new", "new:3260")]
        )

        with self.assertRaisesRegex(connect.ElasticSanConnectError, "conflicts"):
            connect.compute_new_config_content(existing, selected)

    def test_identical_existing_nickname_is_idempotent(self):
        entry = connect.ManagedConfigEntry(
            "esan-vg-vol-0123456789abcdef-s1",
            "iqn.example:target",
            "portal:3260",
        )
        block = connect.render_managed_block([entry])
        self.assertEqual(block, connect.compute_new_config_content(block, block))


class PortalMarkerCompatibilityTests(unittest.TestCase):
    """Both this standalone script and the Azure portal's generated FreeBSD connect script can
    write to the same /etc/iscsi.conf. They must use byte-for-byte identical managed-block
    markers so that whichever one ran most recently recognizes and safely merges the other's
    managed entries, rather than treating them as a stray/corrupted marker (see
    "MANAGED_BLOCK_BEGIN"/"MANAGED_BLOCK_END" in this file and generateFreeBsdConnectScript in
    VolumeHelpers.ts). These constants are duplicated here (rather than imported, since the portal
    generator is TypeScript) and must be kept in sync by hand with VolumeHelpers.ts."""

    PORTAL_MANAGED_BLOCK_BEGIN = "# BEGIN AZURE ELASTIC SAN FREEBSD MANAGED BLOCK -- DO NOT EDIT"
    PORTAL_MANAGED_BLOCK_END = "# END AZURE ELASTIC SAN FREEBSD MANAGED BLOCK"

    def test_marker_pair_matches_portal_generated_script(self):
        self.assertEqual(connect.MANAGED_BLOCK_BEGIN, self.PORTAL_MANAGED_BLOCK_BEGIN)
        self.assertEqual(connect.MANAGED_BLOCK_END, self.PORTAL_MANAGED_BLOCK_END)

    def test_content_rendered_with_portal_markers_is_recognized_and_merged(self):
        # Simulate the exact byte content the Azure portal's generated FreeBSD connect script
        # would have written to /etc/iscsi.conf, then confirm this script's splice logic treats
        # it as an ordinary existing managed block (not corruption) and merges into it.
        portal_written_content = (
            connect.render_managed_block(
                [
                    connect.ManagedConfigEntry(
                        "esan-portal-vol-s1", "iqn.portal.example", "portal.example:3260"
                    )
                ]
            )
        )
        entries = [connect.ManagedConfigEntry("esan-vg-vol-s1", "iqn.example:target", "10.0.0.1:3260")]
        new_block = connect.render_managed_block(entries)

        result = connect.compute_new_config_content(portal_written_content, new_block)

        self.assertIn(b"esan-portal-vol-s1", result)
        self.assertIn(b"esan-vg-vol-s1", result)


class AtomicWriteAndBackupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="esan-freebsd-test-")
        self.config_path = os.path.join(self.tempdir, "iscsi.conf")

    def tearDown(self):
        for name in os.listdir(self.tempdir):
            os.remove(os.path.join(self.tempdir, name))
        os.rmdir(self.tempdir)

    def test_write_is_atomic_and_uses_os_replace_in_same_directory(self):
        with open(self.config_path, "wb") as handle:
            handle.write(b"original content\n")
        original_stat = os.stat(self.config_path)

        with mock.patch.object(connect.os, "chown", create=True):
            with mock.patch.object(connect.os, "replace", wraps=os.replace) as replace_mock:
                connect.write_config_atomically(self.config_path, b"new content\n", original_stat)
        replace_mock.assert_called_once()
        temp_arg, final_arg = replace_mock.call_args.args
        self.assertEqual(os.path.dirname(temp_arg), self.tempdir)
        self.assertEqual(final_arg, self.config_path)

        with open(self.config_path, "rb") as handle:
            self.assertEqual(b"new content\n", handle.read())

    def test_write_preserves_permissions_and_ownership_of_existing_file(self):
        with open(self.config_path, "wb") as handle:
            handle.write(b"original\n")

        class FakeStat(object):
            st_mode = 0o100640
            st_uid = 4242
            st_gid = 4343

        with mock.patch.object(connect.os, "chmod") as chmod_mock:
            with mock.patch.object(connect.os, "chown", create=True) as chown_mock:
                connect.write_config_atomically(self.config_path, b"updated\n", FakeStat())

        chmod_mock.assert_called_once()
        temp_path_arg, mode_arg = chmod_mock.call_args.args
        self.assertEqual(0o640, mode_arg)
        chown_mock.assert_called_once_with(temp_path_arg, 4242, 4343)

        with open(self.config_path, "rb") as handle:
            self.assertEqual(b"updated\n", handle.read())

    def test_new_file_gets_default_permissions_when_no_prior_stat(self):
        with mock.patch.object(connect.os, "chmod") as chmod_mock:
            connect.write_config_atomically(self.config_path, b"brand new\n", None)
        chmod_mock.assert_called_once()
        _, mode_arg = chmod_mock.call_args.args
        self.assertEqual(0o644, mode_arg)
        with open(self.config_path, "rb") as handle:
            self.assertEqual(b"brand new\n", handle.read())

    def test_chown_failure_is_not_swallowed(self):
        with mock.patch.object(connect.os, "chmod"):
            with mock.patch.object(
                connect.os, "chown", create=True, side_effect=OSError("not permitted")
            ):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "preserve ownership"):
                    connect.write_config_atomically(
                        self.config_path, b"data\n", os.stat_result((0o100640, 0, 0, 0, 0, 0, 0, 0, 0, 0))
                    )
        # The temp file created for the failed write must not leak, and the
        # final destination must not have been created/touched.
        self.assertEqual([], os.listdir(self.tempdir))
        self.assertFalse(os.path.exists(self.config_path))

    def test_backup_temp_file_does_not_leak_on_failure(self):
        with open(self.config_path, "wb") as handle:
            handle.write(b"pre-existing\n")
        content, existed, _ = connect.read_config_file(self.config_path)

        with mock.patch.object(connect.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                connect.backup_config(self.config_path, content, existed)

        remaining = os.listdir(self.tempdir)
        self.assertEqual(["iscsi.conf"], remaining)

    def test_backup_created_only_when_file_previously_existed(self):
        content, existed, _ = connect.read_config_file(self.config_path)
        self.assertFalse(existed)
        self.assertIsNone(connect.backup_config(self.config_path, content, existed))

        with open(self.config_path, "wb") as handle:
            handle.write(b"pre-existing\n")
        content, existed, _ = connect.read_config_file(self.config_path)
        self.assertTrue(existed)
        backup_path = connect.backup_config(self.config_path, content, existed)
        self.assertIsNotNone(backup_path)
        with open(backup_path, "rb") as handle:
            self.assertEqual(b"pre-existing\n", handle.read())

    def test_rollback_restores_original_content_when_file_existed(self):
        with open(self.config_path, "wb") as handle:
            handle.write(b"original\n")
        content, existed, original_stat = connect.read_config_file(self.config_path)

        connect.write_config_atomically(self.config_path, b"mutated\n", original_stat)
        connect.restore_config_content(self.config_path, content, existed, original_stat)

        with open(self.config_path, "rb") as handle:
            self.assertEqual(b"original\n", handle.read())

    def test_rollback_removes_file_that_did_not_exist_before(self):
        connect.write_config_atomically(self.config_path, b"brand new\n", None)
        self.assertTrue(os.path.exists(self.config_path))
        connect.restore_config_content(self.config_path, b"", False, None)
        self.assertFalse(os.path.exists(self.config_path))


class RecordingFcntl(object):
    LOCK_EX = 2
    LOCK_UN = 8

    def __init__(self):
        self.operations = []

    def flock(self, file_descriptor, operation):
        self.operations.append((file_descriptor, operation))


class BlockingFcntl(object):
    LOCK_EX = 2
    LOCK_UN = 8

    def __init__(self):
        self.lock = threading.Lock()
        self.acquire_count = 0
        self.second_acquire_attempted = threading.Event()

    def flock(self, _file_descriptor, operation):
        if operation == self.LOCK_EX:
            self.acquire_count += 1
            if self.acquire_count == 2:
                self.second_acquire_attempted.set()
            self.lock.acquire()
        else:
            self.lock.release()


class ConfigTransactionLockTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="esan-freebsd-lock-test-")
        self.config_path = os.path.join(self.tempdir, "iscsi.conf")

    def tearDown(self):
        for name in os.listdir(self.tempdir):
            os.remove(os.path.join(self.tempdir, name))
        os.rmdir(self.tempdir)

    def test_lock_path_is_stable_and_separate_from_replaceable_config_inode(self):
        self.assertEqual(
            os.path.join(self.tempdir, connect.CONFIG_LOCK_FILENAME),
            connect.get_config_lock_path(self.config_path),
        )

    def test_secure_open_acquires_and_releases_exclusive_lock(self):
        fake_fcntl = RecordingFcntl()
        with mock.patch.object(connect, "fcntl", fake_fcntl):
            with mock.patch.object(connect.os, "open", wraps=os.open) as open_mock:
                lock_path, lock_fd = connect.acquire_config_transaction_lock(
                    self.config_path
                )
            opened_path, flags, mode = open_mock.call_args.args
            self.assertEqual(lock_path, opened_path)
            self.assertEqual(0o600, mode)
            self.assertTrue(flags & os.O_CREAT)
            self.assertTrue(flags & os.O_RDWR)
            if hasattr(os, "O_NOFOLLOW"):
                self.assertTrue(flags & os.O_NOFOLLOW)

            connect.release_config_transaction_lock(lock_fd)

        self.assertEqual(
            [fake_fcntl.LOCK_EX, fake_fcntl.LOCK_UN],
            [operation for _, operation in fake_fcntl.operations],
        )

    def test_non_regular_lock_file_is_rejected_and_handle_is_closed(self):
        fake_fcntl = RecordingFcntl()
        directory_stat = os.stat_result((stat.S_IFDIR | 0o700,) + (0,) * 9)
        with mock.patch.object(connect, "fcntl", fake_fcntl):
            with mock.patch.object(connect.os, "fstat", return_value=directory_stat):
                with mock.patch.object(connect.os, "close", wraps=os.close) as close_mock:
                    with self.assertRaisesRegex(
                        connect.ElasticSanConnectError, "non-symlink regular file"
                    ):
                        connect.acquire_config_transaction_lock(self.config_path)
        close_mock.assert_called_once()
        self.assertEqual([], fake_fcntl.operations)

    def test_symlink_lock_file_is_rejected_and_handle_is_closed(self):
        fake_fcntl = RecordingFcntl()
        symlink_stat = os.stat_result((stat.S_IFLNK | 0o777,) + (0,) * 9)
        with mock.patch.object(connect, "fcntl", fake_fcntl):
            with mock.patch.object(connect.os, "lstat", return_value=symlink_stat):
                with mock.patch.object(connect.os, "close", wraps=os.close) as close_mock:
                    with self.assertRaisesRegex(
                        connect.ElasticSanConnectError, "non-symlink regular file"
                    ):
                        connect.acquire_config_transaction_lock(self.config_path)
        close_mock.assert_called_once()
        self.assertEqual([], fake_fcntl.operations)


class SessionParsingTests(unittest.TestCase):
    def test_parses_target_name_and_portal_from_verbose_output(self):
        output = verbose_session_block("iqn.example:target", "10.0.0.4:3260")
        sessions = connect._parse_iscsictl_verbose_sessions(output)
        self.assertEqual(1, len(sessions))
        self.assertEqual("iqn.example:target", sessions[0]["Target name"])
        self.assertEqual("10.0.0.4:3260", sessions[0]["Target portal"])
        self.assertEqual("Connected", sessions[0]["Session state"])

    def test_parses_multiple_sessions_separated_by_blank_line(self):
        output = "\n\n".join(
            [
                verbose_session_block("iqn.example:target", "10.0.0.4:3260"),
                verbose_session_block("iqn.example:target", "10.0.0.4:3260"),
                verbose_session_block("iqn.example:other", "10.0.0.5:3260"),
            ]
        )
        sessions = connect._parse_iscsictl_verbose_sessions(output)
        self.assertEqual(3, len(sessions))
        self.assertEqual(2, len(connect.sessions_for_target(sessions, "iqn.example:target")))
        self.assertEqual(1, len(connect.sessions_for_target(sessions, "iqn.example:other")))
        self.assertEqual(0, len(connect.sessions_for_target(sessions, "iqn.example:missing")))

    def test_parses_adjacent_sessions_without_blank_separator(self):
        output = (
            verbose_session_block("iqn.example:first", "first:3260").rstrip()
            + "\n"
            + verbose_session_block("iqn.example:second", "second:3260")
        )
        sessions = connect._parse_iscsictl_verbose_sessions(output)
        self.assertEqual(
            ["iqn.example:first", "iqn.example:second"],
            [session["Target name"] for session in sessions],
        )

    def test_empty_output_means_zero_sessions(self):
        self.assertEqual([], connect._parse_iscsictl_verbose_sessions(""))
        self.assertEqual([], connect._parse_iscsictl_verbose_sessions("\n\n"))

    def test_missing_required_session_field_is_rejected(self):
        malformed = verbose_session_block(
            "iqn.example:target", "target.example:3260"
        ).replace("Session state:             Connected\n", "")
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "missing Session state"):
            connect._parse_iscsictl_verbose_sessions(malformed)

    def test_target_matching_is_exact_and_case_insensitive(self):
        sessions = [
            {"Target name": "IQN.Example:Vol1"},
            {"Target name": "iqn.example:vol10"},
            {"Target name": "iqn.example:vol"},
        ]
        self.assertEqual(
            [sessions[0]], connect.sessions_for_target(sessions, "iqn.example:vol1")
        )


def session(target_name, state="Connected", enable="Yes", portal="10.0.0.4:3260"):
    return {
        "Target name": target_name,
        "Target portal": portal,
        "Session state": state,
        "Enable": enable,
    }


class ExistingSessionPlanningTests(unittest.TestCase):
    """plan_volume_action recognizes a volume in any zonal mode, on any portal."""

    def _plan(self, target_name="iqn.example:target", volume_iqn=None, sessions=1):
        return connect.VolumeConnectionPlan(
            "vol1",
            target_name,
            "portal.example:3260",
            [connect.build_nickname("vg", "vol1", target_name, index)
             for index in range(1, sessions + 1)],
            volume_iqn=volume_iqn,
        )

    def _action(self, plan, sessions=(), entries=()):
        return connect.plan_volume_action(plan, list(entries), list(sessions), "/etc/iscsi.conf")

    def test_volume_without_sessions_or_entries_is_connected(self):
        self.assertIsNone(self._action(self._plan()))

    def test_connected_sessions_skip_even_when_redirected_or_more_than_one(self):
        plan = self._plan()
        reason = self._action(plan, [
            session(plan.target_name, portal="redirected.example:3260"),
            session(plan.target_name, portal="10.0.0.5:3260"),
        ])
        self.assertTrue(reason.startswith("already connected (2 live / 0 persistent)"))

    def test_sessions_and_entries_of_another_zonal_mode_skip_the_volume(self):
        plain = self._plan()
        decorated = self._plan("iqn.example:target:az-eastus-az1", volume_iqn="iqn.example:target")
        self.assertIn(
            "already connected",
            self._action(plain, [session("IQN.example:target:AZ-eastus-az3")]),
        )
        self.assertIn(
            "persistent configuration exists",
            self._action(decorated, entries=plain.entries()),
        )

    def test_similar_prefixes_and_nested_suffixes_do_not_match(self):
        plan = self._plan()
        for name in (
            "iqn.example:target2",
            "iqn.example:target:az-",
            "iqn.example:target:az-zone:extra",
            "iqn.example:targe",
        ):
            with self.subTest(name=name):
                self.assertIsNone(self._action(plan, [session(name)]))

    def test_not_connected_or_disabled_sessions_are_anomalies(self):
        plan = self._plan()
        for existing in (
            session(plan.target_name, state="Disconnected"),
            session(plan.target_name, enable="No"),
        ):
            with self.subTest(existing=existing):
                reason = self._action(plan, [session(plan.target_name), existing])
                self.assertTrue(reason.startswith("pre-existing session anomaly (2 session(s)"))

    def test_only_the_exact_planned_entries_are_reestablished(self):
        plan = self._plan(sessions=3)
        self.assertIsNone(self._action(plan, entries=plan.entries()))
        moved = [
            connect.ManagedConfigEntry(entry.nickname, entry.target_name, "10.9.9.9:3260")
            for entry in plan.entries()
        ]
        for entries in (plan.entries()[:2], moved):
            with self.subTest(entries=len(entries)):
                self.assertIn("persistent configuration exists", self._action(plan, entries=entries))


class AddSessionsForVolumeTests(unittest.TestCase):
    def _plan(self, addresses):
        nicknames = [
            connect.build_nickname("vg", "vol1", "iqn.example:target", index)
            for index in range(1, len(addresses) + 1)
        ]
        return connect.VolumeConnectionPlan(
            "vol1", "iqn.example:target", "portal.example:3260", nicknames, addresses
        )

    def test_adds_every_session_in_order_then_waits_for_all_of_them(self):
        plan = self._plan(["10.0.0.1:3260", "10.0.0.1:3260", "10.0.0.2:3260"])
        connecting = session(plan.target_name, state="Connecting")
        connected = session(plan.target_name)
        unrelated = session("iqn.example:unrelated", state="Disconnected", enable="No")
        clock = mock.Mock(side_effect=[0, 0, 1])
        with mock.patch.object(
            connect,
            "list_iscsi_sessions",
            side_effect=[
                [unrelated],
                [unrelated, connected, connecting, connecting],
                [unrelated, connected, connected, connected],
            ],
        ):
            with mock.patch.object(connect, "add_iscsi_session") as add_mock:
                with mock.patch.object(connect.time, "monotonic", clock):
                    with mock.patch.object(connect.time, "sleep"):
                        connect.add_sessions_for_volume(plan, "/etc/iscsi.conf")
        self.assertEqual(
            [mock.call(nickname, "/etc/iscsi.conf") for nickname in plan.nicknames],
            add_mock.call_args_list,
        )

    def test_session_that_appeared_after_the_inventory_is_not_duplicated(self):
        plan = self._plan(["10.0.0.1:3260"])
        with mock.patch.object(
            connect, "list_iscsi_sessions", return_value=[session("iqn.example:target:az-x")]
        ):
            with mock.patch.object(connect, "add_iscsi_session") as add_mock:
                connect.add_sessions_for_volume(plan, "/etc/iscsi.conf")
        add_mock.assert_not_called()

    def test_rejected_second_session_to_one_portal_explains_initiator_limit(self):
        plan = self._plan(["portal.example:3260", "portal.example:3260"])
        with mock.patch.object(connect, "list_iscsi_sessions", return_value=[]):
            with mock.patch.object(
                connect,
                "add_iscsi_session",
                side_effect=[None, connect.ElasticSanConnectError("ISCSISADD: Device busy")],
            ):
                with self.assertRaisesRegex(
                    connect.ElasticSanConnectError,
                    "Device busy The initiator rejected another session to the same target portal",
                ):
                    connect.add_sessions_for_volume(plan, "/etc/iscsi.conf")

    def test_first_submission_error_is_preserved_without_hint(self):
        plan = self._plan(["10.0.0.1:3260", "10.0.0.2:3260"])
        with mock.patch.object(connect, "list_iscsi_sessions", return_value=[]):
            with mock.patch.object(
                connect,
                "add_iscsi_session",
                side_effect=[None, connect.ElasticSanConnectError("kernel rejected request")],
            ):
                with self.assertRaisesRegex(
                    connect.ElasticSanConnectError, "^kernel rejected request$"
                ):
                    connect.add_sessions_for_volume(plan, "/etc/iscsi.conf")


class SessionReadinessPollingTests(unittest.TestCase):
    def test_timeout_is_bounded_and_reports_possible_live_session(self):
        times = iter([100.0, 100.0, 102.0, 105.0])
        sleeps = []
        with mock.patch.object(
            connect,
            "list_iscsi_sessions",
            return_value=[
                session("iqn.example:other", state="Disconnected", enable="No"),
                session("iqn.example:target"),
            ],
        ):
            with self.assertRaisesRegex(
                connect.ElasticSanConnectError,
                r"waiting for 2 session\(s\).*\(1 Connected\).*may still create or leave live",
            ):
                connect.wait_for_connected_session(
                    "iqn.example:target",
                    timeout=5,
                    poll_interval=2,
                    monotonic=lambda: next(times),
                    sleep=sleeps.append,
                    expected_count=2,
                )
        self.assertEqual([2, 2], sleeps)


class ExactCommandArgvTests(unittest.TestCase):
    def test_ensure_iscsid_enabled_and_running_sets_unset_value_and_starts_service(self):
        with mock.patch.object(connect, "_run_command", return_value="") as run_command:
            with mock.patch.object(
                connect,
                "_run_subprocess",
                side_effect=[
                    (1, "", "unknown variable"),
                    (1, "", "iscsid is not running"),
                ],
            ) as run_subprocess:
                connect.ensure_iscsid_enabled_and_running()

        self.assertEqual(
            [
                mock.call(
                    ["sysrc", "-n", "iscsid_enable"],
                    "sysrc iscsid_enable query",
                    connect.SHORT_COMMAND_TIMEOUT_SECONDS,
                ),
                mock.call(
                    ["service", "iscsid", "onestatus"],
                    "service iscsid onestatus",
                    connect.SHORT_COMMAND_TIMEOUT_SECONDS,
                ),
            ],
            run_subprocess.call_args_list,
        )
        self.assertEqual(
            [
                mock.call(
                    ["sysrc", "iscsid_enable=YES"],
                    "sysrc iscsid_enable=YES",
                    connect.SHORT_COMMAND_TIMEOUT_SECONDS,
                ),
                mock.call(
                    ["service", "iscsid", "start"],
                    "service iscsid start",
                    connect.SHORT_COMMAND_TIMEOUT_SECONDS,
                ),
            ],
            run_command.call_args_list,
        )

    def test_ensure_iscsid_enabled_and_running_preserves_yes_and_running_service(self):
        with mock.patch.object(connect, "_run_command") as run_command:
            with mock.patch.object(
                connect,
                "_run_subprocess",
                side_effect=[
                    (0, "YES\n", ""),
                    (0, "iscsid is running as pid 123.\n", ""),
                ],
            ) as run_subprocess:
                connect.ensure_iscsid_enabled_and_running()

        self.assertEqual(
            [
                mock.call(
                    ["sysrc", "-n", "iscsid_enable"],
                    "sysrc iscsid_enable query",
                    connect.SHORT_COMMAND_TIMEOUT_SECONDS,
                ),
                mock.call(
                    ["service", "iscsid", "onestatus"],
                    "service iscsid onestatus",
                    connect.SHORT_COMMAND_TIMEOUT_SECONDS,
                ),
            ],
            run_subprocess.call_args_list,
        )
        run_command.assert_not_called()

    def test_ensure_iscsid_enabled_and_running_changes_no_without_starting_running_service(self):
        with mock.patch.object(connect, "_run_command", return_value="") as run_command:
            with mock.patch.object(
                connect,
                "_run_subprocess",
                side_effect=[
                    (0, "NO\n", ""),
                    (0, "iscsid is running as pid 123.\n", ""),
                ],
            ) as run_subprocess:
                connect.ensure_iscsid_enabled_and_running()

        self.assertEqual(
            [
                mock.call(
                    ["sysrc", "-n", "iscsid_enable"],
                    "sysrc iscsid_enable query",
                    connect.SHORT_COMMAND_TIMEOUT_SECONDS,
                ),
                mock.call(
                    ["service", "iscsid", "onestatus"],
                    "service iscsid onestatus",
                    connect.SHORT_COMMAND_TIMEOUT_SECONDS,
                ),
            ],
            run_subprocess.call_args_list,
        )
        run_command.assert_called_once_with(
            ["sysrc", "iscsid_enable=YES"],
            "sysrc iscsid_enable=YES",
            connect.SHORT_COMMAND_TIMEOUT_SECONDS,
        )

    def test_add_iscsi_session_argv(self):
        with mock.patch.object(connect, "_run_command", return_value="") as run_command:
            connect.add_iscsi_session("esan-vg-vol-s1", "/etc/iscsi.conf")
        run_command.assert_called_once_with(
            ["iscsictl", "-A", "-n", "esan-vg-vol-s1", "-c", "/etc/iscsi.conf"],
            "iscsictl add session 'esan-vg-vol-s1'",
            connect.ISCSICTL_COMMAND_TIMEOUT_SECONDS,
        )

    def test_list_iscsi_sessions_argv(self):
        with mock.patch.object(connect, "_run_command", return_value="") as run_command:
            connect.list_iscsi_sessions()
        run_command.assert_called_once_with(
            ["iscsictl", "-L", "-v"], "iscsictl session listing", connect.AZ_CLI_TIMEOUT_SECONDS
        )

    def test_never_calls_iscsictl_remove_all(self):
        with open(SCRIPT_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        # Look for the actual argv-construction pattern, not the prose
        # description of the constraint that legitimately appears in the
        # module docstring.
        self.assertNotIn('"iscsictl", "-R"', source)
        self.assertNotIn("'iscsictl', '-R'", source)


class GetVolumeStorageTargetTests(unittest.TestCase):
    def test_argv_and_parsing(self):
        payload = json.dumps(
            {"targetIqn": "iqn.example:target", "targetPortalHostname": "10.0.0.4", "targetPortalPort": 3260}
        )
        with mock.patch.object(connect, "_run_az_command", return_value=payload) as run_az:
            iqn, hostname, port = connect.get_volume_storage_target(
                "sub", "rg", "san", "vg", "vol1"
            )
        self.assertEqual(("iqn.example:target", "10.0.0.4", 3260), (iqn, hostname, port))
        run_az.assert_called_once_with(
            [
                "az",
                "elastic-san",
                "volume",
                "show",
                "-g",
                "rg",
                "-e",
                "san",
                "-v",
                "vg",
                "-n",
                "vol1",
                "--query",
                "storageTarget",
                "--output",
                "json",
                "--subscription",
                "sub",
            ],
            "Elastic SAN volume 'vol1' storage target lookup",
        )

    def test_omits_subscription_flag_when_not_provided(self):
        payload = json.dumps(
            {"targetIqn": "iqn.example:target", "targetPortalHostname": "10.0.0.4", "targetPortalPort": 3260}
        )
        with mock.patch.object(connect, "_run_az_command", return_value=payload) as run_az:
            connect.get_volume_storage_target(None, "rg", "san", "vg", "vol1")
        command = run_az.call_args.args[0]
        self.assertNotIn("--subscription", command)

    def test_missing_field_raises(self):
        payload = json.dumps({"targetIqn": "iqn.example:target"})
        with mock.patch.object(connect, "_run_az_command", return_value=payload):
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "missing"):
                connect.get_volume_storage_target(None, "rg", "san", "vg", "vol1")


class BuildConnectionPlanTests(unittest.TestCase):
    def test_opt_out_uses_original_iqn_without_zone_resolution(self):
        with mock.patch.object(connect, "resolve_physical_zone") as resolve_zone:
            with mock.patch.object(
                connect,
                "get_volume_storage_target",
                return_value=("iqn.original", "portal.example", 3260),
            ):
                plans = connect.build_connection_plan(
                    None, "rg", "san", "vg", ["volume1"], 1, enable_zonal_affinity=False
                )
        resolve_zone.assert_not_called()
        self.assertEqual("iqn.original", plans[0].target_name)
        self.assertEqual("portal.example:3260", plans[0].target_address)
        self.assertEqual(1, len(plans[0].nicknames))

    def test_opt_out_does_not_apply_enabled_iqn_normalization_or_validation(self):
        original_iqn = "iqn.2024-01.com.microsoft:Legacy_Volume"
        with mock.patch.object(connect, "resolve_physical_zone") as resolve_zone:
            with mock.patch.object(
                connect,
                "get_volume_storage_target",
                return_value=(original_iqn, "portal.example", 3260),
            ):
                plans = connect.build_connection_plan(
                    None, "rg", "san", "vg", ["volume1"], 1
                )
        resolve_zone.assert_not_called()
        self.assertEqual(original_iqn, plans[0].target_name)
        self.assertEqual("portal.example:3260", plans[0].target_address)
        self.assertEqual(
            [connect.build_nickname("vg", "volume1", original_iqn, 1)],
            plans[0].nicknames,
        )

    def test_opt_in_decorates_iqn_for_all_volumes(self):
        with mock.patch.object(connect, "resolve_physical_zone", return_value=("EASTUS-AZ3", "sub")) as resolve_zone:
            with mock.patch.object(
                connect,
                "get_volume_storage_target",
                side_effect=[
                    ("iqn.original.volume1", "portal.example", 3260),
                    ("iqn.original.volume2", "portal.example", 3260),
                ],
            ):
                plans = connect.build_connection_plan(
                    "sub", "rg", "san", "vg", ["volume1", "volume2"], 1, enable_zonal_affinity=True
                )
        resolve_zone.assert_called_once_with("sub", "rg", "san")
        for plan in plans:
            self.assertEqual(
                "iqn.original.{}:az-eastus-az3".format(plan.volume_name), plan.target_name
            )

    def test_empty_volume_selection_is_rejected_before_discovery(self):
        with mock.patch.object(connect, "resolve_physical_zone") as resolve_zone:
            with mock.patch.object(connect, "get_volume_storage_target") as get_target:
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "At least one volume"):
                    connect.build_connection_plan(
                        None, "rg", "san", "vg", [], 1, enable_zonal_affinity=True
                    )
        resolve_zone.assert_not_called()
        get_target.assert_not_called()

    def test_target_iqn_is_resolved_before_nickname_generation(self):
        with mock.patch.object(
            connect,
            "get_volume_storage_target",
            return_value=("iqn.example:resolved", "portal.example", 3260),
        ):
            with mock.patch.object(
                connect, "build_nickname", return_value="esan-vg-volume1-deadbeefdeadbeef-s1"
            ) as build_nickname:
                connect.build_connection_plan(
                    None, "rg", "san", "vg", ["volume1"], 1
                )

        build_nickname.assert_called_once_with(
            "vg", "volume1", "iqn.example:resolved", 1
        )

    def test_duplicate_generated_nicknames_are_rejected(self):
        with mock.patch.object(
            connect,
            "get_volume_storage_target",
            side_effect=[
                ("iqn.example:first", "first.example", 3260),
                ("iqn.example:second", "second.example", 3260),
            ],
        ):
            with mock.patch.object(
                connect,
                "build_nickname",
                return_value="esan-collision-deadbeefdeadbeef-s1",
            ):
                with self.assertRaisesRegex(
                    connect.ElasticSanConnectError, "duplicate nickname"
                ):
                    connect.build_connection_plan(
                        None, "rg", "san", "vg", ["volume1", "volume2"], 1
                    )

    def test_volume_selected_more_than_once_in_any_case_is_planned_once(self):
        with mock.patch.object(
            connect,
            "get_volume_storage_target",
            side_effect=[
                ("iqn.example:vol1", "portal.example", 3260),
                ("iqn.example:vol2", "portal.example", 3260),
                ("IQN.EXAMPLE:VOL1", "portal.example", 3260),
                ("iqn.example:vol1", "portal.example", 3260),
            ],
        ):
            with mock.patch("builtins.print") as print_mock:
                plans = connect.build_connection_plan(
                    None, "rg", "san", "vg", ["vol1", "vol2", "VOL1", "vol1"], 1
                )
        self.assertEqual(["vol1", "vol2"], [plan.volume_name for plan in plans])
        self.assertEqual(
            [
                "VOL1 [IQN.EXAMPLE:VOL1]: Ignored duplicate selection; the same target is "
                "already planned as 'vol1'",
                "vol1 [iqn.example:vol1]: Ignored duplicate selection; the same target is "
                "already planned as 'vol1'",
            ],
            [call.args[0] for call in print_mock.call_args_list],
        )


def getaddrinfo_answers(*addresses):
    return [(2, 1, 6, "", (address, 0)) for address in addresses]


class VipDistributionTests(unittest.TestCase):
    def test_without_opt_in_every_session_uses_the_fqdn_and_dns_is_not_queried(self):
        with mock.patch.object(connect.socket, "getaddrinfo") as getaddrinfo:
            self.assertEqual(
                [("portal.example", 32)],
                connect.resolve_session_portals("portal.example", 32),
            )
        getaddrinfo.assert_not_called()

    def test_ipv4_answers_are_deduplicated_sorted_numerically_and_split_11_11_10(self):
        answers = getaddrinfo_answers("10.0.0.10", "10.0.0.9", "10.0.0.10", "10.0.0.100")
        with mock.patch.object(connect.socket, "getaddrinfo", return_value=answers) as getaddrinfo:
            portals = connect.resolve_session_portals("portal.example", 32, True)
        getaddrinfo.assert_called_once_with(
            "portal.example", None, connect.socket.AF_INET, connect.socket.SOCK_STREAM
        )
        self.assertEqual([("10.0.0.9", 11), ("10.0.0.10", 11), ("10.0.0.100", 10)], portals)

    def test_one_address_keeps_the_fqdn_and_zero_shares_are_dropped(self):
        with mock.patch.object(
            connect.socket, "getaddrinfo", return_value=getaddrinfo_answers("10.0.0.1")
        ):
            self.assertEqual(
                [("portal.example", 24)], connect.resolve_session_portals("portal.example", 24, True)
            )
        with mock.patch.object(
            connect.socket,
            "getaddrinfo",
            return_value=getaddrinfo_answers("10.0.0.1", "10.0.0.2", "10.0.0.3"),
        ):
            self.assertEqual(
                [("10.0.0.1", 1), ("10.0.0.2", 1)],
                connect.resolve_session_portals("portal.example", 2, True),
            )

    def test_dns_failures_stop_with_the_hostname(self):
        for side_effect, expected in (
            (OSError("no such host"), "DNS lookup failed for 'portal.example': no such host"),
            ([], "DNS lookup returned no IPv4 addresses for 'portal.example'"),
        ):
            with self.subTest(expected=expected):
                kwargs = {"side_effect": side_effect} if isinstance(side_effect, Exception) else {
                    "return_value": side_effect
                }
                with mock.patch.object(connect.socket, "getaddrinfo", **kwargs):
                    with self.assertRaisesRegex(connect.ElasticSanConnectError, expected):
                        connect.resolve_session_portals("portal.example", 32, True)

    def test_plan_assigns_contiguous_sessions_per_vip_and_resolves_each_fqdn_once(self):
        answers = getaddrinfo_answers("10.0.0.3", "10.0.0.1", "10.0.0.2")
        with mock.patch.object(
            connect,
            "get_volume_storage_target",
            side_effect=[
                ("iqn.example:vol1", "portal.example", 3260),
                ("iqn.example:vol2", "portal.example", 3260),
            ],
        ):
            with mock.patch.object(connect.socket, "getaddrinfo", return_value=answers) as dns:
                plans = connect.build_connection_plan(
                    None, "rg", "san", "vg", ["vol1", "vol2"], 32, enable_vip_distribution=True
                )
        dns.assert_called_once()
        for plan in plans:
            self.assertEqual("portal.example:3260", plan.target_address)
            self.assertEqual(
                ["10.0.0.1:3260"] * 11 + ["10.0.0.2:3260"] * 11 + ["10.0.0.3:3260"] * 10,
                plan.session_addresses,
            )
            self.assertEqual(32, len(set(plan.nicknames)))
            self.assertTrue(plan.nicknames[31].endswith("-s32"))

    def test_dry_run_prints_the_portal_split_and_multipath_notice(self):
        argv = [
            "-g", "rg", "-e", "san", "-v", "vg", "-n", "volume1",
            "--enable-vip-distribution", "--dry-run",
        ]
        output = io.StringIO()
        with mock.patch.object(connect, "check_az_cli_available"):
            with mock.patch.object(
                connect, "get_volume_storage_target",
                return_value=("iqn.example:volume1", "portal.example", 3260),
            ):
                with mock.patch.object(
                    connect.socket, "getaddrinfo",
                    return_value=getaddrinfo_answers("10.0.0.1", "10.0.0.2", "10.0.0.3"),
                ):
                    with mock.patch.object(connect, "_run_subprocess") as run_subprocess:
                        with contextlib.redirect_stdout(output):
                            connect.main(argv)
        run_subprocess.assert_not_called()
        text = output.getvalue()
        self.assertIn(connect.MULTIPATH_NOTICE, text)
        self.assertIn(
            "at 10.0.0.1:3260 (11), 10.0.0.2:3260 (11), 10.0.0.3:3260 (10) (32 session(s)", text
        )
        self.assertEqual(32, text.count('TargetName    = "iqn.example:volume1"'))


class ExecuteConnectionPlanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="esan-freebsd-exec-test-")
        self.config_path = os.path.join(self.tempdir, "iscsi.conf")
        self.fake_fcntl = RecordingFcntl()
        self.fcntl_patch = mock.patch.object(connect, "fcntl", self.fake_fcntl)
        self.fcntl_patch.start()

    def tearDown(self):
        self.fcntl_patch.stop()
        for name in os.listdir(self.tempdir):
            os.remove(os.path.join(self.tempdir, name))
        os.rmdir(self.tempdir)

    def _plans(self):
        return [
            connect.VolumeConnectionPlan(
                "vol1", "iqn.example:vol1", "10.0.0.4:3260", ["esan-vg-vol1-s1"]
            )
        ]

    def test_successful_run_writes_config_and_adds_sessions(self):
        plans = self._plans()
        with mock.patch.object(connect, "ensure_freebsd_prerequisites") as prerequisites:
            # Pre-mutation inventory, add_sessions_for_volume's re-check, then readiness poll.
            with mock.patch.object(connect, "list_iscsi_sessions", side_effect=[[], [], [
                {
                    "Target name": "iqn.example:vol1",
                    "Target portal": "redirected:3260",
                    "Session state": "Connected",
                    "Enable": "Yes",
                },
            ]]):
                with mock.patch.object(connect, "add_iscsi_session") as add_mock:
                    with mock.patch("builtins.print") as print_mock:
                        connected = connect.execute_connection_plan(plans, self.config_path, 1)
        prerequisites.assert_called_once_with()
        self.assertEqual(["vol1"], connected)
        self.assertEqual(1, add_mock.call_count)
        self.assertEqual(
            1,
            [call.args for call in print_mock.call_args_list].count(
                (connect.RECOMMENDED_SETTINGS_NOTICE,)
            ),
        )
        self.assertEqual(
            [self.fake_fcntl.LOCK_EX, self.fake_fcntl.LOCK_UN],
            [operation for _, operation in self.fake_fcntl.operations],
        )
        with open(self.config_path, "r", encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("esan-vg-vol1-s1", content)

    def test_failure_during_session_add_rolls_back_config_and_preserves_unrelated_content(self):
        with open(self.config_path, "wb") as handle:
            handle.write(b"# unrelated manual stanza\nmanual { targetname = manual; }\n")
        with open(self.config_path, "rb") as handle:
            original_content = handle.read()

        plans = self._plans()
        original_restore = connect.restore_config_content

        def restore_while_locked(*args):
            self.assertEqual(
                [self.fake_fcntl.LOCK_EX],
                [operation for _, operation in self.fake_fcntl.operations],
            )
            return original_restore(*args)

        with mock.patch.object(connect, "ensure_freebsd_prerequisites"):
            with mock.patch.object(connect, "list_iscsi_sessions", return_value=[]):
                with mock.patch.object(
                    connect, "add_iscsi_session", side_effect=connect.ElasticSanConnectError("boom")
                ):
                    with mock.patch.object(
                        connect,
                        "restore_config_content",
                        side_effect=restore_while_locked,
                    ):
                        with self.assertRaisesRegex(connect.ElasticSanConnectError, "restored"):
                            connect.execute_connection_plan(plans, self.config_path, 1)

        with open(self.config_path, "rb") as handle:
            restored_content = handle.read()
        self.assertEqual(original_content, restored_content)
        self.assertNotIn(b"esan-vg-vol1-s1", restored_content)
        self.assertEqual(
            [self.fake_fcntl.LOCK_EX, self.fake_fcntl.LOCK_UN],
            [operation for _, operation in self.fake_fcntl.operations],
        )

    def test_failure_when_config_did_not_exist_before_removes_it_on_rollback(self):
        self.assertFalse(os.path.exists(self.config_path))
        plans = self._plans()
        with mock.patch.object(connect, "ensure_freebsd_prerequisites"):
            with mock.patch.object(connect, "list_iscsi_sessions", return_value=[]):
                with mock.patch.object(
                    connect, "add_iscsi_session", side_effect=connect.ElasticSanConnectError("boom")
                ):
                    with self.assertRaises(connect.ElasticSanConnectError):
                        connect.execute_connection_plan(plans, self.config_path, 1)
        self.assertFalse(os.path.exists(self.config_path))

    def test_rerun_with_all_sessions_already_connected_does_not_call_add(self):
        plans = self._plans()
        already_connected = [
            {
                "Target name": "iqn.example:vol1",
                "Target portal": "redirected:3260",
                "Session state": "Connected",
                "Enable": "Yes",
            },
        ]
        with mock.patch.object(connect, "ensure_freebsd_prerequisites"):
            with mock.patch.object(connect, "list_iscsi_sessions", return_value=already_connected):
                with mock.patch.object(connect, "add_iscsi_session") as add_mock:
                    connected = connect.execute_connection_plan(plans, self.config_path, 1)
        add_mock.assert_not_called()
        self.assertEqual([], connected)
        # A live session without a managed entry is skipped, not made persistent (P9).
        self.assertFalse(os.path.exists(self.config_path))

    def test_empty_plan_is_rejected_before_any_mutation(self):
        with mock.patch.object(connect, "acquire_config_transaction_lock") as acquire_lock:
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "empty"):
                connect.execute_connection_plan([], self.config_path, 1)
        acquire_lock.assert_not_called()

    def test_plan_session_count_mismatch_is_rejected_before_any_mutation(self):
        plans = self._plans()
        with mock.patch.object(connect, "acquire_config_transaction_lock") as acquire_lock:
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "exactly 2 session"):
                connect.execute_connection_plan(plans, self.config_path, 2)
        acquire_lock.assert_not_called()

    def test_multi_session_vip_plan_writes_one_entry_per_session_and_adds_each(self):
        addresses = ["10.0.0.1:3260", "10.0.0.1:3260", "10.0.0.2:3260"]
        nicknames = [
            connect.build_nickname("vg", "vol1", "iqn.example:vol1", index) for index in (1, 2, 3)
        ]
        plan = connect.VolumeConnectionPlan(
            "vol1", "iqn.example:vol1", "portal.example:3260", nicknames, addresses
        )
        connected = [
            {"Target name": "iqn.example:vol1", "Session state": "Connected", "Enable": "Yes"}
        ] * 3
        with mock.patch.object(connect, "ensure_freebsd_prerequisites"):
            with mock.patch.object(
                connect, "list_iscsi_sessions", side_effect=[[], [], connected]
            ):
                with mock.patch.object(connect, "add_iscsi_session") as add_mock:
                    connected_volumes = connect.execute_connection_plan(
                        [plan], self.config_path, 3
                    )
        self.assertEqual(["vol1"], connected_volumes)
        self.assertEqual(nicknames, [call.args[0] for call in add_mock.call_args_list])
        with open(self.config_path, "rb") as handle:
            entries = connect.read_managed_entries(handle.read())
        self.assertEqual(
            sorted(zip(nicknames, addresses)),
            sorted((entry.nickname, entry.target_address) for entry in entries),
        )

    def test_volume_connected_with_another_layout_is_skipped_without_conflict(self):
        existing = connect.render_managed_block(
            [connect.ManagedConfigEntry("esan-vg-vol1-s1", "iqn.example:vol1", "10.9.9.9:3260")]
        )
        with open(self.config_path, "wb") as handle:
            handle.write(existing)
        live = [{"Target name": "iqn.example:vol1:az-eastus-az1", "Session state": "Connected"}]
        with mock.patch.object(connect, "ensure_freebsd_prerequisites"):
            with mock.patch.object(connect, "list_iscsi_sessions", return_value=live):
                with mock.patch.object(connect, "add_iscsi_session") as add_mock:
                    connected_volumes = connect.execute_connection_plan(
                        self._plans(), self.config_path, 1
                    )
        self.assertEqual([], connected_volumes)
        add_mock.assert_not_called()
        with open(self.config_path, "rb") as handle:
            self.assertEqual(existing, handle.read())

    def test_malformed_existing_block_is_rejected_before_service_mutation(self):
        with open(self.config_path, "wb") as handle:
            handle.write(
                connect.MANAGED_BLOCK_BEGIN.encode("utf-8")
                + b"\nunknown\n"
                + connect.MANAGED_BLOCK_END.encode("utf-8")
                + b"\n"
            )
        with mock.patch.object(connect, "ensure_freebsd_prerequisites") as ensure:
            with self.assertRaisesRegex(connect.ElasticSanConnectError, "unknown"):
                connect.execute_connection_plan(self._plans(), self.config_path, 1)
        ensure.assert_not_called()

    def test_concurrent_transactions_serialize_without_lost_updates(self):
        first_plan = connect.VolumeConnectionPlan(
            "vol1",
            "iqn.example:vol1",
            "10.0.0.4:3260",
            ["esan-vg-vol1-1111111111111111-s1"],
        )
        second_plan = connect.VolumeConnectionPlan(
            "vol2",
            "iqn.example:vol2",
            "10.0.0.5:3260",
            ["esan-vg-vol2-2222222222222222-s1"],
        )
        blocking_fcntl = BlockingFcntl()
        first_in_session_setup = threading.Event()
        allow_first_to_finish = threading.Event()
        session_setup_volumes = []
        errors = []

        def add_sessions(plan, _config_path):
            session_setup_volumes.append(plan.volume_name)
            if plan.volume_name == "vol1":
                first_in_session_setup.set()
                if not allow_first_to_finish.wait(5):
                    raise AssertionError("test timed out waiting to release first transaction")

        def execute(plan):
            try:
                connect.execute_connection_plan([plan], self.config_path, 1)
            except BaseException as error:
                errors.append(error)

        with mock.patch.object(connect, "fcntl", blocking_fcntl):
            with mock.patch.object(connect, "ensure_freebsd_prerequisites"):
                with mock.patch.object(connect, "list_iscsi_sessions", return_value=[]):
                    with mock.patch.object(
                        connect, "add_sessions_for_volume", side_effect=add_sessions
                    ):
                        first = threading.Thread(target=execute, args=(first_plan,))
                        second = threading.Thread(target=execute, args=(second_plan,))
                        first.start()
                        self.assertTrue(first_in_session_setup.wait(5))
                        second.start()
                        self.assertTrue(blocking_fcntl.second_acquire_attempted.wait(5))
                        self.assertEqual(["vol1"], session_setup_volumes)
                        allow_first_to_finish.set()
                        first.join(5)
                        second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual([], errors)
        with open(self.config_path, "rb") as handle:
            content = handle.read()
        self.assertIn(first_plan.nicknames[0].encode("ascii"), content)
        self.assertIn(second_plan.nicknames[0].encode("ascii"), content)


def _session(target_name, state="Connected", enable="Yes"):
    return {
        "Target name": target_name,
        "Target portal": "portal.example:3260",
        "Session state": state,
        "Enable": enable,
        "Header digest": "CRC32C",
        "Data digest": "CRC32C",
    }


def _volume_plan(volume_name, target_name=None, volume_group_name="vg"):
    target_name = target_name or "iqn.example:" + volume_name
    return connect.VolumeConnectionPlan(
        volume_name,
        target_name,
        "portal.example:3260",
        [connect.build_nickname(volume_group_name, volume_name, target_name, 1)],
    )


class ExistingStatePolicyTests(unittest.TestCase):
    """Section 4 policy: inventory every volume before mutating any of them and
    never add a second stanza or session for an IQN."""

    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="esan-freebsd-state-test-")
        self.config_path = os.path.join(self.tempdir, "iscsi.conf")
        self.fcntl_patch = mock.patch.object(connect, "fcntl", RecordingFcntl())
        self.fcntl_patch.start()

    def tearDown(self):
        self.fcntl_patch.stop()
        for name in os.listdir(self.tempdir):
            os.remove(os.path.join(self.tempdir, name))
        os.rmdir(self.tempdir)

    def _write_config(self, *entries):
        content = b"# operator content\n" + connect.render_managed_block(list(entries))
        with open(self.config_path, "wb") as handle:
            handle.write(content)
        return content

    def _read_config(self):
        with open(self.config_path, "rb") as handle:
            return handle.read()

    def _execute(self, plans, session_listings):
        with mock.patch.object(connect, "ensure_freebsd_prerequisites"):
            with mock.patch.object(connect, "list_iscsi_sessions", side_effect=session_listings):
                with mock.patch.object(connect, "add_iscsi_session") as add_mock:
                    with mock.patch("builtins.print") as print_mock:
                        connected = connect.execute_connection_plan(plans, self.config_path, 1)
        output = "\n".join(str(call.args[0]) for call in print_mock.call_args_list)
        return connected, add_mock, output

    def _entry(self, plan):
        return connect.ManagedConfigEntry(plan.nicknames[0], plan.target_name, plan.target_address)

    def test_managed_and_live_volume_is_skipped_without_config_write(self):
        plan = _volume_plan("vol1")
        original = self._write_config(self._entry(plan))
        connected, add_mock, output = self._execute([plan], [[_session(plan.target_name)]])
        self.assertEqual([], connected)
        add_mock.assert_not_called()
        self.assertIn("vol1 [iqn.example:vol1]: Skipped: already connected (1 live / 1 persistent)", output)
        self.assertEqual(original, self._read_config())
        self.assertEqual(
            sorted(["iscsi.conf", connect.CONFIG_LOCK_FILENAME]), sorted(os.listdir(self.tempdir))
        )

    def test_own_managed_entry_without_live_session_is_reestablished_once(self):
        plan = _volume_plan("vol1")
        self._write_config(self._entry(plan))
        connected, add_mock, _ = self._execute(
            [plan], [[], [], [_session(plan.target_name)]]
        )
        self.assertEqual(["vol1"], connected)
        add_mock.assert_called_once_with(plan.nicknames[0], self.config_path)
        self.assertEqual(1, self._read_config().count(b'TargetName    = "iqn.example:vol1"'))

    def test_foreign_entry_for_same_iqn_is_never_duplicated(self):
        # Differently cased resource names hash to a different nickname for the same IQN.
        plan = _volume_plan("vol1")
        foreign = _volume_plan("Vol1", "IQN.EXAMPLE:VOL1", volume_group_name="VG")
        original = self._write_config(self._entry(foreign))
        connected, add_mock, output = self._execute([plan], [[]])
        self.assertEqual([], connected)
        add_mock.assert_not_called()
        self.assertIn("persistent configuration exists but no live sessions", output)
        self.assertIn("'{}'".format(foreign.nicknames[0]), output)
        self.assertIn("iscsictl -A -n <nickname>' for each entry", output)
        self.assertEqual(original, self._read_config())

    def test_live_session_without_managed_entry_is_not_made_persistent(self):
        plan = _volume_plan("vol1")
        connected, add_mock, output = self._execute([plan], [[_session("IQN.example:VOL1")]])
        self.assertEqual([], connected)
        add_mock.assert_not_called()
        self.assertIn("(1 live / 0 persistent); the sessions are not persistent", output)
        self.assertFalse(os.path.exists(self.config_path))

    def test_pre_existing_anomaly_is_skipped_while_other_volumes_connect(self):
        healthy = _volume_plan("vol1")
        anomalous = _volume_plan("vol2")
        anomalies = {
            "not connected": [_session(anomalous.target_name, state="Disconnected")],
            "disabled": [_session(anomalous.target_name), _session(anomalous.target_name, enable="No")],
        }
        for label, anomaly_sessions in anomalies.items():
            with self.subTest(anomaly=label):
                if os.path.exists(self.config_path):
                    os.remove(self.config_path)
                connected, add_mock, output = self._execute(
                    [healthy, anomalous],
                    [anomaly_sessions, anomaly_sessions, anomaly_sessions + [_session(healthy.target_name)]],
                )
                self.assertEqual(["vol1"], connected)
                add_mock.assert_called_once_with(healthy.nicknames[0], self.config_path)
                self.assertIn("vol2 [iqn.example:vol2]: Skipped: pre-existing session anomaly", output)
                content = self._read_config()
                self.assertIn(healthy.nicknames[0].encode("ascii"), content)
                self.assertNotIn(anomalous.nicknames[0].encode("ascii"), content)

    def test_failed_volume_rollback_keeps_entries_of_volumes_connected_earlier(self):
        first = _volume_plan("vol1")
        second = _volume_plan("vol2")
        backup_path = "{}.bak.pre-esan-connect.{}".format(self.config_path, os.getpid())
        for config_existed in (True, False):
            with self.subTest(config_existed=config_existed):
                for name in os.listdir(self.tempdir):
                    os.remove(os.path.join(self.tempdir, name))
                original = self._write_config() if config_existed else b""
                with mock.patch.object(connect, "ensure_freebsd_prerequisites"):
                    with mock.patch.object(
                        connect,
                        "list_iscsi_sessions",
                        side_effect=[
                            [], [], [_session(first.target_name)], [_session(first.target_name)]
                        ],
                    ):
                        with mock.patch.object(
                            connect,
                            "add_iscsi_session",
                            side_effect=[None, connect.ElasticSanConnectError("kernel rejected vol2")],
                        ):
                            with mock.patch("builtins.print"):
                                with self.assertRaisesRegex(
                                    connect.ElasticSanConnectError,
                                    r"sessions for 'vol2'; restored .* before this volume.* earlier "
                                    r"in this run keep their sessions and entries: 'vol1'\..* "
                                    r"Original error: kernel rejected vol2",
                                ):
                                    connect.execute_connection_plan(
                                        [first, second], self.config_path, 1
                                    )
                # The config is exactly the state after vol1 connected.
                self.assertEqual(
                    connect.compute_new_config_content(
                        original, connect.render_full_managed_block([first])
                    ),
                    self._read_config(),
                )
                self.assertNotIn(second.nicknames[0].encode("ascii"), self._read_config())
                # The on-disk backup is still the pre-run config (none if there was no file).
                if config_existed:
                    with open(backup_path, "rb") as handle:
                        self.assertEqual(original, handle.read())
                else:
                    self.assertFalse(os.path.exists(backup_path))


class ValidationTests(unittest.TestCase):
    PLAN = _volume_plan("vol1")
    HEALTHY_HOST = {
        "iscsid_enable": "YES",
        "iscsictl_flags": "-Aa",
        "iscsictl_enable": "YES",
        "iscsi_load": "YES",
    }

    def _validate(self, connected=True, host=None, driver="module", sessions=None, entries=None):
        values = dict(self.HEALTHY_HOST, **(host or {}))
        if sessions is None:
            sessions = [_session(self.PLAN.target_name)]
        if entries is None:
            entries = [
                connect.ManagedConfigEntry(
                    self.PLAN.nicknames[0], self.PLAN.target_name, self.PLAN.target_address
                )
            ]
        content = connect.render_managed_block(entries) if entries else b""
        list_sessions = mock.Mock(
            side_effect=sessions if isinstance(sessions, Exception) else None,
            return_value=sessions,
        )
        with mock.patch.object(
            connect, "_query_sysrc", side_effect=lambda variable, config_file=None: values[variable]
        ), mock.patch.object(connect, "get_iscsi_driver_state", return_value=driver), \
                mock.patch.object(connect, "list_iscsi_sessions", list_sessions), \
                mock.patch.object(connect, "read_config_file", return_value=(content, True, None)), \
                mock.patch("builtins.print") as print_mock:
            failed = connect.validate_connections(
                [self.PLAN], ["vol1"] if connected else [], "/etc/iscsi.conf"
            )
        return failed, [call.args[0] for call in print_mock.call_args_list]

    def test_healthy_host_passes_with_loaded_or_compiled_in_driver(self):
        for driver, detail in (
            ("module", 'iscsi.ko loaded; iscsi_load="YES" in /boot/loader.conf'),
            ("compiled-in", "compiled into the kernel"),
        ):
            with self.subTest(driver=driver):
                failed, lines = self._validate(driver=driver)
                self.assertEqual(0, failed)
                self.assertIn("[PASS] iSCSI kernel driver: " + detail, lines)
                self.assertIn("[PASS] vol1 [iqn.example:vol1] live sessions: 1 Connected", lines)
                self.assertIn(
                    "[PASS] vol1 [iqn.example:vol1] digests: header CRC32C, data CRC32C", lines
                )
                self.assertIn("[PASS] vol1 [iqn.example:vol1] managed entries: 1", lines)
                self.assertEqual("Validation: 6 passed, 0 warnings, 0 failed", lines[-1])

    def test_host_problems_fail_and_customized_iscsictl_flags_warn(self):
        cases = [
            ({"iscsid_enable": "NO"}, "module", "[FAIL] iscsid_enable: NO"),
            ({"iscsictl_enable": "NO"}, "module", "[FAIL] iscsictl_enable: NO"),
            ({"iscsi_load": ""}, "module", "[FAIL] iSCSI kernel driver: iscsi.ko loaded but"),
            ({}, "missing", "[FAIL] iSCSI kernel driver: not loaded"),
            ({"iscsictl_flags": "-A -n other", "iscsictl_enable": "NO"}, "module",
             "[WARN] iscsictl_enable: NO with customized iscsictl_flags '-A -n other'"),
            ({"iscsictl_flags": ""}, "module",
             "[WARN] iscsictl_enable: YES with customized iscsictl_flags ''"),
        ]
        for host, driver, expected in cases:
            with self.subTest(expected=expected):
                failed, lines = self._validate(host=host, driver=driver)
                self.assertTrue(any(line.startswith(expected) for line in lines), lines)
                self.assertEqual(0 if expected.startswith("[WARN]") else 1, failed)

    def test_volume_problems_fail_when_connected_this_run_and_warn_when_skipped(self):
        weak_session = dict(_session(self.PLAN.target_name), **{"Header digest": "None"})
        for connected, severity in ((True, "FAIL"), (False, "WARN")):
            with self.subTest(connected=connected):
                failed, lines = self._validate(
                    connected=connected, sessions=[weak_session], entries=[]
                )
                self.assertIn(
                    "[{}] vol1 [iqn.example:vol1] digests: header None, data CRC32C; expected "
                    "header CRC32C, data CRC32C".format(severity),
                    lines,
                )
                self.assertIn(
                    "[{}] vol1 [iqn.example:vol1] managed entries: none in /etc/iscsi.conf; the "
                    "sessions are not persistent".format(severity),
                    lines,
                )
                self.assertEqual(2 if connected else 0, failed)

    def test_skipped_volume_without_session_warns_and_extra_sessions_always_warn(self):
        _, lines = self._validate(connected=False, sessions=[])
        self.assertIn(
            "[WARN] vol1 [iqn.example:vol1] live sessions: 0 Connected, requested 1", lines
        )
        self.assertFalse(any("digests" in line for line in lines))

        failed, lines = self._validate(
            sessions=[_session(self.PLAN.target_name), _session(self.PLAN.target_name)]
        )
        self.assertEqual(0, failed)
        self.assertIn(
            "[WARN] vol1 [iqn.example:vol1] live sessions: 2 Connected, requested 1; this script "
            "never removes sessions",
            lines,
        )

    def test_counts_are_checked_against_the_requested_sessions(self):
        plan = connect.VolumeConnectionPlan(
            "vol1", "iqn.example:vol1", "portal.example:3260",
            [connect.build_nickname("vg", "vol1", "iqn.example:vol1", index) for index in (1, 2, 3)],
            ["10.0.0.1:3260", "10.0.0.2:3260", "10.0.0.3:3260"],
        )
        entries = plan.entries()
        with mock.patch.object(self, "PLAN", plan):
            failed, lines = self._validate(sessions=[_session(plan.target_name)] * 3, entries=entries)
            self.assertEqual(0, failed)
            self.assertIn("[PASS] vol1 [iqn.example:vol1] live sessions: 3 Connected", lines)
            self.assertIn("[PASS] vol1 [iqn.example:vol1] managed entries: 3", lines)

            failed, lines = self._validate(
                sessions=[_session(plan.target_name)] * 2
                + [_session(plan.target_name, state="Connecting")],
                entries=entries[:2],
            )
            self.assertEqual(2, failed)
            self.assertIn(
                "[FAIL] vol1 [iqn.example:vol1] live sessions: 2 Connected, requested 3; not "
                "Connected: Connecting",
                lines,
            )
            self.assertIn(
                "[FAIL] vol1 [iqn.example:vol1] managed entries: 2 in /etc/iscsi.conf, requested 3; "
                "only those sessions are re-added at boot",
                lines,
            )

            # Sessions of another zonal mode still count for the volume.
            failed, lines = self._validate(
                connected=False, sessions=[_session("iqn.example:vol1:az-eastus-az1")] * 3,
                entries=entries,
            )
            self.assertEqual(0, failed)
            self.assertIn("[PASS] vol1 [iqn.example:vol1] live sessions: 3 Connected", lines)

    def test_session_inventory_failure_is_a_single_fail(self):
        failed, lines = self._validate(
            sessions=connect.ElasticSanConnectError("iscsictl session listing failed: boom")
        )
        self.assertEqual(1, failed)
        self.assertIn(
            "[FAIL] iSCSI sessions: could not list sessions: iscsictl session listing failed: boom",
            lines,
        )
        self.assertFalse(any(" live sessions: " in line for line in lines))


class MainFlowTests(unittest.TestCase):
    ARGV = ["-g", "rg", "-e", "san", "-v", "vg", "-n", "vol1"]

    def test_non_root_mutating_run_stops_before_any_azure_lookup(self):
        with mock.patch.object(connect.os, "geteuid", return_value=1000, create=True):
            with mock.patch.object(connect, "check_az_cli_available") as check_az:
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "must be run as root"):
                    connect.main(self.ARGV)
        check_az.assert_not_called()

    def _main(self, execute, failed_checks=0):
        with mock.patch.object(connect, "check_freebsd_mutation_prerequisites"), \
                mock.patch.object(connect, "check_az_cli_available"), \
                mock.patch.object(connect, "build_connection_plan", return_value=[_volume_plan("vol1")]), \
                mock.patch.object(connect, "execute_connection_plan", execute), \
                mock.patch.object(connect, "validate_connections", return_value=failed_checks) as validate:
            connect.main(self.ARGV)
        return validate

    def test_validation_failure_ends_the_run_in_error(self):
        with self.assertRaisesRegex(connect.ElasticSanConnectError, "reported 2 failed check"):
            self._main(mock.Mock(return_value=["vol1"]), failed_checks=2)

    def test_successful_validation_returns_normally(self):
        validate = self._main(mock.Mock(return_value=["vol1"]))
        self.assertEqual(["vol1"], validate.call_args.args[1])

    def test_connect_phase_failure_stops_without_validation(self):
        execute = mock.Mock(side_effect=connect.ElasticSanConnectError("Failed while establishing"))
        with mock.patch.object(connect, "validate_connections") as validate:
            with mock.patch.object(connect, "check_freebsd_mutation_prerequisites"), \
                    mock.patch.object(connect, "check_az_cli_available"), \
                    mock.patch.object(connect, "build_connection_plan", return_value=[_volume_plan("vol1")]), \
                    mock.patch.object(connect, "execute_connection_plan", execute):
                with self.assertRaisesRegex(connect.ElasticSanConnectError, "Failed while establishing"):
                    connect.main(self.ARGV)
        validate.assert_not_called()

    def test_script_reports_stops_as_one_error_line_without_traceback(self):
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "-g", "rg"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(1, result.returncode)
        self.assertEqual(
            "ERROR: Need to provide resource_group_name, elastic_san_name, volume_group_name, "
            "volume_names to connect to the Elastic SAN volume(s)",
            result.stderr.strip(),
        )
        self.assertNotIn("Traceback", result.stderr)


class EnabledInputPreflightTests(unittest.TestCase):
    def test_malformed_later_portal_fails_before_execution(self):
        invalid_portals = [
            (None, 3260), ("", 3260), (" ", 3260), (True, 3260), ([], 3260),
            ("portal..example", 3260), ("portal.example/path", 3260),
            ("portal.example", None), ("portal.example", True),
            ("portal.example", 0), ("portal.example", 70000),
            ("portal.example", 3260.5), ("portal.example", "3260"),
        ]
        for enabled in (False, True):
            for dry_run in (False, True):
                for hostname, port in invalid_portals:
                    with self.subTest(enabled=enabled, dry_run=dry_run, hostname=hostname, port=port):
                        argv = ["-g", "rg", "-e", "san", "-v", "vg", "-n", "first", "second"]
                        if enabled:
                            argv.append("--enable-zonal-affinity")
                        if dry_run:
                            argv.append("--dry-run")
                        with mock.patch.object(connect, "check_freebsd_mutation_prerequisites"), \
                                mock.patch.object(connect, "check_az_cli_available"):
                            with mock.patch.object(connect, "resolve_physical_zone", return_value=("eastus-az3", "sub")):
                                with mock.patch.object(connect, "get_volume_storage_target", side_effect=[
                                    ("iqn.example:first", "first.example", 3260),
                                    ("iqn.example:second", hostname, port),
                                ]):
                                    with mock.patch.object(connect, "execute_connection_plan") as execute:
                                        with self.assertRaises(connect.ElasticSanConnectError):
                                            connect.main(argv)
                        execute.assert_not_called()

    def test_enabled_volume_queries_pin_resolved_subscription_after_context_change(self):
        subscription_id = "00000000-0000-0000-0000-000000000001"
        compute = {"zone": "2", "subscriptionId": subscription_id, "location": "eastus"}
        for requested_subscription in (None, "friendly-name", subscription_id):
            with self.subTest(requested_subscription=requested_subscription):
                commands = []
                context_changed = False

                def run(command, description):
                    nonlocal context_changed
                    commands.append(command)
                    if command[:3] == ["az", "account", "show"]:
                        self.assertFalse(context_changed)
                        return subscription_id
                    if command[:3] == ["az", "elastic-san", "show"]:
                        return "eastus"
                    if command[:2] == ["az", "rest"]:
                        context_changed = True
                        return locations_payload()
                    self.assertTrue(context_changed)
                    self.assertEqual(["--subscription", subscription_id], command[-2:])
                    volume = command[command.index("-n") + 1]
                    return json.dumps({
                        "targetIqn": "iqn.example:" + volume,
                        "targetPortalHostname": volume + ".example",
                        "targetPortalPort": 3260,
                    })

                with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
                    with mock.patch.object(connect, "_run_az_command", side_effect=run):
                        plans = connect.build_connection_plan(
                            requested_subscription, "rg", "san", "vg", ["first", "second"], 1, True
                        )
                self.assertEqual(1, sum(command[:3] == ["az", "account", "show"] for command in commands))
                self.assertEqual(
                    ["iqn.example:first:az-eastus-az3", "iqn.example:second:az-eastus-az3"],
                    [plan.target_name for plan in plans],
                )

    def test_later_invalid_volume_fails_before_execution_in_live_and_dry_run_modes(self):
        valid_target = ("iqn.2024-01.com.microsoft:first", "portal.example", 3260)
        invalid_targets = [
            ("iqn.2024-01.com.microsoft:Second", "portal.example", 3260),
            ("iqn.2024-01.com.microsoft:second\u00e9", "portal.example", 3260),
            ("i" * 211, "portal.example", 3260),
            (None, "portal.example", 3260),
            ("iqn.2024-01.com.microsoft:second", 'portal"example', 3260),
        ]
        for dry_run in (False, True):
            for invalid_target in invalid_targets:
                with self.subTest(dry_run=dry_run, invalid_target=invalid_target):
                    argv = [
                        "-g", "rg", "-e", "san", "-v", "vg", "-n", "first", "second",
                        "--enable-zonal-affinity",
                    ]
                    if dry_run:
                        argv.append("--dry-run")
                    with mock.patch.object(connect, "check_freebsd_mutation_prerequisites"), \
                            mock.patch.object(connect, "check_az_cli_available"):
                        with mock.patch.object(
                            connect, "resolve_physical_zone", return_value=("eastus-az3", "sub")
                        ):
                            with mock.patch.object(
                                connect, "get_volume_storage_target",
                                side_effect=[valid_target, invalid_target],
                            ) as storage_target:
                                with mock.patch.object(connect, "execute_connection_plan") as execute:
                                    with mock.patch.object(connect, "_run_subprocess") as run:
                                        with self.assertRaises(connect.ElasticSanConnectError):
                                            connect.main(argv)
                    self.assertEqual(2, storage_target.call_count)
                    execute.assert_not_called()
                    run.assert_not_called()

    def test_enabled_discovery_uses_explicit_subscription_rest_and_plans_all_fqdns(self):
        subscription_id = "00000000-0000-0000-0000-000000000001"
        compute = {"zone": "2", "subscriptionId": subscription_id, "location": "eastus"}
        targets = [
            {
                "targetIqn": "iqn.2024-01.com.microsoft:" + volume,
                "targetPortalHostname": volume + ".example",
                "targetPortalPort": 3260,
            }
            for volume in ("first", "second")
        ]
        outputs = [
            "elastic-san\n", subscription_id, "eastus", locations_payload(),
        ] + [json.dumps(target) for target in targets]
        with mock.patch.object(connect.shutil, "which", return_value="az"):
            with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
                with mock.patch.object(
                    connect, "_run_subprocess",
                    side_effect=[(0, output, "") for output in outputs],
                ) as run:
                    with mock.patch.object(connect, "check_freebsd_mutation_prerequisites"), \
                            mock.patch.object(connect, "validate_connections", return_value=0), \
                            mock.patch.object(connect, "execute_connection_plan") as execute:
                        connect.main([
                            "--elastic-san-subscription", subscription_id,
                            "-g", "rg", "-e", "san", "-v", "vg", "-n", "first", "second",
                            "--enable-zonal-affinity",
                        ])

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([
            "az", "elastic-san", "show", "-g", "rg", "--elastic-san-name", "san",
            "--subscription", subscription_id, "--query", "location", "--output", "tsv",
        ], commands[2])
        self.assertEqual([
            "az", "rest", "--method", "get", "--url",
            "https://management.azure.com/subscriptions/{}/locations"
            "?api-version=2022-12-01".format(subscription_id),
        ], commands[3])
        for command in commands[4:]:
            self.assertEqual(["--subscription", subscription_id], command[-2:])
        for call in run.call_args_list:
            self.assertEqual(connect.AZ_CLI_TIMEOUT_SECONDS, call.args[2])
        execute.assert_called_once()
        plans, _, session_count = execute.call_args.args
        self.assertEqual(32, session_count)
        self.assertEqual(
            [target["targetIqn"] + ":az-eastus-az3" for target in targets],
            [plan.target_name for plan in plans],
        )
        self.assertEqual(
            [target["targetIqn"] for target in targets],
            [plan.volume_iqn for plan in plans],
        )
        self.assertEqual(
            ["first.example:3260", "second.example:3260"],
            [plan.target_address for plan in plans],
        )
        for plan in plans:
            self.assertEqual(32, len(plan.nicknames))
            self.assertEqual([plan.target_address] * 32, plan.session_addresses)


class DryRunTests(unittest.TestCase):
    def test_dry_run_performs_no_mutation_and_calls_no_freebsd_tooling(self):
        argv = [
            "-g",
            "rg",
            "-e",
            "san",
            "-v",
            "vg",
            "-n",
            "volume1",
            "-s",
            "1",
            "--dry-run",
        ]
        with mock.patch.object(connect, "check_az_cli_available") as check_az:
            with mock.patch.object(
                connect,
                "get_volume_storage_target",
                return_value=("iqn.original", "portal.example", 3260),
            ):
                with mock.patch.object(connect, "check_freebsd_mutation_prerequisites") as preflight:
                    with mock.patch.object(connect, "ensure_iscsid_enabled_and_running") as ensure_iscsid:
                        with mock.patch.object(connect, "_run_command") as run_command:
                            with mock.patch.object(connect, "_run_subprocess") as run_subprocess:
                                with mock.patch.object(connect.os, "replace") as os_replace:
                                    with mock.patch("builtins.open", mock.mock_open()) as open_mock:
                                        with mock.patch.object(
                                            connect, "acquire_config_transaction_lock"
                                        ) as acquire_lock:
                                            connect.main(argv)

        check_az.assert_called_once()
        preflight.assert_not_called()
        ensure_iscsid.assert_not_called()
        run_command.assert_not_called()
        run_subprocess.assert_not_called()
        os_replace.assert_not_called()
        open_mock.assert_not_called()
        acquire_lock.assert_not_called()

    def test_dry_run_does_not_require_root(self):
        argv = ["-g", "rg", "-e", "san", "-v", "vg", "-n", "volume1", "--dry-run"]
        with mock.patch.object(connect, "check_az_cli_available"):
            with mock.patch.object(
                connect,
                "get_volume_storage_target",
                return_value=("iqn.original", "portal.example", 3260),
            ):
                with mock.patch.object(connect.os, "geteuid", create=True) as geteuid:
                    connect.main(argv)
        geteuid.assert_not_called()

    def test_dry_run_with_zonal_affinity_still_performs_only_reads(self):
        argv = [
            "-g",
            "rg",
            "-e",
            "san",
            "-v",
            "vg",
            "-n",
            "volume1",
            "--enable-zonal-affinity",
            "--dry-run",
        ]
        with mock.patch.object(connect, "check_az_cli_available"):
            with mock.patch.object(
                connect, "resolve_physical_zone", return_value=("eastus-az1", "sub")
            ) as resolve_zone:
                with mock.patch.object(
                    connect,
                    "get_volume_storage_target",
                    return_value=("iqn.original", "portal.example", 3260),
                ):
                    with mock.patch.object(connect, "check_freebsd_mutation_prerequisites") as preflight:
                        connect.main(argv)
        resolve_zone.assert_called_once()
        preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
