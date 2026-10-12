import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock


SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "connect_for_documentation.py")
SPEC = importlib.util.spec_from_file_location("connect_for_documentation", SCRIPT_PATH)
connect = importlib.util.module_from_spec(SPEC)
with mock.patch("os.path.exists", side_effect=lambda path: path == "/usr/bin/apt-get"):
    SPEC.loader.exec_module(connect)


def completed_process(stdout="", stderr="", returncode=0):
    def launch(command, **kwargs):
        kwargs["stdout"].write(stdout.encode("utf-8"))
        kwargs["stderr"].write(stderr.encode("utf-8"))
        kwargs["stdout"].flush()
        kwargs["stderr"].flush()
        process = mock.Mock()
        process.returncode = returncode
        return process
    return launch


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


def locations_list(region_name="eastus", mappings=None):
    return json.loads(locations_payload(region_name, mappings))["value"]


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

    def test_success_resolves_current_subscription_and_normalizes_region(self):
        subscription_id = "00000000-0000-0000-0000-000000000001"
        processes = [
            completed_process(subscription_id + "\n"),
            completed_process(" eastus \n"),
            completed_process(locations_payload(region_name="EaStUs")),
        ]
        compute = {
            "zone": "2",
            "subscriptionId": subscription_id.upper(),
            "location": "EASTUS",
        }

        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with mock.patch.object(
                connect.subprocess, "Popen",
                side_effect=lambda command, **kwargs: processes.pop(0)(command, **kwargs),
            ) as popen:
                physical_zone = connect.resolve_physical_zone(None, "rg", "san")

        self.assertEqual("eastus-az3", physical_zone)
        self.assertEqual(
            ["az", "account", "show", "--query", "id", "--output", "tsv"],
            popen.call_args_list[0].args[0],
        )
        self.assertEqual(
            [
                "az",
                "elastic-san",
                "show",
                "-g",
                "rg",
                "--elastic-san-name",
                "san",
                "--subscription",
                subscription_id,
                "--query",
                "location",
                "--output",
                "tsv",
            ],
            popen.call_args_list[1].args[0],
        )
        self.assertEqual(
            [
                "az",
                "rest",
                "--method",
                "get",
                "--url",
                (
                    "https://management.azure.com/subscriptions/"
                    "00000000-0000-0000-0000-000000000001/locations"
                    "?api-version=2022-12-01"
                ),
                "--output",
                "json",
            ],
            popen.call_args_list[2].args[0],
        )

    def test_explicit_subscription_must_match_imds_subscription(self):
        cli_subscription_id = "00000000-0000-0000-0000-000000000001"
        compute = {
            "zone": "1",
            "subscriptionId": "00000000-0000-0000-0000-000000000002",
            "location": "eastus",
        }

        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with mock.patch.object(
                connect.subprocess,
                "Popen",
                side_effect=completed_process(cli_subscription_id),
            ) as popen:
                with self.assertRaisesRegex(
                    connect.ZonalAffinityError,
                    "Elastic SAN subscription .* does not match VM subscription",
                ):
                    connect.resolve_physical_zone(
                        "production-subscription", "rg", "san"
                    )

        self.assertEqual(
            [
                "az",
                "account",
                "show",
                "--subscription",
                "production-subscription",
                "--query",
                "id",
                "--output",
                "tsv",
            ],
            popen.call_args.args[0],
        )
        self.assertEqual(1, popen.call_count)

    def test_non_zonal_vm_is_rejected(self):
        compute = {
            "zone": "",
            "subscriptionId": "00000000-0000-0000-0000-000000000001",
            "location": "eastus",
        }

        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with self.assertRaisesRegex(connect.ZonalAffinityError, "not availability-zone pinned"):
                connect.resolve_physical_zone(None, "rg", "san")

    def test_imds_failure_is_explicit(self):
        opener = mock.Mock()
        opener.open.side_effect = connect.URLError("unreachable")

        with mock.patch.object(connect, "build_opener", return_value=opener):
            with self.assertRaisesRegex(connect.ZonalAffinityError, "IMDS request failed"):
                connect.get_vm_compute_metadata()

    def test_cli_failure_is_explicit(self):
        compute = {
            "zone": "1",
            "subscriptionId": "00000000-0000-0000-0000-000000000001",
            "location": "eastus",
        }
        process = completed_process(stderr="Please run 'az login'.", returncode=1)

        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with mock.patch.object(connect.subprocess, "Popen", side_effect=process):
                with self.assertRaisesRegex(
                    connect.ZonalAffinityError, "Please run 'az login'"
                ):
                    connect.resolve_physical_zone(None, "rg", "san")

    def test_invalid_cli_subscription_guid_is_rejected_before_url_construction(self):
        with mock.patch.object(
            connect.subprocess,
            "Popen",
            side_effect=completed_process("../../other-resource"),
        ) as popen:
            with self.assertRaisesRegex(connect.ZonalAffinityError, "must be a GUID"):
                connect.resolve_elastic_san_subscription_id(None)

        self.assertEqual(1, popen.call_count)

    def test_location_url_rejects_invalid_subscription_guid_before_cli_call(self):
        with mock.patch.object(connect, "_run_az_command") as run:
            with self.assertRaisesRegex(connect.ZonalAffinityError, "must be a GUID"):
                connect.get_azure_locations("../../other-resource")

        run.assert_not_called()

    def test_invalid_imds_subscription_guid_is_rejected(self):
        compute = {
            "zone": "1",
            "subscriptionId": "not-a-guid",
            "location": "eastus",
        }

        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with mock.patch.object(connect.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(
                    connect.ZonalAffinityError, "IMDS subscription ID must be a GUID"
                ):
                    connect.resolve_physical_zone(None, "rg", "san")

        popen.assert_not_called()

    def test_azure_cli_timeout_kills_and_reaps_process(self):
        for platform in ("posix", "nt"):
            with self.subTest(platform=platform):
                process = mock.Mock(pid=1234)
                process.wait.side_effect = [subprocess.TimeoutExpired("az", 30), -9]
                with mock.patch.object(connect.subprocess, "Popen", return_value=process) as popen:
                    with mock.patch.object(connect.os, "name", platform):
                        with mock.patch.object(connect.os, "killpg", create=True) as killpg:
                            with mock.patch.object(connect.signal, "SIGKILL", 9, create=True):
                                with self.assertRaisesRegex(connect.ZonalAffinityError, "timed out"):
                                    connect._run_az_command(["az", "account", "show"], "Azure CLI test")
                self.assertEqual([mock.call(timeout=30), mock.call(timeout=30)], process.wait.call_args_list)
                self.assertEqual(platform == "posix", popen.call_args.kwargs["start_new_session"])
                if platform == "posix":
                    killpg.assert_called_once_with(process.pid, 9)
                    process.kill.assert_not_called()
                else:
                    process.kill.assert_called_once_with()
                    killpg.assert_not_called()
                self.assertTrue(popen.call_args.kwargs["stdout"].closed)
                self.assertTrue(popen.call_args.kwargs["stderr"].closed)

    def test_location_rest_response_requires_top_level_value_array(self):
        for payload in ("[]", "{}", '{"value": {}}'):
            with self.subTest(payload=payload):
                with mock.patch.object(connect, "_run_az_command", return_value=payload):
                    with self.assertRaisesRegex(
                        connect.ZonalAffinityError,
                        "JSON object|top-level 'value' array",
                    ):
                        connect.get_azure_locations(
                            "00000000-0000-0000-0000-000000000001"
                        )

    def test_elastic_san_location_lookup_uses_canonical_subscription(self):
        subscription_id = "00000000-0000-0000-0000-000000000001"
        with mock.patch.object(
            connect, "_run_az_command", return_value=" eastus2 \n"
        ) as run:
            location = connect.get_elastic_san_location(
                subscription_id, "resource-group", "elastic-san"
            )

        self.assertEqual("eastus2", location)
        run.assert_called_once_with(
            [
                "az",
                "elastic-san",
                "show",
                "-g",
                "resource-group",
                "--elastic-san-name",
                "elastic-san",
                "--subscription",
                subscription_id,
                "--query",
                "location",
                "--output",
                "tsv",
            ],
            "Elastic SAN location lookup",
        )

    def test_different_vm_and_elastic_san_regions_are_rejected_before_mapping(self):
        subscription_id = "00000000-0000-0000-0000-000000000001"
        compute = {
            "zone": "2",
            "subscriptionId": subscription_id,
            "location": "eastus",
        }
        processes = [
            completed_process(subscription_id),
            completed_process("westus"),
        ]

        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with mock.patch.object(
                connect.subprocess, "Popen",
                side_effect=lambda command, **kwargs: processes.pop(0)(command, **kwargs),
            ) as popen:
                with self.assertRaisesRegex(
                    connect.ZonalAffinityError,
                    r"^The VM and Elastic SAN must be in the same region\.$",
                ):
                    connect.resolve_physical_zone(None, "rg", "san")

        self.assertEqual(2, popen.call_count)

    def test_elastic_san_subscription_options_parse_identically(self):
        parser = connect.create_argument_parser()
        preferred = parser.parse_args(
            ["--elastic-san-subscription", "san-subscription"]
        )
        compatibility = parser.parse_args(["--subscription", "san-subscription"])

        self.assertEqual(
            "san-subscription", preferred.elastic_san_subscription
        )
        self.assertEqual(
            preferred.elastic_san_subscription,
            compatibility.elastic_san_subscription,
        )
        self.assertFalse(hasattr(preferred, "subscription"))

    def test_missing_region_is_rejected(self):
        with self.assertRaisesRegex(connect.ZonalAffinityError, "no region matching"):
            connect.map_logical_to_physical_zone(
                locations_list(region_name="westus"), "eastus", "2"
            )

    def test_malformed_region_is_rejected(self):
        with self.assertRaisesRegex(connect.ZonalAffinityError, "malformed region entry"):
            connect.map_logical_to_physical_zone([None], "eastus", "2")

    def test_missing_malformed_and_unmatched_zone_mappings_are_rejected(self):
        cases = [
            (
                [{"name": "eastus", "metadata": {"regionType": "Physical"}}],
                "missing availabilityZoneMappings",
            ),
            (
                [
                    {
                        "name": "eastus",
                        "metadata": {"regionType": "Physical"},
                        "availabilityZoneMappings": "invalid",
                    }
                ],
                "malformed availabilityZoneMappings",
            ),
            (
                json.loads(
                    locations_payload(
                        mappings=[{"logicalZone": "2", "physicalZone": ""}]
                    )
                )["value"],
                "malformed entry",
            ),
            (
                json.loads(
                    locations_payload(
                        mappings=[{"logicalZone": "1", "physicalZone": "eastus-az1"}]
                    )
                )["value"],
                "no availability-zone mapping",
            ),
        ]

        for locations, error_pattern in cases:
            with self.subTest(error_pattern=error_pattern):
                with self.assertRaisesRegex(connect.ZonalAffinityError, error_pattern):
                    connect.map_logical_to_physical_zone(locations, "eastus", "2")

    def test_duplicate_trimmed_logical_zones_are_rejected(self):
        locations = locations_list(
            mappings=[
                {"logicalZone": " 2 ", "physicalZone": " eastus-az2 "},
                {"logicalZone": "2", "physicalZone": "eastus-az3"},
            ]
        )

        with self.assertRaisesRegex(
            connect.ZonalAffinityError, "duplicate logical zone '2'"
        ):
            connect.map_logical_to_physical_zone(locations, " eastus ", " 2 ")

    def test_mapping_trims_logical_and_physical_zones(self):
        locations = locations_list(
            mappings=[
                {"logicalZone": " 2 ", "physicalZone": " eastus-az3 "}
            ]
        )

        self.assertEqual(
            "eastus-az3",
            connect.map_logical_to_physical_zone(
                locations, " EASTUS ", " 2 "
            ),
        )

    def test_iqn_suffix_is_lowercase_and_rejects_unsafe_characters(self):
        self.assertEqual(
            "iqn.2024-01.com.microsoft:volume:az-eastus-az3",
            connect.decorate_target_iqn(
                "iqn.2024-01.com.microsoft:volume", "EASTUS-AZ3"
            ),
        )
        with self.assertRaisesRegex(connect.ZonalAffinityError, "unsafe"):
            connect.decorate_target_iqn(
                "iqn.2024-01.com.microsoft:volume", "eastus_az3"
            )

    def test_iqn_length_is_enforced_in_utf8_bytes(self):
        suffix = ":az-eastus-az3"
        allowed_iqn = "i" * (connect.MAX_IQN_UTF8_BYTES - len(suffix))
        self.assertEqual(
            connect.MAX_IQN_UTF8_BYTES,
            len(connect.decorate_target_iqn(allowed_iqn, "eastus-az3").encode("utf-8")),
        )
        with self.assertRaisesRegex(connect.ZonalAffinityError, "223-byte"):
            connect.decorate_target_iqn(allowed_iqn + "x", "eastus-az3")
        with self.assertRaisesRegex(connect.ZonalAffinityError, "223-byte"):
            connect.decorate_target_iqn(allowed_iqn[:-1] + "\u00e9", "eastus-az3")

    def test_opt_out_uses_original_iqn_without_zone_resolution(self):
        with mock.patch.object(
            connect, "resolve_zonal_affinity_context"
        ) as resolve_physical_zone:
            with mock.patch.object(
                connect,
                "get_iqns",
                return_value=("iqn.original", "portal.example", 3260),
            ):
                with mock.patch.object(connect, "check_connection", return_value=False) as check:
                    with mock.patch.object(connect, "connect_volume") as connect_volume:
                        connect.connect_volumes(
                            None, "rg", "san", "vg", ["volume1"], 4, False
                        )

        resolve_physical_zone.assert_not_called()
        check.assert_called_once_with("iqn.original", "portal.example", 3260, volume_iqn="iqn.original")
        connect_volume.assert_called_once_with(
            "volume1", "iqn.original", [("portal.example", 4)], 3260, ()
        )

    def test_opt_in_uses_decorated_iqn_for_connection_commands(self):
        with mock.patch.object(
            connect, "resolve_zonal_affinity_context", return_value=("sub-id", "EASTUS-AZ3")
        ) as resolve_context:
            with mock.patch.object(
                connect,
                "get_mapped_volume_target",
                return_value=("iqn.original", "portal.example", 3260),
            ) as get_target:
                with mock.patch.object(connect, "check_connection", return_value=False) as check:
                    with mock.patch.object(connect, "connect_volume") as connect_volume:
                        connect.connect_volumes(
                            "sub", "rg", "san", "vg", ["volume1"], 4, True
                        )

        decorated_iqn = "iqn.original:az-eastus-az3"
        resolve_context.assert_called_once_with("sub", "rg", "san")
        get_target.assert_called_once_with("sub-id", "rg", "san", "vg", "volume1")
        check.assert_called_once_with(decorated_iqn, "portal.example", 3260, volume_iqn="iqn.original")
        connect_volume.assert_called_once_with(
            "volume1", decorated_iqn, [("portal.example", 4)], 3260, ()
        )

    def test_full_iqn_rejects_unsafe_identity_without_rewriting(self):
        for iqn in (
            None, 123, "", "IQN.original", "iqn.Original", " iqn.original",
            "iqn.original ", "iqn.original\n", "iqn.original\t",
            "iqn.original/volume", "iqn.original_volume", "iqn.original;volume",
            "iqn.original\x00", "iqn.\u00e9", "iqn.\ud800",
        ):
            with self.subTest(iqn=iqn):
                with self.assertRaises(connect.ZonalAffinityError):
                    connect.decorate_target_iqn(iqn, "eastus-az3")

    def test_duplicate_normalized_regions_are_rejected(self):
        with self.assertRaisesRegex(connect.ZonalAffinityError, "duplicate region"):
            connect.map_logical_to_physical_zone(
                locations_list() + locations_list(" EASTUS "), "eastus", "2"
            )

    def test_distinct_logical_zones_do_not_require_distinct_physical_values(self):
        self.assertEqual(
            "eastus-az3",
            connect.map_logical_to_physical_zone(locations_list(mappings=[
                {"logicalZone": "1", "physicalZone": " EASTUS-AZ3 "},
                {"logicalZone": "2", "physicalZone": "eastus-az3"},
            ]), "eastus", "2"),
        )

    def test_invalid_unselected_mapping_entry_is_rejected(self):
        for value in (None, 3, "", "eastus_az1", "eastus-az1\nbad"):
            with self.subTest(value=value):
                locations = locations_list(mappings=[
                    {"logicalZone": "2", "physicalZone": "eastus-az3"},
                    {"logicalZone": "1", "physicalZone": value},
                ])
                with self.assertRaises(connect.ZonalAffinityError):
                    connect.map_logical_to_physical_zone(locations, "eastus", "2")

    def test_invalid_region_after_selected_region_is_not_ignored(self):
        for region in (None, {}, {"name": None}, {"name": " "}):
            with self.subTest(region=region):
                with self.assertRaisesRegex(connect.ZonalAffinityError, "malformed region"):
                    connect.map_logical_to_physical_zone(
                        locations_list() + [region], "eastus", "2"
                    )

    def test_mapping_normalizes_physical_zone_case(self):
        self.assertEqual(
            "eastus-az3",
            connect.map_logical_to_physical_zone(
                locations_list(mappings=[
                    {"logicalZone": "2", "physicalZone": " EASTUS-AZ3 "},
                ]), "eastus", "2",
            ),
        )

    def test_cli_cleanup_wait_is_bounded_and_closes_output(self):
        process = mock.Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("az", 30)
        with mock.patch.object(connect.subprocess, "Popen", return_value=process) as popen:
            with mock.patch.object(connect.os, "name", "nt"):
                with self.assertRaisesRegex(connect.ZonalAffinityError, "did not exit after termination"):
                    connect._run_az_command(["az", "account", "show"], "Azure CLI test")
        self.assertEqual([mock.call(timeout=30), mock.call(timeout=30)], process.wait.call_args_list)
        process.kill.assert_called_once_with()
        self.assertTrue(popen.call_args.kwargs["stdout"].closed)
        self.assertTrue(popen.call_args.kwargs["stderr"].closed)

    def test_cli_captures_large_output_without_pipe_readers(self):
        output = connect._run_az_command(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 262144); sys.stderr.write('y' * 262144)"],
            "Local output test",
        )
        self.assertEqual("x" * 262144, output)

    def test_cli_real_process_is_reaped_on_timeout(self):
        real_popen = subprocess.Popen
        processes = []

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        try:
            with mock.patch.object(connect.subprocess, "Popen", side_effect=launch):
                with mock.patch.object(connect, "AZ_CLI_TIMEOUT_SECONDS", 0.5):
                    with self.assertRaisesRegex(connect.ZonalAffinityError, "timed out"):
                        connect._run_az_command(
                            [sys.executable, "-c", "import time; time.sleep(60)"],
                            "Local timeout test",
                        )
            self.assertIsNotNone(processes[0].returncode)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    def test_cli_inherited_output_handles_do_not_delay_success_or_timeout(self):
        real_popen = subprocess.Popen
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                processes = []

                def launch(*args, **kwargs):
                    # Model a descendant that retains both handles and escapes
                    # the CLI group. Own its handle directly for portable cleanup.
                    holder = real_popen(
                        [sys.executable, "-c", "import time; time.sleep(60)"],
                        stdout=kwargs["stdout"], stderr=kwargs["stderr"],
                    )
                    processes.append(holder)
                    cli = real_popen(*args, **kwargs)
                    processes.append(cli)
                    return cli

                code = "import time; time.sleep(60)" if timeout else "print('done')"
                started = time.monotonic()
                try:
                    with mock.patch.object(connect.subprocess, "Popen", side_effect=launch):
                        with mock.patch.object(connect, "AZ_CLI_TIMEOUT_SECONDS", 1):
                            if timeout:
                                with self.assertRaisesRegex(connect.ZonalAffinityError, "timed out"):
                                    connect._run_az_command(
                                        [sys.executable, "-c", code], "Local held-output test"
                                    )
                            else:
                                output = connect._run_az_command(
                                    [sys.executable, "-c", code], "Local held-output test"
                                )
                                self.assertEqual("done\n", output.replace("\r\n", "\n"))
                    self.assertLess(time.monotonic() - started, 5)
                    self.assertIsNone(processes[0].poll())
                    self.assertIsNotNone(processes[1].returncode)
                finally:
                    for process in processes:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=5)

    def test_cli_interruption_and_termination_race_still_reap(self):
        for error, kill_error in (
            (KeyboardInterrupt(), None),
            (subprocess.TimeoutExpired("az", 30), ProcessLookupError()),
        ):
            with self.subTest(error=type(error).__name__):
                process = mock.Mock()
                process.wait.side_effect = [error, -9]
                process.kill.side_effect = kill_error
                expected_error = KeyboardInterrupt if isinstance(error, KeyboardInterrupt) else connect.ZonalAffinityError
                with mock.patch.object(connect.subprocess, "Popen", return_value=process):
                    with mock.patch.object(connect.os, "name", "nt"):
                        with self.assertRaises(expected_error):
                            connect._run_az_command(["az"], "Azure CLI test")
                process.kill.assert_called_once_with()
                self.assertEqual(2, process.wait.call_count)

    def test_enabled_mode_requires_python3_before_discovery(self):
        with mock.patch.object(connect.sys, "version_info", (2, 7)):
            with mock.patch.object(connect, "resolve_zonal_affinity_context") as resolve:
                with self.assertRaisesRegex(connect.ZonalAffinityError, "Python 3.5"):
                    connect.preflight_zonal_affinity(None, "rg", "san", "vg", ["first"])
        resolve.assert_not_called()

    def test_enabled_batch_snapshots_subscription_and_targets_before_native_calls(self):
        subscription_id = "00000000-0000-0000-0000-000000000001"
        compute = {"zone": "2", "subscriptionId": subscription_id, "location": "eastus"}
        targets = [
            {"targetIqn": "iqn.first", "targetPortalHostname": "first.example", "targetPortalPort": 3260},
            {"targetIqn": "iqn.second", "targetPortalHostname": "second.example", "targetPortalPort": 3261},
        ]
        responses = [
            subscription_id, "eastus", locations_payload(),
            json.dumps(targets[0]), json.dumps(targets[1]),
        ]
        events = []

        def popen(command, **kwargs):
            events.append(("az", command))
            return completed_process(responses.pop(0))(command, **kwargs)

        def check(*args, **kwargs):
            events.append(("check", args))
            return False

        def mutate(*args):
            events.append(("connect", args))

        with mock.patch.object(connect, "get_vm_compute_metadata", return_value=compute):
            with mock.patch.object(connect.subprocess, "Popen", side_effect=popen):
                with mock.patch.object(connect, "check_connection", side_effect=check):
                    with mock.patch.object(connect, "connect_volume", side_effect=mutate):
                        with mock.patch.object(connect, "get_iqns") as legacy_lookup:
                            connect.connect_volumes(None, "rg", "san", "vg", ["first", "second"], 4, True)

        legacy_lookup.assert_not_called()
        # Every volume is inventoried before the first one is changed.
        self.assertEqual(
            ["az", "az", "az", "az", "az", "check", "check", "connect", "connect"],
            [event[0] for event in events],
        )
        for command in (events[3][1], events[4][1]):
            self.assertEqual(subscription_id, command[command.index("--subscription") + 1])
        self.assertEqual(
            ("second", "iqn.second:az-eastus-az3", [("second.example", 4)], 3261, ()),
            events[-1][1],
        )

    def test_later_volume_failure_prevents_all_native_calls(self):
        failures = [
            connect.ZonalAffinityError("discovery failed"),
            ("IQN.second", "second.example", 3260),
            ("iqn.second\n", "second.example", 3260),
            ("i" * 223, "second.example", 3260),
        ]
        for failure in failures:
            with self.subTest(failure=failure):
                with mock.patch.object(
                    connect, "resolve_zonal_affinity_context", return_value=("sub-id", "eastus-az3")
                ):
                    with mock.patch.object(
                        connect, "get_mapped_volume_target",
                        side_effect=[("iqn.first", "first.example", 3260), failure],
                    ) as lookup:
                        with mock.patch.object(connect, "check_connection") as check:
                            with mock.patch.object(connect, "connect_volume") as mutate:
                                with self.assertRaises(connect.ZonalAffinityError):
                                    connect.connect_volumes(
                                        None, "rg", "san", "vg", ["first", "second"], 4, True
                                    )
                self.assertEqual(2, lookup.call_count)
                check.assert_not_called()
                mutate.assert_not_called()

    def test_mapping_failure_prevents_volume_and_native_discovery(self):
        with mock.patch.object(
            connect, "resolve_zonal_affinity_context",
            side_effect=connect.ZonalAffinityError("mapping failed"),
        ):
            with mock.patch.object(connect, "get_mapped_volume_target") as lookup:
                with mock.patch.object(connect, "check_connection") as check:
                    with mock.patch.object(connect, "connect_volume") as mutate:
                        with self.assertRaisesRegex(connect.ZonalAffinityError, "mapping failed"):
                            connect.connect_volumes(None, "rg", "san", "vg", ["first"], 4, True)
        lookup.assert_not_called()
        check.assert_not_called()
        mutate.assert_not_called()

    def test_enabled_volume_discovery_is_bounded_and_uses_argv_not_string_splitting(self):
        payload = json.dumps({
            "targetIqn": "iqn.original", "targetPortalHostname": "portal.example",
            "targetPortalPort": 3260,
        })
        with mock.patch.object(connect, "_run_az_command", return_value=payload) as run:
            self.assertEqual(
                ("iqn.original", "portal.example", 3260),
                connect.get_mapped_volume_target("sub-id", "my rg", "san", "vg", "volume"),
            )
        run.assert_called_once_with([
            "az", "elastic-san", "volume", "show",
            "-g", "my rg", "-e", "san", "-v", "vg", "-n", "volume",
            "--subscription", "sub-id", "--query", "storageTarget", "--output", "json",
        ], "Volume 'volume' target lookup")

    def test_malformed_storage_target_is_rejected(self):
        target = {
            "targetIqn": "iqn.original", "targetPortalHostname": "portal.example",
            "targetPortalPort": 3260,
        }
        payloads = ["invalid-json", "null", "[]", "{}"]
        for field, values in (
            ("targetIqn", (None, 42, "")),
            ("targetPortalHostname", (None, "", "portal.example --option", "portal.example\n", "-portal")),
            ("targetPortalPort", (None, "3260", True, 0, 65536)),
        ):
            for value in values:
                invalid = dict(target)
                invalid[field] = value
                payloads.append(json.dumps(invalid))
        for payload in payloads:
            with self.subTest(payload=payload):
                with mock.patch.object(connect, "_run_az_command", return_value=payload):
                    with self.assertRaises(connect.ZonalAffinityError):
                        connect.get_mapped_volume_target("sub-id", "rg", "san", "vg", "volume")

    def test_opt_out_discovers_every_volume_before_inventory_and_connect(self):
        events = []

        def discover(*args):
            events.append(("discover", args[-1]))
            return "IQN." + args[-1], "portal.example", 3260

        def check(iqn, *args, **kwargs):
            events.append(("check", iqn))
            if iqn == "IQN.first":
                return connect.ExistingState([], [("1", iqn, "portal.example:3260")])
            return connect.ExistingState([], [])

        def mutate(name, *args):
            events.append(("connect", name))

        with mock.patch.object(connect, "preflight_zonal_affinity") as preflight:
            with mock.patch.object(connect, "get_iqns", side_effect=discover):
                with mock.patch.object(connect, "check_connection", side_effect=check):
                    with mock.patch.object(connect, "connect_volume", side_effect=mutate):
                        with mock.patch.object(connect.sys, "stdout", new_callable=io.StringIO) as output:
                            connect.connect_volumes(None, "rg", "san", "vg", ["first", "second"], 4)
        preflight.assert_not_called()
        self.assertEqual(
            [("discover", "first"), ("discover", "second"),
             ("check", "IQN.first"), ("check", "IQN.second"), ("connect", "second")],
            events,
        )
        self.assertEqual(
            "first [IQN.first]: Skipped: already connected (1 live / 0 persistent); these sessions are "
            "not persistent and will not return after a reboot\n",
            output.getvalue(),
        )

    def test_entrypoint_preserves_configurable_count_and_opt_out_defaults(self):
        for enabled in (False, True):
            cases = ((None, 32), ("4", 4), ("64", 32))
            for count, expected in cases:
                with self.subTest(enabled=enabled, count=count):
                    events = []
                    args = ["--subscription", "sub", "-g", "rg", "-e", "san", "-v", "vg", "-n", "volume"]
                    if enabled:
                        args.append("--enable-zonal-affinity")
                    if count is not None:
                        args.extend(["-s", count])
                    with mock.patch.object(connect, "check_privileges", side_effect=lambda: events.append("privilege")):
                        with mock.patch.object(connect, "detect_distro", side_effect=lambda: events.append("distro")):
                            with mock.patch.object(connect, "connect_volumes") as run:
                                with mock.patch.object(connect, "finish_connections") as finish:
                                    connect.main(args)
                    self.assertEqual(["privilege", "distro"], events)
                    run.assert_called_once_with(
                        "sub", "rg", "san", "vg", ["volume"], expected, enabled, False,
                        recommended_settings=connect.RECOMMENDED_NODE_SETTINGS, prepare_host=mock.ANY,
                    )
                    finish.assert_called_once_with(run.return_value, expected, True)

    def test_enabled_entrypoint_rejects_later_iqn_before_native_inventory_or_connect(self):
        args = [
            "-g", "rg", "-e", "san", "-v", "vg", "-n", "first", "second",
            "--enable-zonal-affinity",
        ]
        with mock.patch.object(connect, "check_privileges") as privileges:
            with mock.patch.object(connect, "detect_distro", return_value="debian") as distro:
                with mock.patch.object(
                    connect, "resolve_zonal_affinity_context",
                    return_value=("sub-id", "eastus-az3"),
                ):
                    with mock.patch.object(connect, "get_mapped_volume_target", side_effect=[
                        ("iqn.first", "first.example", 3260),
                        ("IQN.second", "second.example", 3260),
                    ]) as lookup:
                        with mock.patch.object(connect, "prepare_iscsi_host") as prepare:
                            with mock.patch.object(connect, "check_connection") as inventory:
                                with mock.patch.object(connect, "connect_volume") as mutate:
                                    with self.assertRaises(connect.ZonalAffinityError):
                                        connect.main(args)
        # Privilege and platform checks come first; a failed read-only lookup
        # stops before any prerequisite is installed or any volume changes.
        privileges.assert_called_once_with()
        distro.assert_called_once_with()
        self.assertEqual(2, lookup.call_count)
        prepare.assert_not_called()
        inventory.assert_not_called()
        mutate.assert_not_called()


