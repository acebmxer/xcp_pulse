# Collect

Downloads a host's full log bundle — the same status report `xen-bugtool`
produces, which is what Vates support ask for. A real bundle runs to a few
hundred megabytes and takes roughly two minutes.

1. Pick a host from the list. If none appear, run **Refresh inventory** on
   the [Jobs](jobs.md) page first.
2. Optionally tick **Also download the XAPI audit trail** — a separate file,
   on top of the audit log already inside the bundle, mostly the toolstack
   calling itself rather than a record of user actions.
3. **Redact using the rules switched on in Redaction** is ticked by default —
   leave it on unless you specifically want the raw bundle only (for example,
   to change a redaction rule before masking). Untick it and you redact later
   from the [Jobs](jobs.md) page or with the **Redact now** button that
   appears on an unredacted collection here.
4. Optionally set a date range (see [Date ranges](date-ranges.md)) or expand
   **Also extract specific log categories after collecting** to pull
   specific log families into a second, smaller archive in the same run.
5. Press **Collect**. Progress and an ETA show while it runs; the page
   refreshes itself until it finishes.

Each stored collection lists its files with a size and a **Download** link.
**Which file is which matters**: a raw bundle is tagged **raw — unmasked**
and contains addresses, session tokens and credentials; only the copy tagged
**redacted — safe to send** should leave the machine. If a collection shows
"Not redacted yet," it was stored raw on purpose — press **Redact now** once
you're ready.

From a stored collection you can also extract specific categories afterwards
(no second download — it reads the bundle already on disk) or delete the
collection and its files.

**Retention**, at the bottom of the page, previews what a cleanup would
delete before it happens: set how many recent collections to always keep and
how old is too old, and the preview lists exactly what would be freed. Ticking
"Preview these limits" only recalculates the preview; nothing is deleted
until you press **Delete these now**.

## Collect XO diagnostics

A second, independent card further up the page. It reads raw detail from the
Xen Orchestra API that [Findings](findings.md) throws away: the full
per-VM/per-disk task tree behind every backup and restore run in the window,
plus XAPI tasks, messages and alarms — not one host's logs but the whole
instance, so there is no host to pick here.

1. Tick which of the three sources to read.
2. Optionally set a date range (see [Date ranges](date-ranges.md)).
3. Press **Collect diagnostics**. Downloads nothing from a host, but a busy
   pool can still take a few seconds — detail is fetched for every run in the
   window, not just failed ones.

Each ticked source stores a raw and a redacted copy, masked with the rules
switched on in [Redaction](redaction.md), including account names read live
from Xen Orchestra. A stored run's files and its per-rule masking report
appear under **Stored diagnostics**, the same way a host collection's do —
**which file is which matters**: only the copy tagged **redacted — safe to
send** should leave the machine.

Pools reached through an XO Proxy have not been verified — see the note on
the card.
