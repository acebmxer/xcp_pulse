"""The date-range parser and filters shared by extraction, collection and
findings."""

from __future__ import annotations

from datetime import UTC, datetime

from app.log_dates import (
    DateRange,
    line_in_range,
    mtime_in_range,
    parse_log_timestamp,
    preset_range,
    range_from_form,
)


def _ts(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC).timestamp()


# -- DateRange -------------------------------------------------------------


def test_date_range_both_ends_inclusive():
    r = DateRange(start=_ts(2026, 1, 1), end=_ts(2026, 1, 31, 23, 59, 59))
    assert r.contains(_ts(2026, 1, 1))
    assert r.contains(_ts(2026, 1, 31, 23, 59, 59))
    assert not r.contains(_ts(2025, 12, 31, 23, 59, 59))
    assert not r.contains(_ts(2026, 2, 1))


def test_date_range_open_start_or_end():
    since = DateRange(start=_ts(2026, 6, 1), end=None)
    assert since.contains(_ts(2030, 1, 1))
    assert not since.contains(_ts(2026, 5, 31))

    until = DateRange(start=None, end=_ts(2026, 6, 1))
    assert until.contains(_ts(2000, 1, 1))
    assert not until.contains(_ts(2026, 6, 2))


def test_date_range_unbounded():
    assert DateRange(start=None, end=None).is_unbounded


# -- presets -----------------------------------------------------------------


def test_preset_24h_and_7d_and_30d():
    now = _ts(2026, 9, 11, 12, 0, 0)
    assert preset_range("24h", now=now).start == now - 86400
    assert preset_range("7d", now=now).start == now - 7 * 86400
    assert preset_range("30d", now=now).start == now - 30 * 86400
    for key in ("24h", "7d", "30d"):
        assert preset_range(key, now=now).end is None


def test_preset_since_reboot_needs_uptime():
    now = _ts(2026, 9, 11, 12, 0, 0)
    assert preset_range("since_reboot", now=now) is None
    resolved = preset_range("since_reboot", now=now, uptime_seconds=3600)
    assert resolved.start == now - 3600
    assert resolved.end is None


def test_preset_unknown_key_returns_none():
    assert preset_range("nonsense") is None
    assert preset_range("custom") is None


# -- range_from_form -----------------------------------------------------------


def test_range_from_form_preset_wins_over_dates():
    now = _ts(2026, 9, 11)
    r = range_from_form(preset="7d", start_date="2020-01-01", end_date="2020-01-02", now=now)
    assert r.start == now - 7 * 86400


def test_range_from_form_custom_dates():
    r = range_from_form(preset="custom", start_date="2026-01-01", end_date="2026-01-31")
    assert r.start == _ts(2026, 1, 1)
    assert r.end == _ts(2026, 1, 31, 23, 59, 59)


def test_range_from_form_one_sided():
    r = range_from_form(preset=None, start_date="2026-01-01", end_date="")
    assert r.start == _ts(2026, 1, 1)
    assert r.end is None

    r = range_from_form(preset=None, start_date="", end_date="2026-01-31")
    assert r.start is None
    assert r.end == _ts(2026, 1, 31, 23, 59, 59)


def test_range_from_form_nothing_submitted_is_none():
    assert range_from_form(preset=None, start_date=None, end_date=None) is None
    assert range_from_form(preset="", start_date="", end_date="") is None
    assert range_from_form(preset="custom", start_date="not-a-date", end_date="") is None


def test_range_from_form_since_reboot_needs_uptime_too():
    now = _ts(2026, 9, 11)
    assert range_from_form(preset="since_reboot", start_date="", end_date="", now=now) is None
    r = range_from_form(
        preset="since_reboot", start_date="", end_date="", now=now, uptime_seconds=7200
    )
    assert r.start == now - 7200


# -- mtime_in_range ------------------------------------------------------------


def test_mtime_in_range_no_filter_matches_everything():
    assert mtime_in_range(_ts(2000, 1, 1), None)


def test_mtime_in_range_respects_window():
    window = DateRange(start=_ts(2026, 1, 1), end=_ts(2026, 1, 31, 23, 59, 59))
    assert mtime_in_range(_ts(2026, 1, 15), window)
    assert not mtime_in_range(_ts(2025, 12, 1), window)


# -- parse_log_timestamp -------------------------------------------------------


def test_parse_iso_with_t_separator():
    at = parse_log_timestamp("2026-09-11T14:32:01.123456 xapi: [debug] starting")
    assert at == _ts(2026, 9, 11, 14, 32, 1)


def test_parse_iso_with_space_separator():
    at = parse_log_timestamp("2026-09-11 14:32:01 xapi event")
    assert at == _ts(2026, 9, 11, 14, 32, 1)


def test_parse_iso_with_z_suffix():
    at = parse_log_timestamp("2026-09-11T14:32:01Z something happened")
    assert at == _ts(2026, 9, 11, 14, 32, 1)


def test_parse_iso_with_numeric_offset():
    # +02:00 means 12:32:01 UTC
    at = parse_log_timestamp("2026-09-11T14:32:01+02:00 something")
    assert at == _ts(2026, 9, 11, 12, 32, 1)


def test_parse_syslog_style_uses_current_year():
    now = _ts(2026, 9, 11)
    at = parse_log_timestamp("Sep 11 14:32:01 xcp-ng-host1 kernel: message", now=now)
    assert at == _ts(2026, 9, 11, 14, 32, 1)


def test_parse_unrecognised_format_returns_none():
    assert parse_log_timestamp("not a timestamped line at all") is None
    assert parse_log_timestamp("") is None


def test_parse_invalid_calendar_date_returns_none():
    # Feb 30 does not exist; a malformed match must not raise.
    assert parse_log_timestamp("2026-02-30T00:00:00 whatever") is None


# -- line_in_range --------------------------------------------------------------


def test_line_in_range_no_filter_keeps_everything():
    assert line_in_range("anything at all", None)


def test_line_in_range_keeps_unparsable_lines():
    window = DateRange(start=_ts(2026, 1, 1), end=_ts(2026, 1, 2))
    assert line_in_range("no timestamp here", window)


def test_line_in_range_filters_parsed_lines():
    window = DateRange(start=_ts(2026, 1, 1), end=_ts(2026, 1, 31, 23, 59, 59))
    assert line_in_range("2026-01-15T00:00:00 inside the window", window)
    assert not line_in_range("2026-03-01T00:00:00 outside the window", window)
