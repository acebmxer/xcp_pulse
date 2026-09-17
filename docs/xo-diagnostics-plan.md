<!--
Working note, copied verbatim from the actual plan-mode file at
/home/nick/.claude/plans/xo-diagnostics-plan.md (370 lines), which is where
this plan actually lived — not in this repo, which is why earlier sessions
searching the repo, git history, and Claude Code's project memory folder
could not find it. Delete this file once the whole 5-stage feature has
shipped and this stops being useful to plan against.

A "Status" note is appended at the bottom, after the original plan text,
recording what has actually been built so far and where it diverges from
this plan.
-->

# Collect and surface XO API diagnostics (backups, migrations, boot, and beyond)

## Context

xcp_pulse today collects one thing per host: a `logs.tgz` bundle (xen-bugtool)
plus optional `audit.txt`, via XO's REST API. That's the file Vates asks for,
but it only covers what's local to one host. Nick's actual goal, stated
directly: since XO CE carries no Pro support, a user troubleshooting anything
— backups, migrations, boot failures, storage, networking — is on their own
with community forums and whatever they can hand Vates. This project should
close that gap as one complete feature, not a bolt-on: both **archiving** the
richer diagnostic data XO's API exposes (for handing to Vates) and **using**
it to make the existing Findings feature name the actual cause of a failure —
e.g. which step of a backup broke and why, not just "backup failed, go look in
XO yourself" (which is literally what today's finding text tells the user to
do — see Stage 3). This is confirmed to be a genuinely bigger scope than a
single pass, so it is broken into stages below, each a complete, testable
chunk, built and verified one at a time rather than all at once.

## Decisions already made (do not re-litigate)

- **REST API only.** No SSH/appliance-log access to the XO VM itself — that
  stays a separate, unbuilt idea.
- **Both raw archive AND Findings integration.** New downloadable JSON
  artifacts (same category as `logs.tgz`) *and* the same data feeding richer,
  more specific entries in the existing Findings feature — not raw-only, and
  not findings-only. Confirmed: the point is for this to feel like one
  complete part of the product, not a separate bolted-on feature.
