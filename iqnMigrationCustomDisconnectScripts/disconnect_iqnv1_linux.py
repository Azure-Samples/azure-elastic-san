#!/usr/bin/env python3

import argparse
import re
import subprocess
import sys


V1_PREFIX = "net.windows.core.blob"

def validate_iqns(iqn_string):
    items = [x.strip() for x in iqn_string.split(",") if x.strip()]
    for iqn in items:
        if V1_PREFIX not in iqn:
            print(f"Warning: IQN {iqn} is not IQN v1. Please provide a valid IQN v1 string. Exiting.")
            return None
    return items

def get_iscsi_sessions():
    try:
        output = subprocess.check_output(['iscsiadm', '-m', 'session'], text=True)
        sessions = {}
        for line in output.strip().split('\n'):
            if line:
                # Improved regex: allow for protocol at start, flexible whitespace, and trailing fields
                match = re.search(r'^\s*\w+:\s*\[(\d+)\]\s+([^\s]+:\d+),-?\d+\s+(\S+)', line)
                if match:
                    session_id = match.group(1)
                    target = match.group(3)
                    if target not in sessions:
                        sessions[target] = [session_id]
                    else:
                        sessions[target].append(session_id)
                else:
                    print(f"Failed to parse iSCSI session line: {line}")
        return sessions
    except subprocess.CalledProcessError:
        print("Failed to get iSCSI sessions.")
        return {}


def ask_yes_no(prompt):
    while True:
        resp = input(prompt).strip().upper()
        if resp in ("Y", "N"):
            return resp == "Y"
        print("Invalid choice. Please type 'Y' or 'N'.")


def logout_session(session_id):
    try:
        subprocess.check_call(["iscsiadm", "-m", "session", "-r", session_id, "-u"])
        return True
    except subprocess.CalledProcessError:
        return False

def main():
    parser = argparse.ArgumentParser(
        description="Disconnect v1 Elastic SAN iSCSI sessions on Linux."
    )
    # allow the user to either pass a single quoted comma list or multiple
    # IQNs separated by spaces; both forms are supported.
    parser.add_argument(
        "IQN",
        nargs="+",
        help="One or more IQN v1 strings either space-separated or comma-separated",
    )
    args = parser.parse_args()

    # combine into a single comma-separated string before validation
    iqn_input = ",".join(args.IQN)
    iqns = validate_iqns(iqn_input)
    if iqns is None:
        sys.exit(1)

    all_sessions = get_iscsi_sessions()

    if not all_sessions:
            print("No active iSCSI sessions found.")
            return

    for iqn in all_sessions.keys():
        sessions = all_sessions[iqn]
        count = len(sessions)
        if count == 0:
            print(f"{iqn}, No active sessions found on this host.\n")
        else:
            print(f"{iqn}, Session Count: {count}\n")

    confirm = ask_yes_no(
        "Before running this script, please navigate to your volume's connect script on ms.portal.azure.com and\n"
        "confirm that the IQN listed in the call to volume_data.append() contains the substring 'net.azure.storage.blob' and not\n"
        "'net.windows.core.blob'. Have you verified that all of the volumes you will be disconnecting using this script have an\n"
        "IQN v2 in your portal connect script? (Y/N): "
    )
    if not confirm:
        print("Exiting without disconnecting sessions.")
        return

    confirm = ask_yes_no("Would you like to proceed in disconnecting all sessions for these volumes? (Y/N): ")
    if not confirm:
        print("Exiting without disconnecting sessions.")
        return

    for iqn in iqns:
        try:
            subprocess.check_call([
                'iscsiadm', '-m', 'node',
                '-T', iqn,
                '-o', 'update',
                '-n', 'node.startup',
                '-v', 'manual'
            ])

            subprocess.check_call([
                'iscsiadm', '-m', 'node',
                '-T', iqn,
                '--logout'
            ])
            # print(f"Disconnected target: {iqn} ")
        except subprocess.CalledProcessError:
            print(f"Failed to disconnect target: {iqn}")

    print("\nPlease reboot your host and run 'iscsiadm -m session' to verify no sessions are active.")


if __name__ == "__main__":
    main()
