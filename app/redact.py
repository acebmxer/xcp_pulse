"""Masking internal addresses, tokens and credentials out of log text.

The bundle a support ticket wants is full of things that should not leave the
site: management addresses, session tokens, password assignments, the names of
internal machines. This module is what stands between a cached bundle and
anything downloadable.

Three properties drive the design.

**It works a line at a time.** ``redact_line`` takes a string and returns a
string, so the same rules serve a paste into the preview page and a streaming
repack of a 56 MB log that must never be held in memory.

**A replacement keeps the shape of what it replaced.** ``10.20.30.40`` becomes
``[IPv4]``, not an empty string. A reader of a redacted log still needs to see
that there *was* an address there, and someone diagnosing a fault needs to see
that two lines mention the same one — so equal values get equal placeholders.

**Loopback and link-local are left alone.** ``127.0.0.1`` identifies nobody and
masking it makes a log harder to read for no gain in privacy.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.db import transaction

# Addresses that reveal nothing about the site and are worth keeping legible.
# 0.0.0.0 is a bind-to-everything marker rather than a host.
_IPV4_KEEP = frozenset({"127.0.0.1", "0.0.0.0", "255.255.255.255"})
_IPV6_KEEP = frozenset({"::", "::1"})

# Hostnames ending in these are documentation or loopback names, not a site's.
_HOST_KEEP_SUFFIXES = ("localhost", ".local", ".example.com", ".example.org", ".example.net")

# What a hostname's final label may be. Deciding by an explicit list is the only
# thing that works on real logs: `xapi_host.ml`, `host.setMaintenanceMode` and
# `vm.start` are all dot-separated labels, and masking them wrecks the backtrace
# a support ticket exists to explain. Shape alone cannot separate them — length
# does not either, since `setMaintenanceMode` is longer than every real TLD.
#
# The list is the TLDs that plausibly appear in an XCP-ng or Xen Orchestra log,
# plus the internal suffixes sites actually use. A host under some other TLD is
# missed rather than mangled, which is the safer way to be wrong here: the
# operator sees it in the preview and can say so.
_HOSTNAME_SUFFIXES = frozenset(
    """
    com org net edu gov mil int io co uk us ca au de fr nl eu es it se ch dk
    no fi pl cz at be pt ie nz jp cn in br ru za info biz name pro tech cloud
    dev app site online host systems network solutions services email
    internal intranet lan corp home private priv localdomain domain site1
    """.split()
)

_UUID = r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"

# Assignment keys treated as secret wherever they appear: key, separator, value.
# Matching the key rather than the value shape is what catches a password that
# happens to look like an ordinary word.
_SECRET_KEYS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "session_id",
    "sessionid",
    "auth[_-]?token",
    "api[_-]?key",
    "apikey",
    "authorization",
    "private[_-]?key",
)


def _is_hostname(value: str) -> bool:
    """Whether a dotted token is worth masking as a hostname.

    The pattern alone cannot tell `xen01.internal.example` from `xapi_host.ml`,
    `host.setMaintenanceMode` or `vm.start` — all four are dot-separated labels,
    and real logs are mostly the last three. Masking those destroys the
    backtrace a support ticket exists to explain, so the decision is made on the
    final label against an explicit suffix list rather than on shape.

    Being wrong in the missing direction is the safer failure: an unmasked host
    under an unlisted TLD is visible in the preview and can be reported, whereas
    a mangled stack trace is silently useless.
    """
    lowered = value.lower()
    if lowered.endswith(_HOST_KEEP_SUFFIXES):
        return False

    labels = lowered.split(".")
    # An underscore is legal in no hostname label, but common in identifiers.
    if any("_" in label for label in labels):
        return False
    return labels[-1] in _HOSTNAME_SUFFIXES


@dataclass(frozen=True)
class Rule:
    """One pattern and what it replaces matches with.

    ``keep`` is the exception list: a value the rule matched but which is not
    worth masking. It is per-rule rather than global because "leave loopback
    alone" means different literals for IPv4, IPv6 and hostnames.
    """

    name: str
    title: str
    description: str
    pattern: re.Pattern[str]
    placeholder: str
    keep: frozenset[str] = field(default=frozenset())

    def apply(self, text: str) -> tuple[str, int]:
        """Mask every match in ``text``. Returns the result and the hit count.

        The count is what a redaction report is built from later, which is why
        it is produced here rather than by re-scanning the output — a rule
        whose placeholder its own pattern matches would count wrongly.
        """
        hits = 0

        def _replace(match: re.Match[str]) -> str:
            nonlocal hits
            value = match.group(0)
            if self.keep and value.lower() in self.keep:
                return value
            if self.name == "hostname" and not _is_hostname(value):
                return value
            hits += 1
            # A rule with a group masks only that group, so `password=x` keeps
            # its key and loses only the value. The spans are absolute, so they
            # are rebased onto the matched text before slicing it.
            if match.groups():
                start, end = match.span(1)
                offset = match.start()
                return value[: start - offset] + self.placeholder + value[end - offset :]
            return self.placeholder

        return self.pattern.sub(_replace, text), hits


# The rules, in the order they are applied. Order matters: the secret-assignment
# rule runs first so that `password=10.0.0.1` is masked as a password rather
# than as an address, and the email rule runs before hostname so an address is
# not half-eaten by its own domain.
RULES: tuple[Rule, ...] = (
    Rule(
        name="secret",
        title="Passwords and secrets",
        description=(
            "Values assigned to password, secret, session_id, auth token, "
            "API key or authorization keys."
        ),
        pattern=re.compile(
            r"(?i)\b(?:" + "|".join(_SECRET_KEYS) + r")\b\s*[=:]\s*[\"']?([^\s\"',;)&]+)",
        ),
        placeholder="[SECRET]",
    ),
    Rule(
        name="trackid",
        title="Session tokens",
        description="Xen Orchestra and XAPI trackid session identifiers.",
        pattern=re.compile(r"(?i)\btrackid\s*[=:]\s*([0-9a-fA-F]+)"),
        placeholder="[TOKEN]",
    ),
    Rule(
        name="email",
        title="Email addresses",
        description="Anything of the form name@domain.",
        pattern=re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        placeholder="[EMAIL]",
    ),
    Rule(
        name="ipv4",
        title="IPv4 addresses",
        description="Dotted-quad addresses, keeping loopback and 0.0.0.0 legible.",
        pattern=re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        placeholder="[IPv4]",
        keep=frozenset(v.lower() for v in _IPV4_KEEP),
    ),
    Rule(
        name="mac",
        title="MAC addresses",
        description="Six colon- or hyphen-separated octets.",
        # Before IPv6: a MAC is also a run of colon-separated hex, so whichever
        # of the two runs first claims it, and "MAC" is the more useful label.
        pattern=re.compile(r"\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b"),
        placeholder="[MAC]",
    ),
    Rule(
        name="ipv6",
        title="IPv6 addresses",
        description="Colon-separated addresses, keeping :: and ::1 legible.",
        # Written as whole-address alternatives rather than "a run of hex and
        # colons", because that looser form both matched the `12:30:45`
        # timestamp on nearly every syslog line and ate only the tail of a real
        # address, leaving its first group unmasked. An address must therefore
        # either contain `::` or be all eight groups.
        pattern=re.compile(
            r"(?<![0-9a-zA-Z:.])(?:"
            r"(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}"
            r"|(?:[0-9a-fA-F]{1,4}:){1,7}:(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4}){0,6})?"
            r"|::(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4}){0,7})?"
            r")(?![0-9a-zA-Z:.])"
        ),
        placeholder="[IPv6]",
        keep=frozenset(_IPV6_KEEP),
    ),
    Rule(
        name="uuid",
        title="UUIDs",
        description="Pool, host, VM, storage and network identifiers.",
        pattern=re.compile(_UUID),
        placeholder="[UUID]",
    ),
    Rule(
        name="hostname",
        title="Hostnames",
        description=(
            "Dotted names such as xen01.internal.example. Bare single words are left alone."
        ),
        # Deliberately only dotted names. A bare word rule would match half the
        # vocabulary of a log file, and an unreadable bundle helps nobody.
        pattern=re.compile(r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}\b"),
        placeholder="[HOST]",
    ),
)

# Every rule is on unless a caller says otherwise. Turning one off is a
# deliberate act, so the safe default is everything masked.
DEFAULT_ENABLED: frozenset[str] = frozenset(rule.name for rule in RULES)


def rule_by_name(name: str) -> Rule | None:
    """One rule, or None when nothing is called that."""
    for rule in RULES:
        if rule.name == name:
            return rule
    return None


def active_rules(enabled: frozenset[str] | set[str] | None = None) -> tuple[Rule, ...]:
    """The rules to apply, in order. ``None`` means all of them."""
    if enabled is None:
        return RULES
    return tuple(rule for rule in RULES if rule.name in enabled)


def redact_line(line: str, enabled: frozenset[str] | set[str] | None = None) -> str:
    """Mask one line. The unit everything else is built from.

    A line rather than a whole file because the caller that matters most reads
    a 56 MB log a line at a time and must never hold it in memory.
    """
    for rule in active_rules(enabled):
        line, _ = rule.apply(line)
    return line


def redact_text(
    text: str, enabled: frozenset[str] | set[str] | None = None
) -> tuple[str, dict[str, int]]:
    """Mask a block of text. Returns the result and per-rule hit counts.

    Line endings are preserved exactly, because a redacted log that has had its
    CRLFs rewritten no longer matches the file it came from.
    """
    counts: dict[str, int] = {}
    rules = active_rules(enabled)
    out: list[str] = []

    for line in text.splitlines(keepends=True):
        for rule in rules:
            line, hits = rule.apply(line)
            if hits:
                counts[rule.name] = counts.get(rule.name, 0) + hits
        out.append(line)

    return "".join(out), counts


def enabled_rules(conn: sqlite3.Connection) -> frozenset[str]:
    """The names of the rules currently switched on.

    Read as "everything except what is stored as off", so a rule added to
    ``RULES`` in a later version is on from the moment it exists, on databases
    written before it did. A stored name that no longer matches a rule is
    ignored rather than raising — renaming a rule turns it back on, which is
    the safe direction.
    """
    rows = conn.execute("SELECT name FROM redaction_disabled").fetchall()
    disabled = {row["name"] for row in rows}
    return frozenset(rule.name for rule in RULES if rule.name not in disabled)


def set_enabled_rules(conn: sqlite3.Connection, names: Iterable[str]) -> frozenset[str]:
    """Switch on exactly the named rules and switch off the rest.

    Written as a whole set rather than one toggle at a time because the form it
    serves posts every checkbox at once: an unticked box sends nothing, so the
    absence of a name is the instruction to turn it off, which only a
    replace-everything write can express.

    Unknown names are dropped. Returns the enabled set as it now stands.
    """
    wanted = {name for name in names if rule_by_name(name) is not None}
    now = time.time()
    with transaction(conn):
        conn.execute("DELETE FROM redaction_disabled")
        conn.executemany(
            "INSERT INTO redaction_disabled (name, disabled_at) VALUES (?, ?)",
            [(rule.name, now) for rule in RULES if rule.name not in wanted],
        )
    return frozenset(wanted)
