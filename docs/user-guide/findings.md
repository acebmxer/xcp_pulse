# Findings

Two independent checks for problems, run on demand:

- **Read findings from Xen Orchestra** — asks the API about failed tasks,
  alarms, XAPI messages, missing patches, backup and restore results, and the
  pool dashboard. Downloads nothing and answers in a second or two.
- **Findings from collected logs** — analyzes an already-stored log bundle
  locally for storage failures, multipath changes, XAPI exceptions, HA
  fencing and heartbeat failures, out-of-memory events and clock sync
  failures. Also downloads nothing — it reads the bundle already on disk.

Each finding shows its severity, the evidence behind it (redacted using the
rules switched on in [Redaction](redaction.md)), and what to do about it. A
finding spotted by both checks — matched by condition and, when both are
timed, within an hour of each other — is marked as confirmed by the other,
so one real incident doesn't read as two unrelated findings.

Results can be downloaded as JSON or Markdown; the Markdown copy is the one
to paste into a support ticket. If any rules were switched off when a report
ran, the page says so plainly rather than letting an unmasked value and a
value no rule looked for read identically. The **Sources** table at the
bottom lists what was read (or why something couldn't be) and whether it
comes from Xen Orchestra itself or is relayed from a host.
