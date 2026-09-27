"""Parsing ethtool output and building the NIC statistics check's SSH command."""

from __future__ import annotations

from unittest.mock import patch

from app.nic_stats_client import CHECK_NAME, fetch_stats, parse_ethtool_stats

REAL_ETHTOOL_OUTPUT = """\
===IFACE:eth4===
NIC statistics:
     rx_packets: 918273645
     tx_packets: 918273600
     rx_errors: 0
     tx_errors: 0
     rx_dropped: 0
     tx_dropped: 0
     rx_crc_errors: 0
     fdir_miss: 714125
     fcoe_bad_fccrc: 0
===IFACE:eth5===
NIC statistics:
     rx_packets: 12345
     rx_errors: 7
     rx_crc_errors: 3
"""


def test_parse_splits_marked_blocks_by_interface() -> None:
    parsed = parse_ethtool_stats(REAL_ETHTOOL_OUTPUT)
    assert set(parsed) == {"eth4", "eth5"}
    assert parsed["eth4"]["rx_errors"] == 0
    assert parsed["eth4"]["fdir_miss"] == 714125
    assert parsed["eth5"]["rx_errors"] == 7
    assert parsed["eth5"]["rx_crc_errors"] == 3


def test_parse_ignores_non_counter_lines() -> None:
    parsed = parse_ethtool_stats(REAL_ETHTOOL_OUTPUT)
    assert "NIC statistics" not in parsed["eth4"]


def test_parse_with_no_marker_produces_no_interfaces() -> None:
    """Output from a host whose dispatcher script was not set up as documented."""
    assert parse_ethtool_stats("NIC statistics:\n  rx_errors: 0\n") == {}


def test_parse_empty_output() -> None:
    assert parse_ethtool_stats("") == {}


def test_fetch_stats_sends_the_check_name_and_no_arguments() -> None:
    """A thin wrapper over app.ssh_client.run_check — this only proves the
    NIC-stats-specific check name is wired through, not the SSH plumbing
    itself (covered by tests/test_ssh_client.py). No interface list is sent:
    the host discovers its own interfaces (host-scripts/xcp-pulse-diag.sh)."""
    with patch("app.nic_stats_client.run_check", return_value=("output", False)) as mock_run:
        fetch_stats(
            host="10.0.0.1",
            port=22,
            private_key="key",
            known_host_key=None,
            on_trust_new_host_key=lambda *a: None,
        )

    assert mock_run.call_args.kwargs["check"] == CHECK_NAME
    assert "args" not in mock_run.call_args.kwargs
