# Support package

Bundles everything a Vates support ticket needs into one archive: the
redacted log bundle, findings in both Markdown and JSON, the redaction
report, the inventory, and a manifest listing what's inside and what was
masked. Building one always runs a fresh findings check and inventory
refresh alongside it, so the package is never missing a piece. A NIC
statistics report is included too, when one has already been run for this
host from the Findings page.

Two ways to build one:

- **Collect from a host and package it** — for hosts with nothing collected
  yet. Tick one or more; each downloads its own logs and redacts a copy.
  Since a support ticket is usually about a pool-wide incident rather than
  one host on its own, ticking more than one host lets you choose **one
  package per host** or **one combined package** covering all of them — the
  combined archive holds every host's own redacted bundle, redaction report,
  and NIC statistics report (when it has one), alongside one shared findings
  report and inventory. A combined package appears in its own **Multi-host
  packages** section rather than nested under any single host's card.
- **Package an existing collection** — for a host you've already collected.
  Skips the download and builds straight from what's stored.

A package built from a collection that has since been deleted still works —
the archive doesn't need the original collection to exist — and appears
under its own **Built from a since-deleted collection** section.