- **Three sources, each instance-wide, no pool/host tie** (verified live
  against XO's own API schema — backup-logs and XAPI tasks carry no pool field
  at all, not even via the pool master; messages/alarms do carry `$pool` but
  per Nick's explicit call, are also pulled instance-wide with no pool
  filter, for consistency):
  1. Full backup/restore job detail trees (`/backup-logs/{id}`,
     `/restore-logs/{id}` — confirmed live to return the full nested per-VM/
     per-disk task tree; a different, hyphenated route from the flat-summary
     `/backup/logs` the project already calls).
  2. XAPI task records (`XoClient.tasks(since)` — already exists, already
     instance-wide).
  3. Messages & alarms (`XoClient.messages(since)`/`alarms(since)` — already
     exist, already instance-wide; no per-object-type calls needed since XO
     tags every message/alarm with which object it's about).
- **No pool/host picker for the archive feature.** All three sources ignore
  pool/host entirely (see above), so picking a host wouldn't change what's
  pulled. It lives as its **own separate card** on the existing Collect page
  — not nested in the per-host form, not a new page/nav item.
- **New redaction rule for XO usernames**, since a bare username (e.g. `nick`)
  has no fixed regex shape — it can only be masked by knowing the live account
  list (`/rest/v0/users`). Applies to the new diagnostics artifacts only in
  this pass; extending it to the existing log-bundle redaction path is a
  follow-on decision, not built here.
- **Support package page gets matching checkboxes too**, to pull fresh
  diagnostics while building a package, mirroring the existing `include_audit`
  checkbox there.
- **Findings gets more specific, not just more data.** Confirmed: a failed
  backup should say which step failed and why (not just "failed"); the same
  precision is wanted for other object types (a VIF, a VDI, etc.) wherever XO
  actually reports enough detail to say something specific. This is Stage 3/4
  below — real design work in `app/findings.py`, not a side effect of Stage 2.
- **Excluded from scope:** the XO audit plugin (`/plugins/audit/records`) and
  full per-object config dumps (every VDI/VBD/VIF/etc.'s own record — 296
  objects on Nick's own pool). Neither was requested.
- **XO Proxies (remote pools managed via XOA/XO through a relay VM) —
  unverified, shipped on a documented assumption.** Checked live: this XO
  instance has zero proxies configured (`/rest/v0/proxies` → `[]`), and the
  `XoProxy` schema (`id, url, version, name, address, vmUuid`) has no field
  linking a proxy to the pool(s) it serves — there's no way to test or even
  statically confirm proxy behavior against this instance today. The design
  needs no proxy-aware code either way, since every source is already pulled
  instance-wide with no pool filtering — the expectation is that XO records
  backups/tasks/messages centrally for every pool it manages, proxied or not,
  so proxied pools' data should already be swept up automatically. That
  expectation is **not verified**. State this plainly in the on-screen note/
  report text and confirm it for real the first time this runs against an
  instance that has a proxy configured.

## Forward-compatibility note: future MCP support (not built now)

Nick wants to be able to add MCP support later (letting a local AI query
findings/diagnostics), without this plan being designed narrowly enough that
doing so means reworking it. **No MCP server work is in scope here** — this is
a design constraint on stages 1–5, not a stage of its own:

- Keep the reader functions (`diagnostics_artifacts_from_job`,
  `report_from_job`, and the existing `findings` readers) as plain functions
  of `(conn, job_id)` / `(conn, data_dir, job_id)` — not entangled with
  FastAPI request/response objects — so a future MCP tool handler could call
  the same function a route calls today, rather than needing new plumbing.
- Keep the new JSON artifact shapes (the diagnostics report, the per-source
  payloads) intentionally stable and documented (`docs/functions.md`), since
  anything consuming them externally later would depend on that shape not
  drifting silently.
- No other change needed for this now — just don't paint this into a corner.

## Stages — build and verify one at a time

This ships as one feature, but each stage below is a complete, independently
testable chunk. **Stop after each stage for Nick to review/test before
starting the next one** — do not build straight through all five.

---

### Stage 1 — Foundation: new API methods + redaction infrastructure — ✅ DONE (commit `1a0f9d6`, matches this spec)

No user-visible change. Everything later stages depend on.

**`app/xo_client.py`:**
- New constants: `BACKUP_LOG_DETAIL_PATH = "/backup-logs/{id}"`,
  `RESTORE_LOG_DETAIL_PATH = "/restore-logs/{id}"` (hyphenated — deliberately
  distinct from the existing `BACKUP_LOGS_PATH = "/backup/logs"`), plus
  `USERS_PATH = "/users"`, `USER_FIELDS = "id,email,permission"`.
- New private helper `_single_object(path, *, not_found=None) -> dict`,
  factoring out the status/JSON/shape checks `pool_dashboard()` already does
  inline. Does **not** touch `pool_dashboard` itself — leave its specific
  error handling alone.
- `backup_log_detail(log_id) -> dict`, `restore_log_detail(log_id) -> dict` —
  single-object GETs via `_single_object`, following `pool_dashboard()`'s
  pattern rather than the collection-fetch `_events`/`_record_list` helper
  (these are single-record fetches by id, not filtered lists).
- `users() -> list[dict]` — GET `/users`, via the existing
  `_record_list`/`_raise_for_collection` path like every other collection
  read. Feeds the username redaction rule below. **Verify live** whether
  `email` is the bare login name (e.g. `nick`) as expected, or a real address
  the existing `email` rule already catches.

**`app/redact.py`:**
- Widen `Rule.pattern` to `re.Pattern[str] | None`; `Rule.apply` gets one new
  line: `if self.pattern is None: return text, 0`.
- Add a static `"username"` entry to `RULES` with `pattern=None` — an inert
  placeholder that makes it show up automatically in the on/off toggle list,
  the report's rule rows, and `enabled_rules`/`set_enabled_rules` bookkeeping
  (all name-keyed, unaffected by a null pattern), with a description noting it
  needs a live account pull and matches nothing in the redaction preview page.
- `build_username_rule(usernames: Iterable[str]) -> Rule` — `dataclasses.replace`
  on the static entry with a compiled alternation (longest-first, `\b`-bounded,
  case-insensitive) when names are given; returns the inert entry unchanged
  when the list is empty.
- Thread it through as a new optional keyword, defaulting to `None`
  everywhere: `active_rules(..., username_rule=None)`, and the same kwarg on
  `redact_line`/`redact_text`. Every existing caller is unaffected.
- `redact_json(value, enabled=None, *, username_rule=None) -> tuple[object, dict[str, int]]`
  — walks a parsed JSON value recursively, masking only **string leaf
  values** (never dict keys — they're XO's own field names), via the same
  `active_rules`/`Rule.apply` every other redaction path uses. This is the one
  genuinely new piece of infrastructure needed: whole-file/line-based
  redaction (the existing mechanism) is unsafe to run against a serialized
  JSON string, since a regex substitution could in principle land on or span
  a quote character and corrupt the document; walking the *parsed* tree and
  only replacing inside values Python already knows are strings avoids that
  entirely.

**Explicitly out of scope this pass:** wiring live username masking into the
existing log-bundle redaction path (`job_collect`, `job_redact`, `job_extract`
don't build an `XoClient` today). Flagged as a follow-on decision.

**Tests:** `tests/test_xo_client.py` (new methods against 200/404/401/403 via
the existing `MockTransport` helper), `tests/test_redact.py`
(`build_username_rule` empty/populated, word-boundary correctness;
`redact_json` on nested structures — keys untouched, leaves masked, and a
string containing a literal `"` or `\` still round-trips through
`json.dumps`/`json.loads` cleanly after masking).

---

### Stage 2 — Raw diagnostics collection (Collect page card) — ⚠️ BUILT WRONG (see Status at bottom of file: different shape shipped as commit `f3c229b`, does not match this spec)

Independently useful and testable on its own: download the raw and redacted
JSON for all three sources and confirm they're valid, complete, and masked
correctly.

**New module `app/job_diagnostics.py`** (mirrors `job_collect.py`'s doc-comment
style; structurally mirrors `findings.collect_findings`'s "try each source
independently, record read/refused/empty" loop, without importing from
`findings.py` — this module's output is raw archived JSON, not Findings):

```python
KIND = "collect_diagnostics"
SOURCE_BACKUP_RESTORE = "backup_restore"
SOURCE_TASKS = "tasks"
SOURCE_MESSAGES_ALARMS = "messages_alarms"
SOURCES = (SOURCE_BACKUP_RESTORE, SOURCE_TASKS, SOURCE_MESSAGES_ALARMS)
BACKUP_RESTORE_ARTIFACT = "backup-restore-detail.json"
TASKS_ARTIFACT = "xapi-tasks.json"
MESSAGES_ALARMS_ARTIFACT = "messages-alarms.json"
REPORT_ARTIFACT = "diagnostics-report.json"
```

`run(context)`: params are just `sources: list[str]` and
`date_preset`/`date_start`/`date_end` (same convention `job_extract` already
uses). No `pool_id`, no chaining to any other job — fully standalone. For each
ticked source, call the relevant `XoClient` method(s), catching `XoError`
per-source without failing the whole run (mirrors `findings.py`'s posture);
`store_json` the raw payload, run it through `redact_json`, `store_json` the
redacted copy via the existing `redacted_name()` helper
(`app/job_redact.py`). The backup/restore source enumerates ids via the
existing `backup_logs(since)`/`restore_logs(since)`, filters locally by the
range's end, then calls the new `backup_log_detail`/`restore_log_detail` per
id, catching a per-id failure without losing the rest of the batch. Build the
username rule once via `client.users()` → `build_username_rule(...)`,
degrading to the inert rule (not a failed job) if that call raises.

Reader functions following the `FINDINGS_ARTIFACT`/`X_from_job` convention:
`diagnostics_artifacts_from_job(conn, job_id)` (redacted copies only — same
"only the redacted copy ever leaves the box" rule `job_support_package`
already applies to the log bundle) and `report_from_job(conn, data_dir, job_id)`.
Register at the bottom: `register(KIND, run)`; import this module wherever
`job_collect`, `job_extract`, etc. are already imported at startup.

**`app/routes/collect.py` + `app/templates/collect.html`:** a new, independent
card ("Collect XO diagnostics") on the Collect page, same visual pattern as
the existing "Collect from a host" card, sitting alongside it — not inside the
per-host `<form>`. Three checkboxes (one per source) plus the existing
`_date_range.html` picker (using its `prefix` argument so it doesn't collide
with the per-host form's own date range), all wired via `form="diagnostics-form"`.
On-screen note states plainly: none of this is host- or pool-scoped, and
coverage of pools reached through an XO Proxy is unverified. New route
`POST /collect/diagnostics` (validates `sources` against `job_diagnostics.SOURCES`,
refuses an empty selection inline, enqueues the job, gated by the same
`has_active`-style concurrency check other kinds already get) and
`POST /collect/diagnostics/{job_id}/delete` (via `retention.delete_job`, same
generic manual-delete path extractions/redactions already use — no automatic
retention sweep needed for small JSON artifacts, same posture as those two
kinds today). `collect_page`'s data loading gains a `list_jobs(kind=job_diagnostics.KIND)`
call merged into the existing artifacts/reports maps, and its own
"Stored diagnostics" `.joblist` section, reusing the existing report-table
partial and artifact-list rendering — no new template widgets needed.

**Tests:** `tests/test_job_diagnostics.py` (new — all-sources-succeed stores 6
artifacts + 1 report; one source raising `XoError` still completes with that
source marked unread; `client.users()` raising still succeeds with masking
inert), `tests/test_collect_page.py` (new route enqueues correctly; empty
selection refused inline).

---

### Stage 3 — Findings: name the actual cause of a backup/restore failure — ✅ DONE (see Status at bottom of file)

Today's code already admits the gap: `findings.py`'s `_failed_runs` (used by
both `_from_backups`/`_from_restores`) builds its `action` text as *"Open the
backup job in Xen Orchestra and read the failed run's tasks — the failing step
names the VM or the remote"* — i.e., it tells the user to go look this up
manually. This stage makes XCP Pulse do that lookup and put the answer
directly in the finding.

**Design:** extend the per-job grouping in `_failed_runs` to also track the
specific record `id` of the most recent failing run (not just its
`at`/`count`/`status`). After grouping, for each job's latest failure, call
the new `client.backup_log_detail(id)` / `client.restore_log_detail(id)` and
walk its nested `tasks` array with a new helper —
`_locate_backup_failure(detail: dict) -> str | None` — to find the first (or
deepest) node with `status in ("failure", "interrupted")`, and build a
specific description from its `message` (e.g. `"transfer"`, `"snapshot"`,
`"merge"`) plus the enclosing VM task's `data.name_label` and the failing
node's own `result`/error text. Fold that into the finding's `evidence`
(replacing or supplementing the current count-only text) and simplify the
`action` text now that the finding itself names the step — it no longer needs
to tell the user to go find it in XO.

Catch `XoError` on the per-job detail fetch without failing the whole findings
run (one job's detail being unreachable doesn't lose every other finding) and
degrade to today's existing count-only evidence for that job.

**Flagged, needs verification before/during implementation:** every detail
tree captured live so far (Stage 1/2's own testing) has been from a
*successful* run — there is no confirmed real example of a *failed* tree's
exact shape (does a failing node's `result` carry a `message` string, a
nested error object, both?). Verify against a real failure if one exists in
the test pool's history, or construct a synthesized fixture from XO's own
source/documentation for the failure shape, and confirm it before writing the
final parsing logic — don't guess the shape.

**Tests:** `tests/test_findings.py` — a synthetic failed detail tree fixture
walked correctly by `_locate_backup_failure`; `_from_backups`/`_from_restores`
producing a finding with the specific step/VM in its evidence; the per-job
detail-fetch failure path degrading gracefully.

**Status (added 2026-09-17): partly built.** `_failed_runs` now tracks each
job's latest failing run's id, fetches its detail
(`backup_log_detail`/`restore_log_detail`), and folds the cause into the
finding's evidence via a new public `findings.backup_failure_message(detail)`
— reading `detail.result.message`, confirmed against a real failure
(disconnecting the backup remote: top-level `message` was just `"backup"`,
the real reason `"couldn't instantiate any remote"` was under `result`, and
`tasks` was empty — a whole-job failure never reaches a per-VM task at all).
`XoError` on the fetch, or no recognisable shape, degrades to the original
count-only evidence.

**Per-VM failure localization: built and confirmed against real data
(2026-09-17).** During an actual host outage, job "Delta Backup" produced a
run with 7 failing VMs — confirmed live via the REST API directly
(`GET /backup-logs/{id}`), not synthesized. That run's top level has no
`result` at all (only a whole-job failure puts it there); each VM has its own
task under `tasks`, tagged `data: {type: "VM", name_label: ...}`. Two distinct
shapes confirmed in the same run: 6 of 7 VMs had the VM task itself as the
deepest failure (its clean-vm/snapshot/export children all succeeded; the
error was a XAPI call the VM task made directly — `VDI.get_nbd_info` against
the offline host, cascaded `HOST_OFFLINE`), and 1 VM ("Beacon_PXE") had its
"snapshot" child task fail instead with a distinct real reason
(`SR_BACKEND_FAILURE_82`, "failed to pause VDI") while the VM's own status
cascaded up to "failure" too with nothing informative of its own. New
`findings._locate_backup_failure` walks the tree, via new `_deepest_failure`
preferring a failing child over its failing parent, and names both the VM and
the specific step/reason. Wired into `_detail_failure_cause` as a fallback
after `backup_failure_message` (whole-job case) finds nothing. Returns the
first failing VM found, not necessarily the most specific one when several
failed in the same run — naming one specific step and VM is already strictly
better than the bare count.

**Additional scope for this stage — successful-but-degraded run: built and
confirmed against real data (2026-09-17).** Disabling NBD on the pool's
network connection while leaving NBD enabled in the backup job forces every
VM in that job to silently fall back from a delta to a full backup, with
nothing reporting a failure anywhere — XO records the run as a plain
`success`. This is invisible to `_failed_runs`, which only ever looks at
failed runs. Confirmed live (job "Delta Backup", run id `1789612969276`, 7
VMs — re-verified directly against the same live run while building this):
each affected VM's task in `backup_log_detail`'s `tasks` array carries
a `warnings` list with three related lines — `"can't compute delta
OpaqueRef:... from OpaqueRef:..., fall back to a full"` (present tense, names
no cause, carries internal object references), `"can't connect through NBD,
fall back to stream export"` (names the actual cause — note this line does
**not** contain "to a full", so a naive "fall back to a full" pattern misses
it), and `"Backup fell back to a full"` (past tense, the clean summary). A
normal, non-degraded run's tasks carry no `warnings` at all (`warnings:
None`) — confirmed on the same job's earlier runs.

**Revised 2026-09-17, same day, after direct feedback:** the first version of
`findings._degraded_backups` only checked each job's *latest* run, skipping
the check entirely once that run's status was anything but `success`. Nick
caught this directly — a 24-hour window containing both an earlier degraded
run and a later outright failure only reported the failure, because the
degraded run was inside the chosen window and simply never looked at. Fixed:
it now checks every `success` run in the window (not only the latest), and
groups by job the same way `_failed_runs` already does — one finding per job,
counting every degraded run in range, evidenced by the most recent. Re-verified
live: a 1-day window against the real pool now reports both
"Backup job silently fell back to full: Delta Backup" (3 runs degraded) and
"Backup job failed: Delta Backup" (5 runs failed) side by side. Fixing this
also surfaced a real detection bug: the "fall back" substring match missed a
VM whose only warning was the past-tense "Backup fell back to a full" summary
line (which reads "fell back", not "fall back") — `_fallback_vms` now matches
both tenses.

New `findings._degraded_backups` scans each `success` run in the window and
calls new `findings._fallback_vms`, which scans each VM task's `warnings` for
a "fall back"/"fell back" substring and returns the affected VM names with the
NBD-specific line preferred over the generic/verbose one. The finding's action
text says to check NBD is enabled both on the pool's network connection and in
the job, since the two are set independently. Re-run directly against
`_fallback_vms(detail)` for the real degraded run: all 7 VMs detected, each
with the NBD-specific line selected. Also verified against today's live data
that a currently-*failing* latest run for the same job still correctly
produces only `_failed_runs`' finding, not a second one from this
path.

**Revised again 2026-09-17, same day, after further direct feedback:** with
the fix above, the evidence for a job with multiple degraded runs still kept
only the *most recent* run's cause once counted, discarding the rest. Nick
caught this too, from a real window with three degraded runs: an older run
that genuinely was an NBD outage (7 VMs, the real named cause) was completely
invisible in the evidence, buried behind a single VM's later, generic
"Backup fell back to a full" line, which happened to be newer. Fixed:
`_degraded_backups` now groups by job *and* by distinct cause — every
distinct (VM, cause) combination seen in the window gets its own evidence
line — with identical repeats across separate runs collapsing into one line
with a run count.

An intermediate version of that fix ordered the lines by how many VMs each
cause affected (biggest first), which put the older 7-VM outage ahead of
runs that happened later — Nick caught that too, immediately and furiously:
order has to be by when it actually happened, full stop, not by any measure
of severity. Fixed to order by recency instead. Re-verified live: the finding
read `XO-CE: Backup fell back to a full (2 run(s))` first, then the 7-VM NBD
outage — correct order, but Nick immediately flagged a deeper problem behind
it, backed by the two runs' actual raw JSON: bundling both causes into one
job-level finding meant that finding's own `at` (and its displayed "N hours
ago" age) was always the *most recent* cause's time, silently hiding that the
NBD outage's real date was a full day earlier than the card made it look —
correct line order inside the card did not fix a wrong date on the card
itself.

**Fixed properly, same day:** `_degraded_backups` no longer bundles causes as
lines inside one finding at all. Every distinct (job, cause) combination is
now its own finding, with its own real timestamp — the same way
`_classify_events` already treats the same message name recurring on two
different objects as two separate findings rather than one. This needs no
ordering logic of its own: each finding falls into the report's overall
chronological position via the existing `sort_findings` call, interleaved
correctly with every other finding type, which is what "just like every
other thing" actually required all along. Identical (VM, cause) repeats
across separate runs still collapse into one finding with a run count.
Verified directly against the two runs' actual downloaded JSON (not just the
live API pull): the NBD outage is its own finding dated 2026-09-17 03:07 UTC
(its real time), separate from the `XO-CE: Backup fell back to a full (2
run(s))` finding dated 2026-09-17 17:32 UTC — and a full live report shows
both correctly interleaved by actual time among every "Task failed" finding
around them, exactly matching Xen Orchestra's own backup-log history table.

While checking for the same mistake elsewhere per Nick's explicit "not just
findings, logs too, everything" instruction: found and fixed a real,
pre-existing instance of the identical class of bug in
`collect_log_findings` (unrelated to this stage — it is the log-bundle
findings path) — it kept whichever matching line the tar archive happened to
yield *last* as evidence, not the line with the latest actual timestamp. A
bundle's members are not read in chronological order (a rotated
`xensource.log.2.gz` can land before or after the current `xensource.log`),
so that was silently wrong the same way. Now parses each line's own
timestamp via `log_dates.parse_log_timestamp` and only replaces the kept
evidence with a line whose parsed time is later.

**One more real bug found the same day, this time a correctness bug, not an
ordering one:** Nick pointed out directly that not every fallback is an NBD
problem — the action text was unconditionally telling every operator to
"check NBD is enabled," even for a fallback whose own warnings never named
NBD at all. Re-checking every real Delta Backup run on the live pool over 30
days (35 runs, all manually cross-checked against raw JSON, not just the
function's output) confirmed it: one VM ("XO-CE") had silently fallen back to
full three separate times (2026-09-15, twice on 2026-09-16/17) with no stated
cause each time — no `"can't connect through NBD"` line, just the generic
`"can't compute delta ..."`/`"Backup fell back to a full"` lines — entirely
unrelated to the one confirmed NBD outage on the same job. `_degraded_backups`
now only gives the NBD-specific action when the kept message actually
contains "NBD"; otherwise it says Xen Orchestra gave no reason, with **no
mention of NBD at all** — Nick caught, immediately, that the first version of
this fix still said "this is not necessarily an NBD problem" for the
generic case, which is technically true but brings up a cause the log never
named; not mentioning NBD one way or the other is the only correct behaviour
when the log doesn't say it. This also surfaced a real, previously invisible
finding on Nick's own pool: XO-CE has a recurring, unexplained delta-backup
fallback that has nothing to do with the NBD outage and is worth his own
investigation separately.

**The most serious catch of the day, same fix session:** the generic-cause
action text — even after removing the NBD mention — still read "open the run
in Xen Orchestra to see why a delta could not be computed." Nick called this
out furiously and correctly: that is *exactly* the "backup failed, go look in
XO yourself" gap named in this plan's own Context section as the reason
Stage 3 exists at all, reproduced by the fallback path of the very feature
built to close it. Fixed properly: when no cause is named,
`_degraded_backups` now recommends the standard real-world fix (trigger a
manual full backup to reset the delta chain) instead of sending the operator
away to investigate. The same exact anti-pattern was found, in the same pass,
in the sibling `_failed_runs` function's own fallback (used when a run's
detail cannot be read at all) — it said "open the {job} job in Xen Orchestra
and read the failed run's tasks," the literal original wording quoted in this
plan's Context section. Fixed the same way: it now recommends collecting XO
diagnostics — this project's own tool, built in Stage 2, for archiving the
full task tree — instead of Xen Orchestra's UI.

**Also confirmed real, same session, unrelated to any of the above:** two
CRITICAL findings ("A host was fenced by high availability" /
"High availability fenced a host") appeared at the top of a live report,
newer than every backup-related finding. Nick asked where they came from —
verified directly against Xen Orchestra's own message log
(`GET /rest/v0/messages`): real `HA_HOST_FAILED`/`HA_HOST_WAS_FENCED`
messages on host `xcp-ng-host3`, timestamped minutes before the check. A
genuine new problem on the live pool, not a bug in this feature.

Stage 3 is now fully built.

---

### Stage 4 — Findings: broader object-type coverage (VIF, VDI, network, etc.)

`MESSAGE_RULES` (`app/findings.py`) already has precedent for object-type-
specific entries beyond VM/host/pool lifecycle events — `SR_BACKEND_FAILURE`,
`SR_DISK_SPACE_LOW`, and `VDI_CBT_METADATA_INCONSISTENT` are already there.
There is currently nothing for VIF/PIF/network-type failures specifically.

**This stage starts with research, not code.** `MESSAGE_RULES`'s existing
entries were each "measured on one pool" per the module's own comment — real,
observed XAPI message names, not invented ones. XO's REST schema doesn't
enumerate the possible message names (`XoMessage.name` is a free-form
string), so before adding VIF/PIF/network entries: check XAPI's own published
message-type source/documentation for real message names in that family (the
same way the existing table's entries are traceable to real XAPI behavior),
or capture one live if the opportunity arises. **Do not hardcode guessed
message-name strings** — an entry that never matches anything real is worse
than no entry, because it looks like coverage that doesn't exist.

Once real names are identified: add entries to `MESSAGE_RULES` (or
`MESSAGE_PREFIX_RULES` for a family of related names) following the exact
existing pattern, and extend `_MESSAGE_NAME_FAMILIES` for cross-source
correlation where a clean counterpart exists on the log side.

**Tests:** one test per new rule entry, following the existing
`test_findings.py` pattern for `MESSAGE_RULES` entries.

---

### Stage 5 — Support package integration + full docs pass

**`app/job_support_package.py`:** new optional param `diagnostics_job_id`,
resolved the same way `extract_job_id`/`redact_job_id` already are. When
present: pull `diagnostics_artifacts_from_job`/`report_from_job`, extend
`stored_entries` (currently a fixed 4-tuple literal — the actual code change,
since there's no generic "include everything" path today) with the
diagnostics artifacts, and add a `diagnostics` section to `build_manifest`'s
output. Fully additive: a package built without `diagnostics_job_id` produces
byte-identical output to today.

**`app/routes/support_package.py` + `support_package.html`:** the same three
checkboxes as Stage 2's Collect card (mirroring the existing `include_audit`
checkbox) so a package can trigger a fresh diagnostics pull directly; reuse an
already-successful `collect_diagnostics` job if one exists rather than
re-pulling (same posture `existing_redaction` already embodies elsewhere in
this file).

**Docs (same pass, not a follow-up):**
- `README.md` — "What it does" gains a line for instance-wide XO API
  diagnostics and the richer Findings evidence; note the new artifacts are
  small JSON, not multi-hundred-MB, so existing storage estimates are
  unaffected.
- `docs/architecture.md` — new row in `## Modules` for `app/job_diagnostics.py`;
  a short paragraph under `## Redaction` on the username rule's `pattern=None`
  placeholder shape.
- `docs/functions.md` — a row (Since: unreleased) for every new public
  function across all five stages. Enforced by `tests/test_function_index.py`
  — run it last as the checklist for missed rows.
- `CHANGELOG.md` — under `[Unreleased]` / `### Added` (new capability, not a
  fix).

**Tests:** `tests/test_job_support_package.py` (diagnostics inclusion; existing
tests must keep passing byte-for-byte — the regression check that this is
additive). Full suite run, then manual verification below.

---

## Manual verification (after Stage 2, and again at Stage 5)

Against the project's live XO test instance (credentials already in memory):
1. Confirm `/backup-logs/{id}` / `/restore-logs/{id}` return real nested
   detail for a real id.
2. Confirm what `/users?fields=id,email,permission` actually returns for real
   accounts — whether `email` is the bare username as expected.
3. Run a full collection with all three checkboxes ticked; confirm the
   redacted JSON artifacts download and open as valid JSON, and a known
   username is masked in the redacted copy and present in the raw copy.
4. (Stage 5) Build a support package including diagnostics and confirm the
   manifest and file list match what was actually staged.
5. Rebuild the running container and confirm each new card/finding renders
   correctly in dark mode before reporting a stage done.
6. XO Proxies remain unverified — confirm live the first time this runs
   against an instance with a real proxy-managed pool.

### Critical files

- `app/xo_client.py`, `app/redact.py` (Stage 1)
- `app/job_diagnostics.py` (new, Stage 2), `app/routes/collect.py`,
  `app/templates/collect.html` (Stage 2)
- `app/findings.py` (Stages 3–4)
- `app/job_support_package.py`, `app/routes/support_package.py`,
  `app/templates/support_package.html` (Stage 5)
- `app/job_collect.py`, `app/job_redact.py` (reference patterns only)

---

## Status (added 2026-09-16, updated 2026-09-17, not part of the original plan text above)

**Stage 1: shipped, commit `1a0f9d6`, matches the plan.**

**Stage 2: rebuilt 2026-09-17 to match this spec.** The first attempt
(commit `f3c229b`, a standalone `/diagnostics` page) was replaced outright —
`app/job_api_diagnostics.py`, `app/routes/diagnostics.py` and
`app/templates/diagnostics.html` are gone. What ships now:

- **Placement:** its own independent card on the existing Collect page, no
  new page or nav entry, not nested in the per-host form — as specified.
- **Source selection:** three independent checkboxes
  (`backup_restore`, `tasks`, `messages_alarms`).
- **Artifact shape:** per-source raw **and** redacted JSON artifacts
  (`backup-restore-detail.json`, `xapi-tasks.json`, `messages-alarms.json`,
  each in both forms) plus `diagnostics-report.json`.
- **Backup/restore detail scope:** full detail is fetched for every
  enumerated run in range, not only failed ones — this is an archive
  feature.
- **Naming:** `app/job_diagnostics.py`, KIND `collect_diagnostics`.
- **Routes:** `POST /collect/diagnostics` and
  `POST /collect/diagnostics/{job_id}/delete`, both on `app/routes/collect.py`.

Verified end to end against the live test XO instance
(`xo-ce.pozzatech.com`, admin token): all three sources read (33 backup/
restore runs, 98 tasks, 167 messages/alarms), detail fetched for all 33 runs
(not just failures), 3,180 values masked across ipv4/uuid/hostname, raw and
redacted copies both downloadable and distinct, delete removes the run from
the page. Full test suite (764 tests), `ruff check` and `ruff format --check`
all pass.

**Stage 3: fully built 2026-09-17, both parts — see the Stage 3 section above
for the confirmed real data behind each.** `findings._detail_failure_cause`
now tries `backup_failure_message` (whole-job) then `_locate_backup_failure`
(per-VM, via `_deepest_failure`) for a failed run's cause; `_from_backups`
also calls new `_degraded_backups`/`_fallback_vms` for any `success` run in
the window that silently fell back from delta to full.

This went through six same-day revisions after Nick caught, in turn: the
initial version only checking a job's latest run (hiding an earlier degraded
run whenever a later run in the same window had failed outright); then, once
fixed, the evidence for multiple degraded runs keeping only the most recent
cause, burying a real NBD outage behind a smaller, later, generic one; then,
once every cause was kept, sorting the bundled lines by VM count instead of
date; then that bundling causes into one job-level finding at all was wrong
regardless of line order, since the finding's own date was always the most
recent cause's — fixed by making every distinct cause its own finding with
its own real timestamp; then the exact same "bundle by job, keep only the
latest cause" defect found, separately, in the sibling `_failed_runs`
function (real failures, not degraded-but-successful runs) — a real window
had five failed runs that were actually two distinct causes, and grouping by
job alone reported only one of them — fixed the same way, one finding per
(job, cause); and finally that the action text unconditionally told every
operator to check NBD even when the run's own warnings never named NBD,
found by re-checking all 35 real Delta Backup runs on the live pool over 30
days against raw JSON, which also surfaced a real, previously invisible
finding on Nick's own pool: one VM has a recurring, unexplained delta-backup
fallback unrelated to the one confirmed NBD outage.

Also fixed, in the same pass, a pre-existing instance of the identical "wrong
thing decides order" bug in the unrelated `collect_log_findings` path
(log-bundle findings, not this stage) — found by checking the rest of the
codebase per Nick's explicit instruction to check everywhere, not just this
feature. All confirmed directly against live real data on
`xo-ce.pozzatech.com`, including raw downloaded JSON, not synthesized. Full
test suite (784 tests), `ruff check` and `ruff format --check` all pass.
Stages 4-5 are still unbuilt.
