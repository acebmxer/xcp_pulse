# Roadmap

[← back to the README](../README.md)

What is built, what is next, and what each piece of work lets you do that you
could not do before.

The dashboard shows the same list under **What is coming**, in its own
hand-written HTML (`app/templates/dashboard.html`) — it does not read this
file, so the two have to be kept in step by hand. That section stays on the
dashboard permanently: as work ships, its rows are updated rather than
removed, not cleared out once there is real content. It comes off only when
Nick says so. See "Finalise the dashboard and the UI" below for whether this
should instead read this file directly.

## About the version numbers

**Only shipped work has a version number.** Planned work is deliberately
unnumbered, because assigning `v0.9.0` to something unbuilt is a guess about
order, and the guess goes stale the moment the order changes.

A number gets attached when the work actually starts, and is recorded in
`CHANGELOG.md` when it ships. Planned items are grouped by whether anything
genuinely has to come first:

- **Free to pick up in any order** — no technical dependency. Choose by what is
  most useful.
- **Has prerequisites** — something must exist first, and the reason is stated.
  These are real constraints, not preferences.

---

## Shipped

### v0.1.0 — Log in

Run the container and log in.

- Docker image and compose file, non-root, data on a named volume
- Login screen, server-side sessions, logout, login throttling
- `/healthz` for the container healthcheck
- Dashboard placeholder, function index, documentation

### v0.2.0 — Connect to Xen Orchestra

Save an XO URL and API token, and check what the account can reach.

- Settings page; the token is encrypted at rest and never rendered back
- The account type is chosen alongside the token — **admin** or **restricted** —
  so XCP Pulse knows which endpoints to expect a `403` from, and says which
  privilege is missing rather than reporting a generic failure
- "Test connection" with a clear error when it fails

### v0.3.0 — List pools and hosts

See the inventory on the dashboard.

- Pools and hosts from `/rest/v0/pools` and `/rest/v0/hosts`, grouped by pool
- Host address, version, cores and memory in use; pool master and disabled
  hosts marked
- An account that can see nothing is told why, rather than shown an empty list

### v0.4.0 — Background jobs and stored results

Work that takes time runs in the background, and what it produced is kept.

- A job system: queued, running, succeeded, failed and cancelled, with progress
- An artifact store — bodies on the data volume, metadata in the database, so
  the same store holds a few hundred bytes of JSON and a 433 MB bundle later
- A **Refresh inventory** job, replacing the read-on-page-load in v0.3.0
- A jobs page: history, live progress, and cancelling what is running

The queue is a database table rather than an in-memory structure. That is what
lets a job survive a restart, and what lets a separate worker process be added
later without changing the schema or any job body.

> [!NOTE]
> The job system was built here, against endpoints that answer in milliseconds,
> so that the 100-second collection job later lands on a system already proven.

### v0.5.0 — Redaction

Mask addresses, tokens and credentials out of log text, and see it happen.

- Eight rules: IPv4, IPv6, MAC, UUID, `trackid=` session tokens,
  password/secret/session_id/API-key assignments, email addresses, hostnames
- A preview page: paste a snippet, see the result and per-rule hit counts
- Placeholders keep the shape of what they replaced, and equal values get equal
  placeholders, so a redacted log still reads as a log
- Loopback and documentation names stay legible; source filenames, API method
  names and backtraces are deliberately left alone

Nothing pasted into the preview is stored. Redaction works a line at a time,
which is the unit the streaming repack of a real bundle will use.

### v0.5.1 — Per-rule enable and disable

Turn an individual redaction rule off when it is masking something you need.

- A rule's on/off state stored in the database and applied wherever redaction
  runs, not only in the preview
- The preview reflects the current settings, so it shows what a collected
  bundle would actually get
- Only the switched-off rules are stored, so a rule added in a later version is
  on from the moment it exists
- The page says how many rules are off, because a bundle collected with masking
  disabled is the failure this makes possible

### v0.5.2 — Redaction report

A record of what was masked, alongside what was produced.

