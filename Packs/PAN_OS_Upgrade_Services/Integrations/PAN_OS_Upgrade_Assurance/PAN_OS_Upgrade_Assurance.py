import demistomock as demisto  # noqa: F401
from CommonServerPython import *  # noqa: F401

from typing import Optional, List
from urllib.parse import urlparse

from panos_upgrade_assurance.firewall_proxy import FirewallProxy
from panos_upgrade_assurance.check_firewall import CheckFirewall
from panos_upgrade_assurance.snapshot_compare import SnapshotCompare

from panos.panorama import Panorama
from panos.errors import PanDeviceXapiError

SETTINGS = {
    "skip_force_locale": True
}


def get_file_path(input_entry_id):
    res = demisto.getFilePath(input_entry_id)
    if not res:
        return_error("Entry {} not found".format(input_entry_id))
    file_path = res['path']
    return file_path


def read_file_by_id(input_entry_id):
    fp = get_file_path(input_entry_id)
    with open(fp) as f:
        return json.load(f)


def get_firewall_object(panorama: Panorama, serial_number):
    """Create a FirewallProxy object and attach it to Panorama, so we can access it."""
    firewall = FirewallProxy(serial=serial_number)
    panorama.add(firewall._fw)
    return firewall


def get_panorama(ip, user, password):
    """Create the Panorama Object

    NOTE: NOT IN USE.
    """
    return Panorama(
        api_username=user,
        api_password=password,
        hostname=ip
    )


def parse_session(session_str: str):
    source, destination, port = session_str.split("/")
    return {
        "source": source,
        "destination": destination,
        "dest_port": port
    }


def run_snapshot(
        firewall: FirewallProxy, snapshot_list: Optional[List] = None):
    """Runs a snapshot and saves it as a JSON file in the XSOAR system."""

    if not snapshot_list:
        # this is the default list while other snapshot types can be passed in the argument
        snapshot_list = [
            'nics',
            'routes',
            'license',
            'arp_table',
            'content_version',
            'session_stats',
            'ip_sec_tunnels',
        ]

    checks = CheckFirewall(firewall, **SETTINGS)
    snapshot = checks.run_snapshots(snapshot_list)

    return snapshot


def run_readiness_checks(
        firewall: FirewallProxy,
        check_list: Optional[List] = None,
        min_content_version: Optional[str] = None,
        candidate_version: Optional[str] = None,
        dp_mp_clock_diff: Optional[int] = None,
        ipsec_tunnel_status: Optional[str] = None,
        check_session_exists: Optional[str] = None,
        arp_entry_exists: Optional[str] = None
):
    """
    Run all the readiness checks and return an xsoar-compatible result.

    :arg firewall: Firewall object
    :arg check_list: List of basic checks. Must match with Upgrade Assurance check types.
    :arg min_content_version: The minimum content version to check for, otherwise latest content version is
        checked if "content_version" is provided in `check_list`.
    :arg candidate_version: The candidate version to runchecks against. Enables "free_disk_space" check
    :arg dp_mp_clock_diff: The drift allowed between DP clock and MP clock. Enables "planes_clock_sync" check.
    :arg ipsec_tunnel_status: Check a specific IPsec - by tunnel name. Tunnel must be up for this check to pass.
    :arg check_session_exists: Check for the presence of a specific connection.
        Session check format is <source>/destination/destination-port
        example: 10.10.10.10/8.8.8.8/443
    :arg arp_entry_exists: Check for the prescence of a specific ARP entry.
        example: 10.0.0.6

    """

    if not check_list:
        # Setup the defaults
        check_list = [
            'panorama',
            'ntp_sync',
            'candidate_config',
            'active_support',
        ]
        # only include HA check if HA is enabled
        if firewall.get_ha_configuration().get('enabled') == 'yes':
            check_list.append('ha')
    custom_checks = []

    # Add the custom checks

    if 'content_version' in check_list:
        if min_content_version:
            custom_checks.append({'content_version': {'version': min_content_version}})
            check_list.remove('content_version')
        # else it will check for latest content version

    if candidate_version:
        custom_checks.append({
            'free_disk_space': {
                'image_version': candidate_version
            }
        })
    else:
        check_list.append('free_disk_space')

    if 'planes_clock_sync' in check_list:
        if isinstance(dp_mp_clock_diff, int) and dp_mp_clock_diff >= 0:
            custom_checks.append({
                'planes_clock_sync': {
                    'diff_threshold': dp_mp_clock_diff
                }
            })
        check_list.remove('planes_clock_sync')

    if 'ip_sec_tunnel_status' in check_list:
        if ipsec_tunnel_status:
            custom_checks.append({
                'ip_sec_tunnel_status': {
                    'tunnel_name': ipsec_tunnel_status
                }
            })
        check_list.remove('ip_sec_tunnel_status')

    if 'session_exist' in check_list:
        if check_session_exists:
            try:
                check_value = parse_session(check_session_exists)
            except ValueError:
                raise ValueError(
                    f"{check_session_exists} is not a valid session string. Must be 'source/destination/port'."
                )
            custom_checks.append({
                'session_exist': check_value
            })
        check_list.remove('session_exist')

    if 'arp_entry_exists' in check_list:
        if arp_entry_exists:
            custom_checks.append({
                'arp_entry_exist': {
                    'ip': arp_entry_exists
                }
            })
        check_list.remove('arp_entry_exists')

    check_config = check_list + custom_checks

    checks = CheckFirewall(firewall, **SETTINGS)
    results = checks.run_readiness_checks(check_config)

    return results


