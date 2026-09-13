# Support package

Bundles everything a Vates support ticket needs into one archive: the
redacted log bundle, findings in both Markdown and JSON, the redaction
report, the inventory, and a manifest listing what's inside and what was
masked. Building one always runs a fresh findings check and inventory
refresh alongside it, so the package is never missing a piece.

Two ways to build one:

- **Collect from a host and package it** — for a host with nothing collected
  yet. Downloads its logs, redacts a copy, and builds the package in one
  action.
- **Package an existing collection** — for a host you've already collected.
  Skips the download and builds straight from what's stored.

A package built from a collection that has since been deleted still works —
the archive doesn't need the original collection to exist — and appears
under its own **Built from a since-deleted collection** section.
