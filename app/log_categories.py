"""Which log family each file inside a collected bundle belongs to.

``logs.tgz`` is ``xen-bugtool``'s whole ``/var/log`` — 609 files on a real host,
measured 2026-09-11 against ``xcp-ng-host1`` (a bundle downloaded once to list
its members, then deleted; nothing about that run is kept). Xen Orchestra's log
routes accept no category filter and no date range, so a request for "just the
storage logs" can only be answered by picking apart a bundle already on disk —
this module is the map that makes that possible.

Every entry below is a real path measured in that bundle, matched against its
**canonical name** — the member's path with a trailing rotation number and
``.gz`` stripped, so ``var/log/xensource.log.27.gz`` and
``var/log/xensource.log`` both match the same rule. A path that matches nothing
below falls into "system" as unclassified rather than being silently dropped —
an extraction must account for every file it read, never quietly drop one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Stripped from a member's path before matching: a trailing ``.N`` (rotation
# index) and/or ``.gz`` (rotated logs are gzipped once rotated, current ones
# are not). ``sa/sa01`` and similar sysstat files have no such suffix at all.
_ROTATION_SUFFIX = re.compile(r"(?:\.\d+)?(?:\.gz)?$")


@dataclass(frozen=True)
class Category:
    """One log family an operator can choose to extract."""

    key: str
    title: str
    # Prefixes matched against a member's canonical name (case-sensitive — the
    # bundle's own names are consistently lower-case). A trailing "/" matches
    # every file under that directory; anything else matches that exact name.
    prefixes: tuple[str, ...]


# Order is display order on the Collect and Jobs forms — grouped by how an
# operator thinks about a fault (storage, then kernel, then network) rather
# than alphabetically.
CATEGORIES: tuple[Category, ...] = (
    Category(
        "xapi",
        "XAPI",
        ("xensource.log", "xen/hypervisor.log"),
    ),
    Category(
        "storage",
        "Storage",
        (
            "SMlog",
            "blktap/",
            "raid-plugin.log",
            "smartctl-plugin.log",
            "lvm-plugin.log",
            "twinstor.log",
            "twinstor-fence-peer.log",
            "drbd-kern.log",
        ),
    ),
    Category("audit", "Audit", ("audit.log",)),
    Category(
        "security",
        "Security",
        ("secure", "btmp", "wtmp", "lastlog", "tallylog", "maillog"),
    ),
    Category(
        "kernel",
        "Kernel",
        ("kern.log", "boot.log", "installer/dmesg-log"),
    ),
    Category("ha", "High availability", ("xha.log",)),
    Category("xenstore", "Xenstore", ("xenstored-access.log",)),
    Category(
        "rrd",
        "RRD plugins",
        ("xcp-rrdd-plugins.log", "VMSSlog"),
    ),
    Category(
        "network",
        "Network",
        ("openvswitch/", "interface-rename.log"),
    ),
    # Deliberately last and deliberately wide: every path not claimed by a rule
    # above lands here rather than being dropped, including files not present
    # in the bundle this was measured against (older or newer XCP-ng releases
    # add and remove a few) and the installer's own miscellany.
    Category(
        "system",
        "System",
        (
            "daemon.log",
            "messages",
            "syslog",
            "cron",
            "user.log",
            "yum.log",
            "installer/",
            "sa/",
            "updater-plugin.log",
            "ipmitool-xapi-plugin-plugin.log",
            "grubby_prune_debug",
        ),
    ),
)

_BY_KEY: dict[str, Category] = {category.key: category for category in CATEGORIES}

# The catch-all a path that matches nothing else — including one from a
# category list a future XCP-ng release changes — is filed under. Kept as a
# constant rather than assuming "system" is last in CATEGORIES forever.
_FALLBACK_KEY = "system"


def category_keys() -> tuple[str, ...]:
    """Every valid category key, in display order."""
    return tuple(category.key for category in CATEGORIES)


def category_by_key(key: str) -> Category | None:
    return _BY_KEY.get(key)


def canonical_name(member_path: str) -> str:
    """A member's path with its rotation suffix stripped.

    ``var/log/xensource.log.27.gz`` and ``var/log/xensource.log`` both become
    ``xensource.log``, which is what every prefix in ``CATEGORIES`` is written
    against. The leading ``var/log/`` — present on every member in a measured
    bundle — is stripped too, since no category needs to distinguish it.
    """
    name = member_path[8:] if member_path.startswith("var/log/") else member_path
    return _ROTATION_SUFFIX.sub("", name)


def classify(member_path: str) -> str:
    """The category key a bundle member belongs to. Never returns an unknown key.

    Longest-prefix-first isn't needed: every prefix below is either an exact
    file name or a directory ending in ``/``, and no category's prefix is a
    prefix of another category's, so first match is the only match.
    """
    name = canonical_name(member_path)
    for category in CATEGORIES:
        for prefix in category.prefixes:
            if prefix.endswith("/"):
                if name.startswith(prefix):
                    return category.key
            elif name == prefix:
                return category.key
    return _FALLBACK_KEY