def compare_snapshots(left_snapshot, right_snapshot,
                      snapshot_list: Optional[List] = None,
                      session_stats_threshold : Optional[int] = None):
    """
    Compare snapshot files taken by the `pan-os-assurance-run-snapshot` command.

    :arg left_snapshot: Left ("first) snapshot to compare against.
    :arg right_snapshot: Right ("second") snapshot to compare against "left" snapshot.
    :arg snapshot_list: List of snapshot types to compare. If not provided, a default set of snapshots will be compared. Must match with Upgrade Assurance snapshot types.
    :arg session_stats_threshold: Percentage of change in session stats that is allowed to pass the comparison.

    """
    snapshot_compare = SnapshotCompare(left_snapshot, right_snapshot)

    if not snapshot_list:
        # Setup the defaults
        snapshot_list = [
            'nics',
            'routes',
            'license',
            'arp_table',
            'content_version',
            'session_stats',
            'ip_sec_tunnels',
        ]

    snapshot_comparisons = []

    if 'nics' in snapshot_list:
        snapshot_comparisons.append({
            'nics': {
                'count_change_threshold': 10
            }
        })

    if 'routes' in snapshot_list:
        snapshot_comparisons.append({
            'routes': {
                'properties': ['!flags', '!age'],
                'count_change_threshold': 10
            }
        })

    if 'license' in snapshot_list:
        snapshot_comparisons.append({
            'license': {
                'properties': ['!serial', '!issued', '!authcode']
            }
        })

    if 'arp_table' in snapshot_list:
        snapshot_comparisons.append({
            'arp_table': {
                'properties': ['!ttl'],
                'count_change_threshold': 10
            }
        })

    if 'content_version' in snapshot_list:
        snapshot_comparisons.append('content_version')

    if 'session_stats' in snapshot_list:
        # if session_stats_threshold not None and bigger than 0(zero)
        if isinstance(session_stats_threshold, int) and session_stats_threshold > 0:
            snapshot_comparisons.append({
                'session_stats': {
                    'thresholds': [
                        {'num-max': session_stats_threshold},
                        {'num-tcp': session_stats_threshold},
                    ]
                }
            })
        else:                   # default session_stats thresholds
            snapshot_comparisons.append({
                'session_stats': {
                    'thresholds': [
                        {'num-max': 10},
                        {'num-tcp': 10},
                    ]
                }
            })

    if 'ip_sec_tunnels' in snapshot_list:
        snapshot_comparisons.append({
            'ip_sec_tunnels': {
                'properties': ['state']
            }
        })

    if 'bgp_peers' in snapshot_list:
        snapshot_comparisons.append({
            'bgp_peers': {
                'properties': ['status']
            }
        })

    return snapshot_compare.compare_snapshots(snapshot_comparisons)


def convert_readiness_results_to_table(results: dict):
    table = []
    for key, result in results.items():
        table.append({
            "Test": key,
            **result
        })

    return table


def convert_snapshot_result_to_table(results: dict):
    table = []
    for key, test_result in results.items():
        if type(test_result) is dict:
            table.append({
                "test": f"{key}",
                "passed": test_result.get("passed")
            })

    return table


