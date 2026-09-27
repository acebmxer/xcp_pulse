"""The NIC statistics job's per-interface report: what it stores, and what a
clean run actually shows.

Before this, a clean run threw away every interface it read once it had
confirmed nothing was wrong, so "no findings" and "nothing was checked" were
indistinguishable on the page. These tests are about that: the interface
list, its link state (from Xen Orchestra's own PIF data), and its error
counters (read over SSH) have to survive the round trip even when there is
nothing to flag — and a host's virtual interfaces, which are not physical
NICs Xen Orchestra knows about, have to be filtered out rather than
generating a nonsense "check the cable" finding.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.artifacts import list_for_job, store_json
from app.db import init_db
from app.findings import Report, SourceResult
from app.job_nic_stats import (
    KIND,
    NIC_STATS_ARTIFACT,
    _interface_records,
    _network_records,
    _physical_only,
    errors_summary,
    link_summary,
    nbd_summary,
    report_from_job,
    to_markdown,
    to_payload,
)
from app.jobs import enqueue
from app.xo_client import Network, PifStatus

HOST_STATS = {
    "xcp-ng-host1": {
        "eth0": {"rx_errors": 0, "tx_errors": 0, "fdir_miss": 714125},
        "eth1": {"rx_errors": 0},
        "vif1.0": {"rx_errors": 99},
    },
    "xcp-ng-host2": {
        "eth0": {"rx_errors": 0, "rx_crc_errors": 3},
    },
}

PIF_BY_HOST = {
    "xcp-ng-host1": {
        "eth0": PifStatus(attached=True, carrier=True, speed=1000),
        "eth1": PifStatus(attached=False, carrier=False, speed=0),
    },
    "xcp-ng-host2": {
        "eth0": PifStatus(attached=True, carrier=False, speed=0),
    },
}

NETWORK_BY_ID = {
    "net-nbd": Network(
        id="net-nbd",
        name="Pool-wide network 0",
        pool_id="pool-1",
        mtu=1500,
        nbd=True,
        locked=False,
        automatic=False,
        pif_count=3,
    ),
    "net-no-nbd": Network(
        id="net-no-nbd",
        name="Work",
        pool_id="pool-1",
        mtu=1500,
        nbd=False,
        locked=False,
        automatic=False,
        vlan=79,
        pif_count=3,
    ),
}

PIF_BY_HOST_WITH_NETWORK = {
    "xcp-ng-host1": {
        "eth0": PifStatus(attached=True, carrier=True, speed=1000, network_id="net-nbd"),
        "eth1": PifStatus(attached=False, carrier=False, speed=0, network_id="net-no-nbd"),
    },
    "xcp-ng-host2": {
        "eth0": PifStatus(attached=True, carrier=False, speed=0, network_id="net-nbd"),
    },
}


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return init_db(tmp_path / "test.db")


def _physical_host_stats() -> dict[str, dict[str, dict[str, int]]]:
    return _physical_only(HOST_STATS, PIF_BY_HOST)


def _report_with_interfaces() -> Report:
    report = Report(sources=[SourceResult("nic_stats", read=True, examined=3)], window_days=0)
    report.interfaces = _interface_records(_physical_host_stats(), PIF_BY_HOST)
    return report


def test_physical_only_drops_a_vif_not_known_to_xen_orchestra() -> None:
    """vif1.0 passes the host's own /sys/class/net/*/device test but is not
    a physical NIC XO's PIF list knows about — it must not survive."""
    filtered = _physical_host_stats()

    assert "vif1.0" not in filtered["xcp-ng-host1"]
    assert set(filtered["xcp-ng-host1"]) == {"eth0", "eth1"}


def test_physical_only_keeps_everything_for_a_host_with_no_pif_lookup() -> None:
    """Xen Orchestra's PIF list could not be read (or did not mention this
    host) — that is not evidence anything read is a virtual interface, so
    nothing is dropped."""
    filtered = _physical_only(HOST_STATS, {"xcp-ng-host1": None, "xcp-ng-host2": None})

    assert filtered == HOST_STATS


def test_interface_records_cover_every_physical_interface() -> None:
    records = _interface_records(_physical_host_stats(), PIF_BY_HOST)

    keys = {(r["host"], r["interface"]) for r in records}
    assert keys == {
        ("xcp-ng-host1", "eth0"),
        ("xcp-ng-host1", "eth1"),
        ("xcp-ng-host2", "eth0"),
    }


