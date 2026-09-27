# Jobs

Everything that runs in the background, in one history, plus two actions
that don't belong on a more specific page:

- **Refresh inventory** — re-reads pools and hosts from Xen Orchestra for the
  dashboard. Runs automatically after the first connection is saved, on every
  login, before Collect or Findings need a host list, and roughly every 15
  minutes in the background — so a pool or host added or removed in Xen
  Orchestra shows up here on its own. Run it here directly for "I want it
  now."
- **Redact a stored file** — pick any stored file that hasn't been redacted
  yet and mask it, keeping a report of what was found.

The history below lists every job — collections, redactions, extractions,
findings runs, packages — with its state, progress while running, and what it
produced. A running job can be cancelled; a finished redaction can be
downloaded or deleted from here. Collections themselves are managed on
[Collect](collect.md), not here — that's where their size and which copy is
which are on screen.