def command_run_readiness_checks(panorama: Panorama):
    """Parse readiness check command to run corresponding checks

    Implemented checks:

    - panorama
    - ha
    - ntp_sync
    - candidate_config
    - expired_licenses
    - active_support
    - content_version
    - session_exist
    - arp (arp_entry_exist)
    - ipsec_tunnel (ip_sec_tunnel_status)
    - free_disk_space (hardcoded check)
    - dp_mp_clock_diff (planes_clock_sync)

    """
    args = demisto.args()
    firewall = get_firewall_object(panorama, args.get('firewall_serial'))
    del args['firewall_serial']

    # this will set it to [] if emptry string or not provided - meaning check all
    check_list = argToList(args.get('check_list'))
    # remove check_list from args to avoid duplicate arg pass to run_readiness_checks
    if 'check_list' in args:
        del args['check_list']

    if 'dp_mp_clock_diff' in check_list:
        check_list.remove('dp_mp_clock_diff')
        check_list.append('planes_clock_sync')  # to match with upgrade assurance check type

    # this will set it to None if emptry string or not provided
    dp_mp_clock_diff = arg_to_number(args.get('dp_mp_clock_diff'), required=False)
    if 'dp_mp_clock_diff' in args:
        del args['dp_mp_clock_diff']

    if 'ipsec_tunnel' in check_list:
        check_list.remove('ipsec_tunnel')
        check_list.append('ip_sec_tunnel_status')

    if 'arp' in check_list:
        check_list.remove('arp')
        check_list.append('arp_entry_exists')

    if (arp_entry := args.get('arp_entry_exists')) and not is_ip_valid(arp_entry):
        raise ValueError(
            f"{arp_entry} is not a valid IPv4 address."
        )

    results = run_readiness_checks(firewall, check_list,
                                   dp_mp_clock_diff=dp_mp_clock_diff, **args)

    return CommandResults(
        outputs={
            'ReadinessCheckResults': convert_readiness_results_to_table(results),
            'Firewall': firewall.serial
        },
        readable_output=tableToMarkdown('Readiness Check Results',
                                        convert_readiness_results_to_table(results),
                                        headers=['Test', 'state', 'reason']),
        outputs_prefix='FirewallAssurance'
    )


def command_run_snapshot(panorama: Panorama):
    """Runs a single snapshot and returns it as a file."""
    args = demisto.args()
    firewall = get_firewall_object(panorama, args.get('firewall_serial'))
    snapshot_name = args.get('snapshot_name', 'fw_snapshot')
    del args['firewall_serial']
    if args.get('snapshot_name'):
        del args['snapshot_name']

    # this will set it to [] if emptry string or not provided - meaning all snapshots
    snapshot_list = argToList(args.get('check_list'))
    if 'check_list' in args:
        del args['check_list']

    snapshot = run_snapshot(firewall, snapshot_list, **args)

    fr = fileResult(
        snapshot_name, json.dumps(snapshot, indent=4)
    )
    return fr


def command_compare_snapshots():
    """Compare two snapshot files, accepting th left and right snapshots as arguments."""
    args = demisto.args()
    left_snapshot = read_file_by_id(args.get('left_snapshot_id'))
    right_snapshot = read_file_by_id(args.get('right_snapshot_id'))

    # this will set it to [] if emptry string or not provided - meaning all snapshots
    snapshot_list = argToList(args.get('check_list'))
    if 'check_list' in args:
        del args['check_list']

    session_stats_threshold = arg_to_number(args.get('session_stats_threshold'), required=False)

    result = compare_snapshots(left_snapshot, right_snapshot,
                               snapshot_list=snapshot_list,
                               session_stats_threshold=session_stats_threshold)
    return CommandResults(
        outputs={
            'SnapshotComparisonResult': convert_snapshot_result_to_table(result),
            'SnapshotComparisonRawResult': result
        },
        readable_output=tableToMarkdown(
            'Snapshot Comparison Results', convert_snapshot_result_to_table(result), headers=['test', 'passed']),
        outputs_prefix='FirewallAssurance'
    )


def main():
    # copied from device mgmt...
    params = demisto.params()
    api_key = str(params.get('key')) or str((params.get('credentials') or {}).get('password', ''))
    parsed_url = urlparse(params.get("url"))
    port = params.get("port", "443")
    hostname = parsed_url.hostname

    handle_proxy()
    panorama = Panorama.create_from_device(
        hostname=hostname,
        api_key=api_key,
        port=port
    )

    command = demisto.command()
    try:
        if command == "pan-os-assurance-run-readiness-checks":
            return_results(command_run_readiness_checks(panorama))
        elif command == "pan-os-assurance-run-snapshot":
            return_results(command_run_snapshot(panorama))
        elif command == "pan-os-assurance-compare-snapshots":
            return_results(command_compare_snapshots())
        elif command == "test-module":
            return_results("ok")
        else:
            return_error(f"{command} not implemented.")
    except PanDeviceXapiError as e:
        return_error(f"{e}")


if __name__ == "__builtin__" or __name__ == "builtins":
    main()