- A **Redact a stored file** job, masking an artifact and keeping the result
- Per-rule hit counts for a whole run, not just a pasted snippet
- The report is kept as an artifact beside the redacted copy, naming both files
  by size and SHA-256, so the counts stay attached to what they describe
- A switched-off rule reads as **off**, not as zero hits, because "nothing was
  found" and "nothing was looked for" are different answers

The file is read a line at a time and never held, so the same job serves a few
hundred bytes now and a 433 MB bundle once collection lands. The masking is the
same `active_rules` and `Rule.apply` the preview page uses, in the same order,
so the two cannot diverge.

### v0.5.3 — Deployment fixes

No new capability; the quick start works as written on a server.

- The compose sample publishes on all interfaces, so a deployment on a remote
  machine is reachable rather than answering only on the Docker host
- It pulls `:latest`, so a fresh install runs the current release without an
  edit first
- Building from a clone uses a `docker-compose.dev.yml` overlay, leaving the
  deployment's own compose file untouched
- The sample is named `docker-compose.yml.example`

### v0.7.0 — Findings, category extraction and the support package

Know what's wrong without collecting anything, pull just the logs you need,
and hand a support ticket one archive instead of several downloads.

- **Findings from the API**: a report built from seven XO/XCP-ng routes —
  messages, alarms, tasks, missing patches, backup runs, restore runs, the
  pool dashboard — in seconds, with no `export:logs` privilege needed.
  Repeated events are grouped by count; routine VM lifecycle events and failed
  logins are dropped as noise. A refused source (a restricted account denied
  the pool dashboard, an XOA without a support subscription) is reported as
  **unread**, never as clean.
- **Findings from collected logs**: the same report built from a stored
  `*-logs.tgz`, covering storage, multipath, XAPI, HA, out-of-memory and clock
  sync failures. A truncated bundle keeps what it read before the break rather
  than failing outright.
- **Correlation between the two**: a finding in both an API report and a log
  report — matched by condition family and, when timed, a one-hour window —
  is marked as confirmed by the other, so one incident doesn't read as two.
- **Extract individual log categories** from a bundle already on the data
  volume, into one combined archive — ten categories, current logs by default
  with rotated history opt-in. No smaller download exists to ask Xen Orchestra
  for; `logs.tgz` has no category filter.
- **Vates support package**: one `.tgz` — redacted bundle, findings in
  Markdown and JSON, the redaction report, the inventory, and a manifest —
  built from a stored collection or from a fresh **Collect + Package** run.
  Never ships with a gap it could have filled itself: building one always
  runs a fresh findings check and inventory refresh alongside the collection.

Both findings reports mask evidence with the redaction rules switched on at
the time, and name any rule that was switched off — an unmasked value and a
value no rule looked for read identically, so a report that doesn't say which
rules were off cannot be judged safe to send.

> [!NOTE]
> **Xen Orchestra applies `limit` to the oldest records, not the newest**, and
> ignores `sort` and `order`. Asking `/messages` for 2,000 of 3,472 rows
> returned the first month and hid every recent event. The window is a
> `filter` instead, applied server-side. Messages and alarms carry seconds
> while tasks and backup runs carry milliseconds — filtering one with the
> other's scale matches everything, which looks exactly like a working filter.

### v0.6.0 — Collect the full bundle

Collect a host's logs and download them, redacted.

- Per-host collection as a background job, with real progress and ETA
- Cancellable; a failed download cannot resume, and says so plainly
- Raw bundle kept; redacted bundle produced for download
- The XAPI audit trail is an optional extra per collection, off by default
- Bundle list with size and age, and retention with a preview of what the next
  cleanup will delete

Expect **about 433 MB and 100 seconds per host** — measured on XCP-ng 8.3.

A tarball cannot be masked in place, so the redacted bundle is repacked member
by member, each text member read a line at a time. The masking is the same
`active_rules` and `Rule.apply` the preview page uses, so what the preview shows
is what a collected bundle gets.

Both copies are kept and the download list marks which is which: the redacted
one is what goes to Vates, and the raw one is the only thing that can answer
"what was masked?" afterwards.

#### Which Xen Orchestra account to use

