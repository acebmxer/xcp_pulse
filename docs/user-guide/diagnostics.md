# Diagnostics

Reads raw detail from the Xen Orchestra API that
[Findings](findings.md) throws away: the full per-VM/per-disk task tree
behind a failed backup or restore run, plus that window's XAPI tasks,
messages and alarms. Downloads nothing from a host — every source is an API
call — but fetching detail for a failed run is its own request, so a busy
pool can take a few seconds rather than one.

Detail is fetched only for a backup or restore run whose summary already
shows a failure — the case a support ticket exists to explain. A run whose
detail could not be read still shows its summary, with the reason in its
place.

Values are masked with the rules switched on in [Redaction](redaction.md),
including account names read live from Xen Orchestra — the one rule with no
fixed pattern of its own. If any rules were switched off when a run happened,
the page says so plainly rather than letting an unmasked value and a value no
rule looked for read identically.

Results can be downloaded as JSON or Markdown; the Markdown copy is the one
to paste into a support ticket. The **Sources** table lists what was read, how
much, and for backups and restores, how many had full detail fetched.
