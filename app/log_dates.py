"""A date range for filtering log files and log lines, and the parser that
makes line-level filtering possible.

Two different filters share one ``DateRange``:

* **File-level**, by modification time — a rotated log outside the window is
  skipped entirely, the same way ``include_rotated`` already skips *all*
  rotated files in ``job_extract``. This never opens a file that does not
  matter.
* **Line-level**, for a file that straddles the window edge — the current
  ``xensource.log`` covers today and the last several days at once, so
  keeping the whole file or dropping it both give a wrong answer. Each line
  is parsed for a timestamp and kept only when that timestamp falls inside
  the range.

``xen-bugtool``'s bundle mixes several logging conventions in one archive —
syslog-style (``kern.log``, ``messages``), XAPI's own format
(``xensource.log``), and sysstat binaries this module never tries to parse.
**Best-effort, always.** A line whose timestamp cannot be parsed is kept
rather than dropped: silently discarding log data because a format was not
anticipated is a worse failure than a report a few lines wider than asked
for. See ``line_in_range``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

# The current year is assumed for formats that carry no year of their own
# (classic syslog, "Sep 11 14:32:01") — xen-bugtool captures logs as they are
# on the host, without rewriting timestamps to add one. A log line from
# December, read in January, would misparse to the wrong year; this is a
# yearless format's own limitation, not something this module can fix, so a
# rotated file spanning a year boundary should be selected by modification
# time (which does carry a year) rather than trusted at the line level near
# December/January.
_SYSLOG_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}  # fmt: skip

# ISO-8601-ish: "2026-09-11T14:32:01" or "2026-09-11 14:32:01", optionally
# with milliseconds and a timezone offset. Matches XAPI's own log format and
# anything else that leads a line with a sortable date.
_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?"
    r"(Z|[+-]\d{2}:?\d{2})?"
)

# Classic syslog: "Sep 11 14:32:01 hostname ...". No year, no timezone — see
# _SYSLOG_MONTHS above for why the current year is assumed.
_SYSLOG_RE = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\b")


@dataclass(frozen=True)
class DateRange:
    """A window in time, both ends inclusive, both as Unix timestamps (UTC).

    Either end may be open (``None``): "since last Tuesday" has no end, and
    "everything before the incident" has no start. A range with both ends
    ``None`` matches everything and is never constructed by the presets
    below, but a hand-built one doing so is treated as "no filtering" rather
    than an error, since it is a legitimate (if useless) window.
    """

    start: float | None
    end: float | None

    def contains(self, at: float) -> bool:
        if self.start is not None and at < self.start:
            return False
        if self.end is not None and at > self.end:
            return False
        return True

    @property
    def is_unbounded(self) -> bool:
        return self.start is None and self.end is None


# Presets shown on the picker, as (key, label, days-back-from-now). "Since
# last reboot" is not a fixed offset and is resolved separately, from the
# host's own uptime where one is known — see ``since_reboot``.
PRESET_24H = "24h"
PRESET_7D = "7d"
PRESET_30D = "30d"
PRESET_SINCE_REBOOT = "since_reboot"
PRESET_CUSTOM = "custom"

PRESETS: tuple[tuple[str, str, float | None], ...] = (
    (PRESET_24H, "Last 24 hours", 1),
    (PRESET_7D, "Last 7 days", 7),
    (PRESET_30D, "Last 30 days", 30),
    (PRESET_SINCE_REBOOT, "Since the last reboot", None),
    (PRESET_CUSTOM, "Custom range", None),
)

_PRESET_DAYS = {key: days for key, _label, days in PRESETS if days is not None}


def preset_range(
    key: str, *, now: float | None = None, uptime_seconds: float | None = None
) -> DateRange | None:
    """The range a preset key resolves to, or None for "custom"/unknown.

    ``uptime_seconds``, when given, resolves "since the last reboot" to an
    actual start time; without it that preset has nothing to compute from and
    resolves to None the same as an unrecognised key — the caller falls back
    to no filtering rather than guessing a boot time.
    """
    moment = time.time() if now is None else now
    if key == PRESET_SINCE_REBOOT:
        if uptime_seconds is None or uptime_seconds < 0:
            return None
        return DateRange(start=moment - uptime_seconds, end=None)
    days = _PRESET_DAYS.get(key)
    if days is None:
        return None
    return DateRange(start=moment - days * 86400, end=None)


def range_from_form(
    *,
    preset: str | None,
    start_date: str | None,
    end_date: str | None,
    now: float | None = None,
    uptime_seconds: float | None = None,
) -> DateRange | None:
    """The range a date-picker form submitted, or None for "no filtering".

    ``preset`` wins when it names a fixed-offset or reboot-based window.
    ``PRESET_CUSTOM`` (or any other value, including absent) falls through to
    the explicit ``start_date``/``end_date`` fields, each an ``YYYY-MM-DD``
    string from an HTML ``<input type="date">`` — a blank or unparsable value
    on either side leaves that end open rather than rejecting the whole
    range, since a one-sided window ("everything since the 1st") is a normal
    thing to ask for.

    Both dates blank (or absent, or every input malformed) returns None —
    the caller applies no filtering rather than an unbounded ``DateRange``,
    so "no range was submitted" and "a full-range preset was chosen" stay
    distinguishable at the call site without inspecting ``is_unbounded``.
    """
    if preset and preset != PRESET_CUSTOM:
        resolved = preset_range(preset, now=now, uptime_seconds=uptime_seconds)
        if resolved is not None:
            return resolved

    start = _parse_form_date(start_date, end_of_day=False)
    end = _parse_form_date(end_date, end_of_day=True)
    if start is None and end is None:
        return None
    return DateRange(start=start, end=end)


def _parse_form_date(value: str | None, *, end_of_day: bool) -> float | None:
    """One ``YYYY-MM-DD`` field as a Unix timestamp, or None.

    The end of a range is taken as the last instant of that day, so "to
    2026-09-11" includes everything that happened on the 11th rather than
    stopping at midnight and silently dropping the whole day.
    """
    if not value:
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    if end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return parsed.timestamp()


def mtime_in_range(mtime: float, date_range: DateRange | None) -> bool:
    """Whether a file's modification time falls inside the range.

    ``None`` means no filtering — every file matches, which is the existing
    behaviour when no range is given.
    """
    if date_range is None:
        return True
    return date_range.contains(mtime)


def parse_log_timestamp(line: str, *, now: float | None = None) -> float | None:
    """Best-effort: a Unix timestamp from the start of one log line, or None.

    None means "could not parse this line's timestamp", which the caller
    (``line_in_range``) treats as "keep it" — never as "outside the range".
    A parse failure is a gap in this function's format coverage, not evidence
    about when the line happened.
    """
    match = _ISO_RE.match(line)
    if match:
        year, month, day, hour, minute, second, tz = match.groups()
        try:
            dt = datetime(
                int(year),
                int(month),
                int(day),
                int(hour),
                int(minute),
                int(second),
                tzinfo=UTC,
            )
        except ValueError:
            return None
        # A non-UTC offset shifts the moment relative to UTC; "Z" and no
        # offset are already UTC. Only a numeric offset needs adjusting.
        if tz and tz != "Z":
            sign = 1 if tz[0] == "+" else -1
            digits = tz[1:].replace(":", "")
            offset_hours, offset_minutes = int(digits[:2]), int(digits[2:])
            dt = dt.replace(tzinfo=UTC)
            return dt.timestamp() - sign * (offset_hours * 3600 + offset_minutes * 60)
        return dt.timestamp()

    match = _SYSLOG_RE.match(line)
    if match:
        month_name, day, hour, minute, second = match.groups()
        month = _SYSLOG_MONTHS.get(month_name)
        if month is None:
            return None
        year = datetime.fromtimestamp(now if now is not None else time.time(), tz=UTC).year
        try:
            dt = datetime(year, month, int(day), int(hour), int(minute), int(second), tzinfo=UTC)
        except ValueError:
            return None
        return dt.timestamp()

    return None


def line_in_range(line: str, date_range: DateRange | None, *, now: float | None = None) -> bool:
    """Whether one log line belongs in a date-filtered output.

    ``None`` range means no filtering. A line whose timestamp cannot be
    parsed is always kept — see the module docstring's "best-effort, always".
    """
    if date_range is None:
        return True
    at = parse_log_timestamp(line, now=now)
    if at is None:
        return True
    return date_range.contains(at)