**An admin account, for now.** Measured against XO CE with
`@xen-orchestra/rest-api` **0.39.0**, a restricted account cannot be granted the
privilege the log download requires.

The REST API checks each route against the account's privileges, and downloading
a host's logs requires `export:logs` on host — a **separate privilege from
read**. Its own API specification documents this:

```
/hosts/{id}/logs.tgz   Required privilege: resource: host, action: export:logs
/hosts/{id}/audit.txt  Required privilege: resource: host, action: export:logs
```

The catch is that `export:logs` may not appear in the privilege catalogue that
roles are built from. Xen Orchestra defines it in its source, but the catalogue
is stored per instance, and on the instance measured it offered only three host
privileges — `read`, `allow-vm` and `*` — against the eighteen host actions the
API requires. Where that is the case, the only grantable privilege satisfying
the log download is `host:*`, which is full host administration.

Checked against all eight built-in roles on that instance: only
**Administrator** carries it. **Read only** grants `host:read` and `pool:read`,
and is refused the log download with `403 not enough privileges`.

**So this is a property of the connected instance, not a fixed rule.** XCP Pulse
reads the catalogue from Xen Orchestra and reports what that instance can
actually grant, rather than assuming either answer.

| What XCP Pulse does | Privilege | Grantable to a restricted account |
| --- | --- | --- |
| List pools and hosts | `read` on pool and host | Yes — the **Read only** role |
| Read alarms, messages, tasks, patches | `read` on those resources | Yes |
| Download logs and audit trail | `export:logs` on host | **No — needs `host:*`** |

So a restricted account works for inventory and API-based findings, and cannot
collect logs. XCP Pulse asks which account type it has been given so it can say
this plainly, rather than surfacing a bare `403`.

> [!NOTE]
> The version string does not tell you which set you have — the instance
> measured reported the same `0.39.0` as the source defining all eighteen
> actions, and served a catalogue seeded before they were added. Read
> `/rest/v0/acl-privileges` to find out, which is what XCP Pulse does.

> [!IMPORTANT]
> **On XOA, restricted accounts additionally need Essential+, Pro or
> Enterprise.** Role-based access control is not available on the lower XOA
> tiers. Installations from the sources are not restricted.

### v0.8.0 — Date ranges

Ask for the window you care about instead of everything on the host.

- A date-range picker wherever a range makes sense: collection, category
  downloads, findings, the support package
- Rotated logs selected by **modification time**, so "the last three days" skips
  the 28 older `xensource.log.N.gz` files rather than downloading and discarding
- Line-level filtering by parsed timestamp for files straddling the window edge
- Presets — last 24 hours, last 7 days, since the last reboot

Of a measured 433 MB bundle, 418 MB is rotated history, so a narrow window is a
large saving on what you keep and send.

> [!NOTE]
> **The first collection still downloads the whole bundle.** Xen Orchestra's
> `logs.tgz` accepts no date parameter and supports no range requests, so
> filtering happens here, after the download. A date range shrinks what you keep
> and send, not the 100 seconds of the initial fetch.
>
> There is nothing to filter until a bundle exists, which is why this follows
> collection rather than standing on its own.

### Documentation in the web UI — built, not yet released

Learn to use XCP Pulse without leaving it: a **User manual** section at
`/help`. A new `docs/user-guide/` — one page per feature area (first login,
Dashboard, Collect, Redaction, Jobs, Findings, Support package, Date ranges,
Settings), collapsible as a group in the sidebar — covers setup after the
container is running and what each page does, alongside Installation,
Configuration and Architecture as top-level entries: the three GitHub docs
still useful to a user rather than a contributor. `README.md` and
`docs/functions.md` are deliberately not rendered here, and neither is
`docs/roadmap.md`. Search across pages, and `[!NOTE]`/`[!WARNING]` callouts
styled instead of shown as plain blockquotes. Docs ship inside the image, so
what renders matches the version you are running rather than whatever is
newest on GitHub.

This is not marked shipped or given a version number yet, since that is a
release decision — the dashboard's roadmap panel says the same.

---

## Planned — free to pick up in any order

Nothing below blocks anything else. Order is a choice about what is most
useful.