class ConnectorRegressionTests(unittest.TestCase):
    def test_digest_argv_and_full_persistent_count(self):
        for count in (1, 4, 32):
            with self.subTest(count=count):
                commands = []

                def launch(command, **kwargs):
                    commands.append(command)
                    process = mock.Mock(returncode=0)
                    output = (b"tcp: [17] 192.0.2.50:3260,7 iqn.original\n"
                              if any("--login" in c for c in commands) else b"")
                    process.communicate.return_value = (output, b"")
                    return process

                with mock.patch.object(connect.subprocess, "Popen", side_effect=launch):
                    with mock.patch("builtins.print"):
                        connect.connect_volume("volume", "iqn.original", [("portal.example", count)], 3260)
                clones = [c for c in commands if c[2:4] == ["-m", "session"] and "-r" in c]
                self.assertEqual(count - 1, len(clones))
                for digest in ("HeaderDigest", "DataDigest"):
                    self.assertIn([
                        "sudo", "iscsiadm", "-m", "node", "--targetname", "iqn.original",
                        "--portal", "portal.example:3260", "--op", "update",
                        "-n", "node.conn[0].iscsi." + digest, "-v", "CRC32C",
                    ], commands)
                persisted = [c for c in commands if "node.session.nr_sessions" in c]
                self.assertEqual("1", persisted[0][-1])
                self.assertEqual(str(count), persisted[-1][-1])


if __name__ == "__main__":
    unittest.main()