def test_interface_records_only_carry_known_error_counters() -> None:
    """fdir_miss is a real driver counter but not one findings treats as an
    error — it should not be reported as one on a clean interface either."""
    records = _interface_records(_physical_host_stats(), PIF_BY_HOST)
    eth0 = next(r for r in records if r["interface"] == "eth0" and r["host"] == "xcp-ng-host1")

    assert "fdir_miss" not in eth0["counters"]
    assert eth0["counters"]["rx_errors"] == 0


def test_link_summary_up_and_connected_names_speed() -> None:
    record = next(
        r
        for r in _interface_records(_physical_host_stats(), PIF_BY_HOST)
        if r["host"] == "xcp-ng-host1" and r["interface"] == "eth0"
    )
    assert link_summary(record) == "up, connected, 1000 Mb/s"


def test_link_summary_attached_with_no_carrier() -> None:
    record = next(
        r
        for r in _interface_records(_physical_host_stats(), PIF_BY_HOST)
        if r["host"] == "xcp-ng-host2" and r["interface"] == "eth0"
    )
    assert link_summary(record) == "up, no carrier"


def test_link_summary_unplugged() -> None:
    record = next(
        r
        for r in _interface_records(_physical_host_stats(), PIF_BY_HOST)
        if r["interface"] == "eth1"
    )
    assert link_summary(record) == "unplugged"


def test_link_summary_unknown_with_no_pif_lookup() -> None:
    records = _interface_records(HOST_STATS, {"xcp-ng-host1": None, "xcp-ng-host2": None})
    record = next(r for r in records if r["interface"] == "vif1.0")
    assert link_summary(record) == "unknown"


def test_errors_summary_none_when_every_counter_is_zero() -> None:
    record = next(
        r
        for r in _interface_records(_physical_host_stats(), PIF_BY_HOST)
        if r["interface"] == "eth1"
    )
    assert errors_summary(record) == "none"


def test_errors_summary_names_the_nonzero_counter() -> None:
    record = next(
        r
        for r in _interface_records(_physical_host_stats(), PIF_BY_HOST)
        if r["host"] == "xcp-ng-host2" and r["interface"] == "eth0"
    )
    assert errors_summary(record) == "rx_crc_errors: 3"


def test_markdown_lists_every_interface_even_on_a_clean_run() -> None:
    text = to_markdown(_report_with_interfaces())

    assert "No findings." in text
    assert "## Interfaces read" in text
    assert "| xcp-ng-host1 | eth0 | up, connected, 1000 Mb/s | unknown | none |" in text
    assert "| xcp-ng-host1 | eth1 | unplugged | unknown | none |" in text
    assert "| xcp-ng-host2 | eth0 | up, no carrier | unknown | rx_crc_errors: 3 |" in text


def test_markdown_says_nothing_about_interfaces_when_none_were_read() -> None:
    text = to_markdown(Report(sources=[SourceResult("nic_stats", read=False, reason="refused")]))
    assert "## Interfaces read" not in text