### Finalise the dashboard and the UI

The dashboard shows status panels for findings, redaction, storage and recent
activity. What is left is the pass over the whole interface once the features
it reports on have stopped moving.

- Review every page for layout, spacing and wording as a set, rather than each
  one as it was built
- Decide which panels stay: a feature earns a panel only when it changes
  whether an operator needs to act, not because it has a page
- Panels for later features where that test is met — log findings, update
  availability, the support package
- Settle what **What is coming** becomes once the list is short — including
  whether it should read this file's Planned section directly instead of
  duplicating it by hand, which would make it structurally impossible for the
  two to drift the way they have already

This is deliberately last among the free-to-pick-up items. Tuning an interface
around features that are still being added means doing it twice.

### Self-update from the UI

*Prerequisite met: releases publish to `ghcr.io/acebmxer/xcp_pulse`.*

Tells you when a new version is out, and applies it from the UI.

Modelled on the mechanism in the sibling project
[beacon_pxe](https://github.com/acebmxer/beacon_pxe), whose hard-won details are
worth copying rather than rediscovering:

- **Compare image digests, not version strings.** What is deployed is read from
  the running container through the Docker socket, not remembered in the
  database — bookkeeping desyncs the moment someone updates by hand with
  `docker compose pull && up -d`, and then advertises an update already installed.
- **Hand the recreation to a throwaway container outside the compose project.**
  A container cannot reliably replace itself; it gets killed partway and the
  update appears to succeed while nothing was replaced.
- **Confirm success from the replacement, not the initiator.** The process that
  starts an update does not survive to see it finish, so the new container
  records the outcome at startup. A stall is reaped on a timeout with an error
  saying what to run by hand.
- An update channel — `latest` following main, `stable` following releases —
  where the tag the checker watches is the tag the compose file pulls.

Two things to settle before building it:

- ~~**It needs a published image.**~~ Settled: releases publish to
  `ghcr.io/acebmxer/xcp_pulse`, which is what the compose file already pulls.
- **It needs the Docker socket, which is effectively host root** — directly
  against this project's own threat model. The intended answer is that
  self-update is **opt-in**, with update *checking* (outbound HTTPS only)
  separable from update *applying*.

### Built-in HTTPS

Serve XCP Pulse over HTTPS without putting your own reverse proxy in front.

- Optional and off by default: a deployment that already has a proxy in front
  of it carries on unchanged, with no second TLS terminator competing for the
  port
- Either a certificate and key you mount in, or one obtained automatically —
  which means a public DNS name, a reachable port and somewhere on the data
  volume to persist it across restarts
- `XCP_PULSE_HTTPS` stops being something you set by hand: when XCP Pulse is
  terminating TLS itself it knows the browser is on HTTPS and can set the
  cookie's `Secure` flag without being told
- A plain-HTTP listener that redirects, so an old bookmark still lands

Most self-hosted deployments already run a proxy — nginx-proxy-manager, Caddy,
Traefik — and for those this is redundant. It is for the deployment that has
none, where standing one up is more work than the app it would front.

### Multiple users

More than one person can use XCP Pulse, with their own credentials.

- A user table replacing the single admin account from the environment; the
  environment variables become the **bootstrap** for the first account only
- Roles: **admin** (manage users, connections, settings) and **viewer**
  (collect and read, but not reconfigure)
- Per-user sessions, password changes, and deactivation without deletion
- An activity log recording who collected, downloaded or deleted what — bundles
  contain credential-adjacent data, so "who took a copy" is a real question
- Optional TOTP two-factor

Migration is automatic: the existing environment-configured admin becomes the
first row in the user table and continues to work.

---

## v1.0.0

When the above is stable and the configuration format is settled.

---

## Not planned

- **Agents on hosts.** Everything goes through the Xen Orchestra API. No
  software is installed on XCP-ng hosts.
- **Writing to your pool.** XCP Pulse reads. It does not start, stop, patch or
  reconfigure anything.
- **Sending data anywhere.** Bundles are downloaded by you and sent by you. XCP
  Pulse does not upload to Vates or anyone else.
