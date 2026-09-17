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
from dataclasses import dataclass, field, replace

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
    pattern: re.Pattern[str] | None
    placeholder: str
    keep: frozenset[str] = field(default=frozenset())

    def apply(self, text: str) -> tuple[str, int]:
        """Mask every match in ``text``. Returns the result and the hit count.

        The count is what a redaction report is built from later, which is why
        it is produced here rather than by re-scanning the output — a rule
        whose placeholder its own pattern matches would count wrongly.

        ``pattern`` is ``None`` for a rule with no fixed shape of its own (see
        "username" below) — matches nothing until a caller supplies a real
        pattern via a rule built for the occasion.
        """
        if self.pattern is None:
            return text, 0

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
    Rule(
        name="username",
        title="Xen Orchestra usernames",
        description=(
            "Account names read live from the connected Xen Orchestra "
            "instance. Unlike every rule above, this has no fixed pattern of "
            "its own — see build_username_rule — so it matches nothing here, "
            "and nothing in the redaction preview page below, which has no "
            "connection to build a live list from."
        ),
        pattern=None,
        placeholder="[USER]",
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


def active_rules(
    enabled: frozenset[str] | set[str] | None = None,
    *,
    username_rule: Rule | None = None,
) -> tuple[Rule, ...]:
    """The rules to apply, in order. ``None`` means all of them.

    ``username_rule`` substitutes a live-data rule (see ``build_username_rule``)
    for the static, inert "username" placeholder in ``RULES``. Every existing
    caller omits it and gets the placeholder, which matches nothing.
    """
    rules = RULES if enabled is None else tuple(rule for rule in RULES if rule.name in enabled)
    if username_rule is not None:
        rules = tuple(username_rule if rule.name == "username" else rule for rule in rules)
    return rules


def build_username_rule(usernames: Iterable[str]) -> Rule:
    """The "username" rule with a pattern built from a live account list.

    A username has no fixed shape, unlike every other rule, so it can only be
    masked by knowing the real ones. Replaces the static entry's pattern
    (``dataclasses.replace``, keeping its name/title/placeholder) so hit
    counting and the on/off toggle both still key off "username" whether or
    not real data was ever supplied.

    Names are sorted longest first, so a short name is not matched inside a
    longer one that happens to contain it before the longer alternative is
    tried, and the pattern is ``\\b``-bounded and case-insensitive so "nick"
    does not eat "nickel". An empty or absent list returns the static entry
    unchanged — no usernames to mask is not an error.
    """
    base = rule_by_name("username")
    assert base is not None
    names = sorted(
        {name.strip() for name in usernames if name and name.strip()},
        key=len,
        reverse=True,
    )
    if not names:
        return base
    pattern = re.compile(r"(?i)\b(?:" + "|".join(re.escape(name) for name in names) + r")\b")
    return replace(base, pattern=pattern)


def redact_line(
    line: str,
    enabled: frozenset[str] | set[str] | None = None,
    *,
    username_rule: Rule | None = None,
) -> str:
    """Mask one line. The unit everything else is built from.

    A line rather than a whole file because the caller that matters most reads
    a 56 MB log a line at a time and must never hold it in memory.
    """
    for rule in active_rules(enabled, username_rule=username_rule):
        line, _ = rule.apply(line)
    return line


def redact_text(
    text: str,
    enabled: frozenset[str] | set[str] | None = None,
    *,
    username_rule: Rule | None = None,
) -> tuple[str, dict[str, int]]:
    """Mask a block of text. Returns the result and per-rule hit counts.

    Line endings are preserved exactly, because a redacted log that has had its
    CRLFs rewritten no longer matches the file it came from.
    """
    counts: dict[str, int] = {}
    rules = active_rules(enabled, username_rule=username_rule)
    out: list[str] = []

    for line in text.splitlines(keepends=True):
        for rule in rules:
            line, hits = rule.apply(line)
            if hits:
                counts[rule.name] = counts.get(rule.name, 0) + hits
        out.append(line)

    return "".join(out), counts


def redact_json(
    value: object,
    enabled: frozenset[str] | set[str] | None = None,
    *,
    username_rule: Rule | None = None,
) -> tuple[object, dict[str, int]]:
    """Mask every string leaf in a JSON-shaped value.

    Returns the masked value and per-rule hit counts, the same shape
    ``redact_text`` returns.

    Whole-document regex substitution — running ``redact_text`` on an already
    serialised JSON string — is unsafe here: a hit spanning or landing on a
    quote character could corrupt the document. Walking the *parsed* tree and
    masking only string leaves means a substitution only ever happens inside a
    value Python already knows is a string, so the surrounding structure can
    never become invalid.

    Dict keys are left alone — they are the API's own field names (``id``,
    ``jobId``, ``message``, ...) and masking one would make the document steer
    a reader wrong about which field they are looking at.
    """
    rules = active_rules(enabled, username_rule=username_rule)
    counts: dict[str, int] = {}

    def _walk(node: object) -> object:
        if isinstance(node, dict):
            return {key: _walk(item) for key, item in node.items()}
        if isinstance(node, list):
            return [_walk(item) for item in node]
        if isinstance(node, str):
            masked = node
            for rule in rules:
                masked, hits = rule.apply(masked)
                if hits:
                    counts[rule.name] = counts.get(rule.name, 0) + hits
            return masked
        return node

    return _walk(value), counts


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