def test_stored_report_round_trips_the_interface_list(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    job = enqueue(conn, KIND, {"host_ids": ["host-1"]})
    report = _report_with_interfaces()
    store_json(
        conn,
        tmp_path,
        job_id=job.id,
        name=NIC_STATS_ARTIFACT,
        payload=to_payload(report),
    )

    rebuilt = report_from_job(conn, tmp_path, job.id)

    assert rebuilt.interfaces == report.interfaces


def test_an_older_stored_report_with_no_interfaces_key_still_loads(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A report stored before this feature existed has no "interfaces" key
    at all — it must still load, just with an empty interface list rather
    than raising."""
    job = enqueue(conn, KIND, {"host_ids": ["host-1"]})
    payload = to_payload(Report(sources=[SourceResult("nic_stats", read=True)], window_days=0))
    del payload["interfaces"]
    store_json(conn, tmp_path, job_id=job.id, name=NIC_STATS_ARTIFACT, payload=payload)

    rebuilt = report_from_job(conn, tmp_path, job.id)

    assert rebuilt.interfaces == []


def test_no_artifact_reads_as_none(conn: sqlite3.Connection, tmp_path: Path) -> None:
    job = enqueue(conn, KIND, {"host_ids": ["host-1"]})
    assert report_from_job(conn, tmp_path, job.id) is None
    assert list_for_job(conn, job.id) == []


def test_interface_records_carry_the_owning_network_and_its_nbd_status() -> None:
    records = _interface_records(
        _physical_only(HOST_STATS, PIF_BY_HOST_WITH_NETWORK),
        PIF_BY_HOST_WITH_NETWORK,
        NETWORK_BY_ID,
    )

    eth0 = next(r for r in records if r["host"] == "xcp-ng-host1" and r["interface"] == "eth0")
    eth1 = next(r for r in records if r["interface"] == "eth1")
    assert eth0["network"] == "Pool-wide network 0"
    assert eth0["nbd"] is True
    assert eth1["network"] == "Work"
    assert eth1["nbd"] is False


def test_interface_records_leave_network_none_without_a_lookup() -> None:
    records = _interface_records(_physical_host_stats(), PIF_BY_HOST)
    eth0 = next(r for r in records if r["host"] == "xcp-ng-host1" and r["interface"] == "eth0")

    assert eth0["network"] is None
    assert eth0["nbd"] is None


def test_nbd_summary_names_the_network_and_whether_nbd_is_on() -> None:
    assert nbd_summary({"network": "Pool-wide network 0", "nbd": True}) == (
        "Pool-wide network 0 (NBD)"
    )
    assert nbd_summary({"network": "Work", "nbd": False}) == "Work (no NBD)"


def test_nbd_summary_unknown_with_no_network() -> None:
    assert nbd_summary({"network": None, "nbd": None}) == "unknown"


def test_network_records_are_sorted_by_name() -> None:
    records = _network_records(list(NETWORK_BY_ID.values()), {"pool-1": "xcp-ng-Pool1"})

    assert [r["name"] for r in records] == ["Pool-wide network 0", "Work"]
    work = next(r for r in records if r["name"] == "Work")
    assert work["pool"] == "xcp-ng-Pool1"
    assert work["nbd"] is False
    assert work["vlan"] == 79


def test_network_records_fall_back_to_the_pool_id_when_the_pool_is_unknown() -> None:
    records = _network_records([NETWORK_BY_ID["net-nbd"]], {})
    assert records[0]["pool"] == "pool-1"


def _report_with_networks() -> Report:
    report = Report(sources=[SourceResult("nic_stats", read=True, examined=3)], window_days=0)
    report.networks = _network_records(list(NETWORK_BY_ID.values()), {"pool-1": "xcp-ng-Pool1"})
    return report


def test_markdown_lists_pool_networks() -> None:
    text = to_markdown(_report_with_networks())

    assert "## Pool networks" in text
    assert "| Pool-wide network 0 | xcp-ng-Pool1 | none | 1500 | yes | no | no | 3 |" in text
    assert "| Work | xcp-ng-Pool1 | 79 | 1500 | no | no | no | 3 |" in text


def test_markdown_says_nothing_about_networks_when_none_were_read() -> None:
    text = to_markdown(Report(sources=[SourceResult("nic_stats", read=False, reason="refused")]))
    assert "## Pool networks" not in text


def test_stored_report_round_trips_the_network_list(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    job = enqueue(conn, KIND, {"host_ids": ["host-1"]})
    report = _report_with_networks()
    store_json(
        conn,
        tmp_path,
        job_id=job.id,
        name=NIC_STATS_ARTIFACT,
        payload=to_payload(report),
    )

    rebuilt = report_from_job(conn, tmp_path, job.id)

    assert rebuilt.networks == report.networks


def test_an_older_stored_report_with_no_networks_key_still_loads(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A report stored before this feature existed has no "networks" key at
    all — it must still load, just with an empty network list."""
    job = enqueue(conn, KIND, {"host_ids": ["host-1"]})
    payload = to_payload(Report(sources=[SourceResult("nic_stats", read=True)], window_days=0))
    del payload["networks"]
    store_json(conn, tmp_path, job_id=job.id, name=NIC_STATS_ARTIFACT, payload=payload)

    rebuilt = report_from_job(conn, tmp_path, job.id)

    assert rebuilt.networks == []
