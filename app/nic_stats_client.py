"""Reading ethtool driver counters over the shared SSH connection.

Xen Orchestra has no route for this: its RRD stats cover throughput, not the
driver-level error, drop and CRC counters ``ethtool -S`` reports, and
``xen-bugtool``'s bundle is a snapshot of ``/var/log``, not live command
output. Reaching them means connecting to the host directly, over the
connection ``app.ssh_connection``/``app.ssh_client`` provide — this module is
only the one check built on top of that shared plumbing, not a connection of
its own.

The host-side allowlist entry for this check is named ``CHECK_NAME`` below,
and the exact dispatcher script line is in ``docs/configuration.md``; this
module's ``IFACE_MARKER`` must stay in lock-step with what that script
prints, since the marker is how ``parse_ethtool_stats`` tells one interface's
block from the next.
"""

from __future__ import annotations

import re

from app.ssh_client import run_check

# The check name sent as the SSH exec request. A correctly configured host's
# dispatcher script matches this against its own allowlist and runs only the
# corresponding fixed logic — see app/ssh_client.py's module docstring.
CHECK_NAME = "nic-stats"

# The marker the documented dispatcher script must print ahead of each
# interface's ethtool block, so a concatenated multi-interface run can be
# split back apart. Chosen to never collide with a real ethtool line, which
# is always "  counter_name: number".
IFACE_MARKER = "===IFACE:"

_MARKER_LINE = re.compile(rf"^{re.escape(IFACE_MARKER)}(?P<name>\S+)===\s*$")
_COUNTER_LINE = re.compile(r"^\s*([\w.-]+):\s*(-?\d+)\s*$")

# Counters read but never listed as an error/drop signal in
# ``findings.NIC_ERROR_COUNTERS`` still end up in the stored raw report — this
# is only the list of markers a truncated or malformed block can be told from
# a genuinely empty one, unrelated to which counters findings later acts on.
_EMPTY_BLOCK_MARKERS = ("nic statistics",)


def fetch_stats(
    *,
    host: str,
    port: int,
    private_key,
    known_host_key: tuple[str, bytes] | None,
    on_trust_new_host_key,
    username: str = "root",
) -> tuple[str, bool]:
    """Run the NIC statistics check on a host and return (output, trusted_new_key).

    Thin wrapper around ``app.ssh_client.run_check``: sends no arguments — the
    host's dispatcher script discovers which interfaces to loop over itself
    (every one with a real device behind it), so there is no operator-typed
    interface list to keep in sync with what hardware is actually there.
    """
    return run_check(
        host=host,
        port=port,
        private_key=private_key,
        known_host_key=known_host_key,
        on_trust_new_host_key=on_trust_new_host_key,
        check=CHECK_NAME,
        username=username,
    )


def parse_ethtool_stats(output: str) -> dict[str, dict[str, int]]:
    """Split marked ``ethtool -S`` blocks into ``{interface: {counter: value}}``.

    Every interface named by an ``IFACE_MARKER`` line is present in the
    result, even with an empty counter dict, so a host whose dispatcher
    script ran but whose ``ethtool`` failed for one interface is still
    visible as "read, nothing usable" rather than silently missing — the same
    read-versus-empty distinction ``findings.SourceResult`` already draws
    everywhere else in this application.
    """
    blocks: dict[str, dict[str, int]] = {}
    current: str | None = None
    for line in output.splitlines():
        marker = _MARKER_LINE.match(line)
        if marker:
            current = marker.group("name")
            blocks[current] = {}
            continue
        if current is None:
            continue
        counter = _COUNTER_LINE.match(line)
        if counter:
            blocks[current][counter.group(1)] = int(counter.group(2))
    return blocks


__all__ = [
    "CHECK_NAME",
    "IFACE_MARKER",
    "fetch_stats",
    "parse_ethtool_stats",
]
