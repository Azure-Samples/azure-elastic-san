"""Disconnect Azure Elastic SAN volumes on FreeBSD 13+.

For each selected volume this removes every iSCSI session whose target is the
volume's IQN or that IQN with one ':az-<zone>' suffix, on any portal (FQDN or
VIP address), then removes the matching entries from the managed block in
/etc/iscsi.conf so they are not re-added at boot. Other targets, stanzas outside
the managed block and host settings (iscsid, iscsictl_enable, kernel module) are
left alone.

The managed-block grammar, config lock and atomic write are shared with
connect_for_documentation.py, which must be in the same directory. See README.md
("Disconnect").
"""

import argparse
import os
import shutil
import sys

import connect_for_documentation as connect

CONFIRMATION_PROMPT = (
    "Warning: this disconnects every iSCSI session of the selected volumes and removes their "
    "managed entries from {} across all portals, including zonal targets. Quiesce I/O and "
    "unmount the volumes first. Continue? [Y/Yes/N/No]: "
)


def confirm(config_path, read_input=input):
    while True:
        try:
            answer = read_input(CONFIRMATION_PROMPT.format(config_path)).strip().lower()
        except EOFError:
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def check_disconnect_prerequisites(config_path):
    if os.geteuid() != 0:
        raise connect.ElasticSanConnectError(
            "Disconnecting removes iSCSI sessions and changes {}, and must be run as root.".format(
                config_path
            )
        )
    system_name = os.uname().sysname
    if system_name != "FreeBSD":
        raise connect.ElasticSanConnectError(
            "This script supports FreeBSD only, but this host reports '{}'. Use the Linux or "
            "Windows Elastic SAN disconnect script on other platforms.".format(system_name)
        )
    if shutil.which("iscsictl") is None:
        raise connect.ElasticSanConnectError("Required FreeBSD tool 'iscsictl' was not found on PATH.")


def replace_managed_entries(content_bytes, entries):
    """Return *content_bytes* whose managed block holds exactly *entries*. An empty
    block is removed with its markers; everything outside it is kept byte-for-byte."""
    prefix, block, suffix = connect._split_managed_block(content_bytes)
    if block is None:
        return content_bytes
    return prefix + (connect.render_managed_block(entries) if entries else b"") + suffix


def disconnect_volumes(volumes, config_path):
    """Disconnect *volumes*, a list of (volume name, volume IQN), under the config lock.

    Every volume's sessions are removed before its entries, so a failure never leaves
    a live session without the entry that explains it. Native failures stop the run.
    """
    _, lock_fd = connect.acquire_config_transaction_lock(config_path)
    try:
        content, existed, original_stat = connect.read_config_file(config_path)
        # Parse strictly before any change: a malformed block stops the run untouched.
        entries = connect.read_managed_entries(content)
        # 'iscsictl -L' can load the kernel module; without /dev/iscsi there are no sessions.
        sessions = (
            connect.list_iscsi_sessions()
            if os.path.exists(connect.ISCSI_DEVICE_NODE)
            else []
        )
        backed_up = False
        for volume_name, volume_iqn in volumes:
            volume_sessions = connect.sessions_for_volume(sessions, volume_iqn)
            target_names = sorted(set(session["Target name"] for session in volume_sessions))
            volume_entries = [
                entry for entry in entries if connect.belongs_to_volume(entry.target_name, volume_iqn)
            ]
            if not target_names and not volume_entries:
                print("{} [{}]: Skipped as this volume is not connected".format(volume_name, volume_iqn))
                continue
            for target_name in target_names:
                # Removes every session configured with this exact target name, on any portal.
                connect._run_command(
                    ["iscsictl", "-R", "-t", target_name],
                    "iscsictl remove sessions for '{}'".format(target_name),
                    connect.ISCSICTL_COMMAND_TIMEOUT_SECONDS,
                )
            sessions = [session for session in sessions if session not in volume_sessions]
            if volume_entries:
                if not backed_up:
                    connect.backup_config(config_path, content, existed, "disconnect")
                    backed_up = True
                entries = [entry for entry in entries if entry not in volume_entries]
                content = replace_managed_entries(content, entries)
                connect.write_config_atomically(config_path, content, original_stat)
            print(
                "{} [{}]: Removed {} session(s) and {} managed {}".format(
                    volume_name,
                    volume_iqn,
                    len(volume_sessions),
                    len(volume_entries),
                    "entry" if len(volume_entries) == 1 else "entries",
                )
            )
    finally:
        connect.release_config_transaction_lock(lock_fd)


def create_argument_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Disconnect Azure Elastic SAN volumes on FreeBSD 13+: remove their iSCSI sessions "
            "and managed " + connect.DEFAULT_ISCSI_CONF_PATH + " entries on every portal."
        )
    )
    parser.add_argument(
        "--elastic-san-subscription",
        "--subscription",
        dest="elastic_san_subscription",
        help=(
            "Elastic SAN resource subscription name or ID. --subscription is retained as a "
            "compatibility alias."
        ),
    )
    parser.add_argument("-g", "--resource-group", dest="resource_group")
    parser.add_argument("-e", "--elastic-san", dest="elastic_san")
    parser.add_argument("-v", "--volume-group", dest="volume_group")
    parser.add_argument("-n", "--volumes", dest="volumes", nargs="+")
    parser.add_argument(
        "--config-path",
        dest="config_path",
        default=connect.DEFAULT_ISCSI_CONF_PATH,
        help=argparse.SUPPRESS,  # override for tests only; not part of the public CLI contract
    )
    return parser


def main(argv=None, read_input=input):
    """Return the process exit code: 0 when done, 1 when the operator cancels."""
    args = create_argument_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if None in (args.resource_group, args.elastic_san, args.volume_group, args.volumes):
        raise connect.ElasticSanConnectError(
            "Need to provide resource_group_name, elastic_san_name, volume_group_name, "
            "volume_names to disconnect the Elastic SAN volume(s)"
        )
    check_disconnect_prerequisites(args.config_path)
    connect.check_az_cli_available()

    # Every lookup finishes before the prompt, so nothing changes if one fails.
    volumes = []
    selected = {}
    for volume_name in args.volumes:
        target_iqn = connect.get_volume_storage_target(
            args.elastic_san_subscription,
            args.resource_group,
            args.elastic_san,
            args.volume_group,
            volume_name,
        )[0]
        connect.validate_config_scalar(target_iqn, "Target IQN for '{}'".format(volume_name))
        previous = selected.get(target_iqn.lower())
        if previous is not None:
            print(
                "{} [{}]: Ignored duplicate selection; the same target is already selected as "
                "'{}'".format(volume_name, target_iqn, previous)
            )
            continue
        selected[target_iqn.lower()] = volume_name
        volumes.append((volume_name, target_iqn))

    if not confirm(args.config_path, read_input):
        print("Cancelled; nothing was changed.")
        return 1
    disconnect_volumes(volumes, args.config_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except connect.ElasticSanConnectError as error:
        sys.stderr.write("ERROR: {}\n".format(error))
        sys.exit(1)
