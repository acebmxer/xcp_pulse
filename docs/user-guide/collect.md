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
