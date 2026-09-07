# Roadmap

[← back to the README](../README.md)

What is built, what is next, and what each piece of work lets you do that you
could not do before.

This list is also shown on the dashboard, under **What is coming**, and stays
there permanently. As work ships, its rows are updated rather than removed — the
section is a standing part of the UI, not a placeholder to be cleared out once
there is real content. It comes off only when Nick says so.

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

---

## Next

### Background jobs and stored results

Work that takes time runs in the background, and what it produced is kept.

- A job system: queued, running, finished and failed, with progress
- An artifact store for what a job produced
- Exercised by a **Refresh inventory** job, replacing today's read-on-page-load

> [!NOTE]
> The job system is built here, against endpoints that answer in milliseconds,
> so that the 100-second collection job later lands on a system already proven.

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


---

## Planned — free to pick up in any order

Nothing below blocks anything else. Order is a choice about what is most useful.

### Documentation in the web UI

Read the documentation without leaving XCP Pulse.

- A **Docs** section in the navigation, rendering the pages under `docs/`
- Docs ship inside the image, so they match the version you are running
- Search across pages, and deep links from the UI to the relevant section
- Markdown reformatted where it renders badly outside GitHub: `[!NOTE]` and
  `[!WARNING]` callouts become styled blocks, the `[← back to the README]`
  header lines give way to real navigation, and links between pages are
  rewritten to UI routes

The source files stay canonical and stay readable on GitHub; the UI is a second
view of them, not a fork.

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

### Date ranges

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
> This is why date ranges are listed as independent: they are useful the moment
> collection exists, and are not a prerequisite for anything.

---

## Planned — has prerequisites

The order inside this group is forced. Each item says what must come first.

### Redaction

*Prerequisite for: any downloadable bundle.*

See exactly what will be masked before anything can be downloaded.

- Rules for IPv4/IPv6, `trackid=` session tokens, password, secret and
  session_id assignments, MAC addresses, UUIDs, hostnames, email addresses
- Per-rule enable and disable
- A preview page: paste a log snippet, see the result
- A redaction report with per-rule hit counts

> [!IMPORTANT]
> This comes **before** the first downloadable bundle, not after. A real bundle
> contains internal addresses, usernames and session tokens, and the point of
> the download button is sending that file to Vates. Shipping collection first
> would mean a release whose headline feature leaks credentials.

Testable with no Xen Orchestra connection at all, which is what makes it easy to
do early.

### Collect the full bundle

*Needs: the job system, and redaction.*

Collect a host's logs and download them, redacted.

- Per-host collection as a background job, with real progress and ETA
- Cancellable; a failed download cannot resume, and says so plainly
- Raw bundle cached; redacted bundle produced for download
- Also collects the XAPI audit trail
- Bundle list with size and age, and retention with a preview of what the next
  cleanup will delete

Expect **about 433 MB and 100 seconds per host** — measured on XCP-ng 8.3.

### Collect individual categories

*Needs: full-bundle collection.*

Download only the log families you want, without collecting again.

- Categories: XAPI, storage, audit, security, kernel, system, HA, xenstore,
  RRD plugins, network
- "Current logs only" or "include rotated history"
- Extracted from the cached bundle, so it takes seconds rather than 100
- Current-logs-only bundles come to roughly **35 MB instead of 433 MB**

> [!NOTE]
> This cannot come first. `logs.tgz` supports no range requests and no
> server-side filtering, so a request for one category cannot be made smaller —
> it has to extract from a bundle already on disk.

### Findings from the API

*Needs: an XO connection. Does not need a collected bundle.*

A findings report without collecting anything.

- Failed tasks with stack traces, alarms, XAPI messages, missing patches,
  backup and restore results, pool dashboard
- Each finding: severity, title, evidence, suggested action, source

### Findings from the logs

*Needs: full-bundle collection.*

- Storage repository failures, multipath flapping, XAPI exceptions,
  out-of-memory events, HA fencing, clock skew — with counts and first/last seen
- Correlation between log events and XAPI messages on one timeline

### Vates support package

*Needs: redaction, collection, and findings.*

One file to attach to a support ticket.

- Redacted bundle, findings in Markdown and JSON, redaction report, inventory
- A manifest saying what is included and what was masked

### Self-update

*Needs: published container images.*

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

- **It needs a published image.** XCP Pulse builds from source today; a digest
  comparison needs something like `ghcr.io/acebmxer/xcp-pulse`. Publishing images
  is the prerequisite, not part of the feature.
- **It needs the Docker socket, which is effectively host root** — directly
  against this project's own threat model. The intended answer is that
  self-update is **opt-in**, with update *checking* (outbound HTTPS only)
  separable from update *applying*.

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
