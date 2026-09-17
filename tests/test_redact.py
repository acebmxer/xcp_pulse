"""The redaction rules.

These are the tests that decide whether a bundle is safe to send, so they check
both directions: that a secret is masked, and that ordinary log furniture is
not. Over-masking is a real failure here — a log with its timestamps replaced
is useless to the support engineer the bundle is for.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db import init_db
from app.redact import (
    DEFAULT_ENABLED,
    RULES,
    build_username_rule,
    enabled_rules,
    redact_json,
    redact_line,
    redact_text,
    rule_by_name,
    set_enabled_rules,
)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("management address 10.20.30.41 up", "management address [IPv4] up"),
        ("peer fe80::1c2d:3e4f:5a6b:7c8d replied", "peer [IPv6] replied"),
        (
            "addr 2001:0db8:85a3:0000:0000:8a2e:0370:7334 ok",
            "addr [IPv6] ok",
        ),
        ("VIF 3a:4b:5c:6d:7e:8f attached", "VIF [MAC] attached"),
        (
            "host 4c8a1f2e-77b3-4a91-9f0e-2d5c8b1a6e34 enabled",
            "host [UUID] enabled",
        ),
        ("trackid=a3f9c2b18e4d0c67 login", "trackid=[TOKEN] login"),
        ("password=Str0ngPass! mounting", "password=[SECRET] mounting"),
        ("session_id: 9f8e7d6c done", "session_id: [SECRET] done"),
        ("user admin@internal.example logged in", "user [EMAIL] logged in"),
        ("connect to xen01.internal.example now", "connect to xen01.internal.example now"),
        ("peer xo-ce.pozzatech.com replied", "peer [HOST] replied"),
    ],
)
def test_each_rule_masks_its_own_shape(line: str, expected: str) -> None:
    assert redact_line(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        # The single most important non-match: this prefixes nearly every
        # syslog line, and an IPv6 rule that eats it makes bundles unreadable.
        "Sep  6 12:30:45 xapi: starting",
        "health check from 127.0.0.1 ok",
        "bound to 0.0.0.0 port 443",
        "loopback ::1 responded",
        "ratio 1:2:3 within range",
        "netmask 255.255.255.255 broadcast",
        "resolving localhost failed",
        "read 4096 bytes in 12ms",
    ],
)
def test_ordinary_log_text_is_left_alone(line: str) -> None:
    assert redact_line(line) == line


def test_a_mac_is_labelled_a_mac_not_an_address() -> None:
    """A MAC is also colon-separated hex, so rule order decides the label."""
    assert "[MAC]" in redact_line("VIF 3a:4b:5c:6d:7e:8f up")
    assert "[IPv6]" not in redact_line("VIF 3a:4b:5c:6d:7e:8f up")


def test_a_secret_beats_the_value_shape_it_holds() -> None:
    """`password=10.0.0.1` is a password, not an address."""
    assert redact_line("password=10.0.0.1") == "password=[SECRET]"


def test_the_key_survives_so_the_line_still_reads() -> None:
    result = redact_line("cmd --password=hunter2 --user=admin")
    assert result.startswith("cmd --password=[SECRET]")


def test_equal_values_get_equal_placeholders() -> None:
    """Two mentions of one host must still look like one host."""
    result = redact_line("10.20.30.41 talked to 10.20.30.41")
    assert result == "[IPv4] talked to [IPv4]"


def test_counts_are_reported_per_rule() -> None:
    text = "10.0.0.1 and 10.0.0.2 and trackid=abc123\n"
    _, counts = redact_text(text)
    assert counts["ipv4"] == 2
    assert counts["trackid"] == 1
    assert "mac" not in counts


def test_line_endings_are_preserved() -> None:
    """A redacted log must still line up with the file it came from."""
    text = "a 10.0.0.1\r\nb 10.0.0.2\r\n"
    result, _ = redact_text(text)
    assert result == "a [IPv4]\r\nb [IPv4]\r\n"
    text_no_trailing = "a 10.0.0.1\nb"
    result_no_trailing, _ = redact_text(text_no_trailing)
    assert result_no_trailing == "a [IPv4]\nb"


def test_disabling_a_rule_leaves_its_values_alone() -> None:
    enabled = DEFAULT_ENABLED - {"uuid"}
    line = "host 4c8a1f2e-77b3-4a91-9f0e-2d5c8b1a6e34 at 10.0.0.1"
    result = redact_line(line, enabled)
    assert "4c8a1f2e-77b3-4a91-9f0e-2d5c8b1a6e34" in result
    assert "[IPv4]" in result


def test_no_rules_enabled_changes_nothing() -> None:
    line = "password=hunter2 at 10.0.0.1"
    assert redact_line(line, frozenset()) == line


def test_empty_text_is_handled() -> None:
    result, counts = redact_text("")
    assert result == ""
    assert counts == {}


def test_rule_by_name_finds_and_misses() -> None:
    assert rule_by_name("ipv4") is not None
    assert rule_by_name("nonesuch") is None


def test_every_rule_has_a_unique_name_and_a_description() -> None:
    names = [rule.name for rule in RULES]
    assert len(names) == len(set(names))
    for rule in RULES:
        assert rule.title and rule.description
        assert rule.placeholder.startswith("[")


def test_default_is_everything_on() -> None:
    """The safe default: a rule is only off when someone turns it off."""
    assert DEFAULT_ENABLED == frozenset(rule.name for rule in RULES)


# Real Xen Orchestra and XAPI logs are full of dotted tokens that are not hosts.
# Masking them wrecks the backtrace the support ticket exists to explain, which
# is exactly what the first version of this rule did to a pasted XapiError.
@pytest.mark.parametrize(
    "line",
    [
        "filename ocaml/xapi/xapi_host.ml line 629",
        "filename lib/xapi-stdext-pervasives/pervasiveext.ml line 159",
        "at file:///usr/local/lib/node_modules/xen-api/index.mjs:1229:24",
        "at default (node_modules/xen-api/_getTaskResult.mjs:13:29)",
        "host.setMaintenanceMode called",
        "vm.start requested",
        "XapiError: VM_REQUIRES_SR raised",
        "reading /etc/xensource/network.conf now",
        "rotated xensource.log.3.gz",
    ],
)
def test_filenames_and_method_names_are_not_hostnames(line: str) -> None:
    assert redact_line(line) == line


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("peer xo-ce.pozzatech.com replied", "peer [HOST] replied"),
        ("backup to nas01.home.lan done", "backup to [HOST] done"),
        ("joined node7.dc1.internal ok", "joined [HOST] ok"),
        ("proxy via cache.corp.net up", "proxy via [HOST] up"),
    ],
)
def test_real_hostnames_are_still_masked(line: str, expected: str) -> None:
    assert redact_line(line) == expected


def test_a_backtrace_survives_redaction_intact() -> None:
    """The line a support engineer most needs must come through unharmed."""
    line = (
        '"backtrace": "(((process xapi)(filename ocaml/xapi/xapi_host.ml)'
        "(line 629))((process xapi)(filename lib/xapi-stdext-pervasives/"
        'pervasiveext.ml)(line 159)))"'
    )
    assert redact_line(line) == line


def test_an_opaqueref_keeps_its_prefix() -> None:
    """OpaqueRef: names the kind of thing; only the identifier is secret."""
    result = redact_line('"resident_on": "OpaqueRef:2b0f6bd0-1234-4a1b-9c8d-aabbccddeeff"')
    assert result == '"resident_on": "OpaqueRef:[UUID]"'


# ---- storing which rules are on ----


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(tmp_path / "test.db")
    yield connection
    connection.close()


def test_a_fresh_database_has_every_rule_on(conn: sqlite3.Connection) -> None:
    """Nothing stored means nothing switched off — the safe default."""
    assert enabled_rules(conn) == DEFAULT_ENABLED


def test_setting_the_enabled_set_switches_off_the_rest(conn: sqlite3.Connection) -> None:
    assert set_enabled_rules(conn, ["ipv4", "uuid"]) == {"ipv4", "uuid"}
    assert enabled_rules(conn) == {"ipv4", "uuid"}


def test_an_empty_set_switches_everything_off(conn: sqlite3.Connection) -> None:
    set_enabled_rules(conn, [])
    assert enabled_rules(conn) == frozenset()
    assert redact_line("host 10.0.0.1 password=hunter2", enabled_rules(conn)) == (
        "host 10.0.0.1 password=hunter2"
    )


def test_a_rule_switched_off_stops_masking(conn: sqlite3.Connection) -> None:
    set_enabled_rules(conn, [name for name in DEFAULT_ENABLED if name != "ipv4"])
    line = "management address 10.20.30.41 up"
    assert redact_line(line, enabled_rules(conn)) == line
    # The rules left on are unaffected.
    assert redact_line("host 4c8a1f2e-77b3-4a91-9f0e-2d5c8b1a6e34", enabled_rules(conn)) == (
        "host [UUID]"
    )


def test_switching_back_on_leaves_nothing_behind(conn: sqlite3.Connection) -> None:
    set_enabled_rules(conn, ["ipv4"])
    set_enabled_rules(conn, [rule.name for rule in RULES])
    assert enabled_rules(conn) == DEFAULT_ENABLED
    assert conn.execute("SELECT COUNT(*) AS n FROM redaction_disabled").fetchone()["n"] == 0


def test_an_unknown_name_is_dropped_rather_than_stored(conn: sqlite3.Connection) -> None:
    assert set_enabled_rules(conn, ["ipv4", "no-such-rule"]) == {"ipv4"}
    assert enabled_rules(conn) == {"ipv4"}


def test_a_stored_name_that_is_no_longer_a_rule_is_ignored(conn: sqlite3.Connection) -> None:
    """Renaming a rule must turn it back on, not raise."""
    conn.execute("INSERT INTO redaction_disabled (name, disabled_at) VALUES ('retired-rule', 0)")
    conn.commit()
    assert enabled_rules(conn) == DEFAULT_ENABLED


def test_a_new_rule_is_on_even_on_a_database_written_before_it(conn: sqlite3.Connection) -> None:
    """Storing the off-set rather than the on-set is what buys this.

    Simulated by switching everything off *except* one rule, then asking for a
    rule the stored rows never mentioned — which is the position a rule added in
    a later version is in.
    """
    conn.execute("DELETE FROM redaction_disabled")
    conn.executemany(
        "INSERT INTO redaction_disabled (name, disabled_at) VALUES (?, 0)",
        [("ipv4",)],
    )
    conn.commit()
    assert "uuid" in enabled_rules(conn)
    assert "ipv4" not in enabled_rules(conn)


# ---- the "username" rule: no fixed pattern of its own ----


def test_username_rule_exists_and_is_inert_by_default() -> None:
    """The static RULES entry matches nothing until given a live list."""
    rule = rule_by_name("username")
    assert rule is not None
    assert rule.pattern is None
    assert redact_line("nick logged in") == "nick logged in"


def test_build_username_rule_with_no_names_returns_the_static_entry() -> None:
    assert build_username_rule([]) is rule_by_name("username")
    assert build_username_rule([""]) is rule_by_name("username")


def test_build_username_rule_masks_known_names() -> None:
    rule = build_username_rule(["nick", "claude-test"])
    assert redact_line("nick logged in", username_rule=rule) == "[USER] logged in"
    assert redact_line("claude-test ran a job", username_rule=rule) == "[USER] ran a job"


def test_build_username_rule_is_case_insensitive() -> None:
    rule = build_username_rule(["nick"])
    assert redact_line("NICK logged in", username_rule=rule) == "[USER] logged in"


def test_build_username_rule_respects_word_boundaries() -> None:
    """A username must not eat "nickel" — the same care every other rule takes."""
    rule = build_username_rule(["nick"])
    assert redact_line("a nickel fell", username_rule=rule) == "a nickel fell"


def test_build_username_rule_ignores_blank_and_duplicate_entries() -> None:
    rule = build_username_rule(["nick", "", "  ", "nick"])
    assert redact_line("nick logged in", username_rule=rule) == "[USER] logged in"


def test_username_rule_is_off_when_disabled_even_with_a_live_list() -> None:
    rule = build_username_rule(["nick"])
    enabled = DEFAULT_ENABLED - {"username"}
    assert redact_line("nick logged in", enabled, username_rule=rule) == "nick logged in"


def test_username_rule_does_not_affect_other_rules() -> None:
    rule = build_username_rule(["nick"])
    result = redact_line("nick at 10.0.0.1", username_rule=rule)
    assert result == "[USER] at [IPv4]"


# ---- redact_json: masking a parsed JSON structure ----


def test_redact_json_masks_string_leaves_only() -> None:
    value = {
        "id": "job-1",
        "ip": "10.0.0.1",
        "tags": ["10.0.0.2", 3, None, True],
        "nested": {"message": "trackid=abc123 failed"},
    }
    masked, counts = redact_json(value)
    assert masked["id"] == "job-1"
    assert masked["ip"] == "[IPv4]"
    assert masked["tags"] == ["[IPv4]", 3, None, True]
    assert masked["nested"]["message"] == "trackid=[TOKEN] failed"
    assert counts["ipv4"] == 2
    assert counts["trackid"] == 1


def test_redact_json_leaves_keys_untouched() -> None:
    """A key that happens to look like an address must not be masked."""
    value = {"10.0.0.1": "reachable"}
    masked, _ = redact_json(value)
    assert "10.0.0.1" in masked
    assert masked["10.0.0.1"] == "reachable"


def test_redact_json_respects_the_enabled_set() -> None:
    value = {"ip": "10.0.0.1", "id": "4c8a1f2e-77b3-4a91-9f0e-2d5c8b1a6e34"}
    masked, counts = redact_json(value, DEFAULT_ENABLED - {"ipv4"})
    assert masked["ip"] == "10.0.0.1"
    assert masked["id"] == "[UUID]"
    assert "ipv4" not in counts


def test_redact_json_supports_the_username_rule() -> None:
    rule = build_username_rule(["nick"])
    value = {"properties": {"credentials": {"username": "nick"}}}
    masked, counts = redact_json(value, username_rule=rule)
    assert masked["properties"]["credentials"]["username"] == "[USER]"
    assert counts["username"] == 1


def test_redact_json_survives_quotes_and_backslashes_in_values() -> None:
    """The actual point of walking the tree instead of regexing serialised JSON."""
    import json

    value = {"message": 'path "C:\\logs\\10.0.0.1.txt" unreachable'}
    masked, _ = redact_json(value)
    # Round-tripping proves the structure was never corrupted by a masked value
    # that happened to contain a quote or backslash.
    round_tripped = json.loads(json.dumps(masked))
    assert round_tripped == masked
    assert "[IPv4]" in masked["message"]


def test_redact_json_on_a_bare_string_or_scalar() -> None:
    masked, counts = redact_json("trackid=abc123")
    assert masked == "trackid=[TOKEN]"
    assert counts == {"trackid": 1}
    assert redact_json(42) == (42, {})
    assert redact_json(None) == (None, {})
