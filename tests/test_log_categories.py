"""Which log family a bundle member belongs to.

The prefixes in app.log_categories were built from a real bundle's file list
(measured against xcp-ng-host1, 2026-09-11). These tests check the mapping
logic — rotation stripping, fallback, exact vs. directory matching — rather
than re-asserting every one of the 609 real paths.
"""

from __future__ import annotations

from app.log_categories import CATEGORIES, canonical_name, category_by_key, category_keys, classify


def test_every_category_key_is_unique() -> None:
    keys = [category.key for category in CATEGORIES]
    assert len(keys) == len(set(keys))


def test_category_keys_matches_the_declared_order() -> None:
    assert category_keys() == tuple(category.key for category in CATEGORIES)


def test_category_by_key_finds_a_real_one() -> None:
    assert category_by_key("storage") is not None
    assert category_by_key("storage").title == "Storage"


def test_category_by_key_returns_none_for_an_unknown_key() -> None:
    assert category_by_key("not-a-category") is None


def test_canonical_name_strips_var_log_prefix() -> None:
    assert canonical_name("var/log/xensource.log") == "xensource.log"


def test_canonical_name_strips_gzipped_rotation() -> None:
    assert canonical_name("var/log/xensource.log.27.gz") == "xensource.log"


def test_canonical_name_strips_bare_numeric_rotation() -> None:
    assert canonical_name("var/log/SMlog.1") == "SMlog"


def test_canonical_name_leaves_a_current_file_alone() -> None:
    assert canonical_name("var/log/daemon.log") == "daemon.log"


def test_classify_matches_an_exact_name() -> None:
    assert classify("var/log/xensource.log") == "xapi"
    assert classify("var/log/SMlog") == "storage"
    assert classify("var/log/audit.log") == "audit"
    assert classify("var/log/xha.log") == "ha"
    assert classify("var/log/xenstored-access.log") == "xenstore"


def test_classify_matches_a_directory_prefix() -> None:
    assert classify("var/log/openvswitch/ovs-vswitchd.log") == "network"
    assert classify("var/log/blktap/tapdisk.1234.log") == "storage"
    assert classify("var/log/installer/dmesg-log") == "kernel"


def test_classify_matches_rotated_members_the_same_as_current() -> None:
    assert classify("var/log/xensource.log.31.gz") == "xapi"
    assert classify("var/log/xensource.log.12") == "xapi"


def test_classify_falls_back_to_system_rather_than_dropping_a_file() -> None:
    # A path not present in the measured bundle at all — a future XCP-ng
    # release adding a new file must not vanish from every extraction.
    assert classify("var/log/some-new-plugin.log") == "system"


def test_every_real_bundle_member_measured_is_classified() -> None:
    """Every canonical name measured in the real 609-member bundle classifies.

    Not a re-assertion of every mapping — just that nothing raises and nothing
    in this real, ground-truth list falls outside the known category keys.
    """
    measured = [
        "var/log/xensource.log",
        "var/log/xen/hypervisor.log",
        "var/log/SMlog",
        "var/log/blktap/tapdisk.1900805.log",
        "var/log/raid-plugin.log",
        "var/log/smartctl-plugin.log",
        "var/log/lvm-plugin.log",
        "var/log/twinstor.log",
        "var/log/twinstor-fence-peer.log",
        "var/log/drbd-kern.log",
        "var/log/audit.log",
        "var/log/secure",
        "var/log/btmp",
        "var/log/wtmp",
        "var/log/lastlog",
        "var/log/tallylog",
        "var/log/maillog",
        "var/log/kern.log",
        "var/log/boot.log",
        "var/log/installer/dmesg-log",
        "var/log/xha.log",
        "var/log/xenstored-access.log",
        "var/log/xcp-rrdd-plugins.log",
        "var/log/VMSSlog",
        "var/log/openvswitch/ovs-ctl.log",
        "var/log/interface-rename.log",
        "var/log/daemon.log",
        "var/log/messages",
        "var/log/cron",
        "var/log/user.log",
        "var/log/yum.log",
        "var/log/installer/install-log",
        "var/log/sa/sa01",
        "var/log/updater-plugin.log",
        "var/log/ipmitool-xapi-plugin-plugin.log",
        "var/log/grubby_prune_debug",
        "var/log/spooler",
    ]
    keys = {category.key for category in CATEGORIES}
    for path in measured:
        assert classify(path) in keys, path
