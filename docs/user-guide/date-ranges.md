# Date ranges

Wherever you see a date-range picker — [Collect](collect.md), category
extraction, [Findings](findings.md), [Support package](support-package.md) —
it narrows what gets **kept and reported**, not what gets downloaded: Xen
Orchestra's log export has no date filter of its own, so the first download
of a bundle is always the full thing. A range skips rotated log files
outside the window by their modification time, and filters a file
straddling the window edge by a best-effort per-line timestamp. Presets
cover the last 24 hours, 7 days, 30 days, and since the last reboot; a
custom start and end is also available.
