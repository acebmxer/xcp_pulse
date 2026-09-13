# Configuration

[← back to the README](../README.md)

Every setting is an environment variable read from `xcp-pulse.env`. Copy `xcp-pulse.env.example`
to `xcp-pulse.env` and edit it; `xcp-pulse.env` is gitignored and must never be committed.

Settings are read once at startup, so a change needs `docker compose up -d`.

## Required

### `XCP_PULSE_ADMIN_PASSWORD_HASH`

An Argon2id hash of the password for the **first** admin account, created the
first time XCP Pulse starts with an empty database. No default — the app
refuses to start without it, and refuses a value that is not an Argon2 hash,
so a plaintext password pasted here is rejected rather than silently
accepted.

Generate one with:

```bash
docker compose run --rm xcp-pulse python -m app.hashpw
```

Once that first account exists, this variable and `XCP_PULSE_ADMIN_USER`
below are no longer read — accounts are managed from the **Users** page
(Settings → Manage users, admin-only) instead. Changing this variable later
has no effect on an existing installation; it only matters on a genuinely
empty database, e.g. a fresh volume. See
[Users and roles](user-guide/users-and-roles.md) for adding accounts, roles,
and password resets.

## Optional

### `XCP_PULSE_ADMIN_USER`

Default `admin`. The username given to that first admin account. See the note
under `XCP_PULSE_ADMIN_PASSWORD_HASH` above — it only applies on first start.

### `XCP_PULSE_SECRET_KEY`

Signs session cookies. Leave it blank and one is generated on first boot and
stored at `/data/secret_key` with owner-only permissions, so sessions survive a
restart without the secret ever being baked into the image.

Changing it logs everyone out. It will also derive the key that encrypts the
stored Xen Orchestra token, so changing it will then also invalidate that.

### `XCP_PULSE_ENABLE_HTTPS`

Default `false`. Set `true` to serve HTTPS with XCP Pulse's own bundled
nginx — no reverse proxy required. Generates a self-signed certificate on
first run (replace it with your own from **Settings**), listens on `8443`
for HTTPS and on `8080` for a redirect to it, and implies `XCP_PULSE_HTTPS`
below so the session cookie is correct without setting that separately. See
[Where to expose it](installation.md#where-to-expose-it) for the full
picture, including running behind your own reverse proxy instead.

### `XCP_PULSE_HTTPS`

Default `false`. Set `true` when XCP Pulse is served over HTTPS by something
*other than* its own built-in nginx above — an external reverse proxy you run
yourself; the session cookie then carries the `Secure` flag.
`XCP_PULSE_ENABLE_HTTPS=true` sets this for you and does not need it set too.

Set it `true` while actually serving plain HTTP and login will appear to
fail — the browser accepts the redirect but discards the cookie.

### `XCP_PULSE_SESSION_HOURS`

Default `12`. How long a session lasts. The clock slides forward on each request,
so an active session does not expire under you.

### `XCP_PULSE_LOGIN_MAX_ATTEMPTS` and `XCP_PULSE_LOGIN_LOCKOUT_MINUTES`

Defaults `5` and `15`. This many failed logins from one address inside the window
blocks further attempts until the window passes — including attempts with the
correct password, so guessing right on the sixth try does not get you in.

The address comes from `X-Forwarded-For` when present, which is why XCP Pulse
should sit behind a proxy: exposed directly, that header is client-supplied.

### `XCP_PULSE_DATA_DIR`

Default `/data`. Where the database, session key and collected bundles live
inside the container. Normally you change the volume in `docker-compose.yml` rather
than this.

### `XCP_PULSE_LOG_LEVEL`

Default `INFO`. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. This is XCP Pulse's
own logging, not the XCP-ng logs it collects.

### `XCP_PULSE_ENABLE_SELF_UPDATE`

Default `false`. Set `true` to check for and apply updates from the **Update**
page (visible to admins and operators) instead of running
`docker compose pull && docker compose up -d` by hand.

Applying an update needs the Docker socket, which is effectively host root, so
this is opt-in and needs three things together — all off by default:

1. `XCP_PULSE_ENABLE_SELF_UPDATE=true` here.
2. The Docker socket volume line uncommented in `docker-compose.yml` (see the
   comment above it in `docker-compose.yml.example`). `docker-compose.yml`
   and `xcp-pulse.env` themselves do **not** need mounting into this
   container — applying an update hands the actual pull and restart to a
   throwaway container that mounts the project directory at its real host
   path instead (see `app/update.py` if you want the mechanism).
3. `group_add` uncommented too, with a `DOCKER_GID` value — see below — and
   `XCP_PULSE_COMPOSE_PROJECT_DIR` below, set to the host directory
   `docker-compose.yml` lives in.

With only the flag set and the socket left unmounted, the Update page still
checks GHCR daily and says whether a new image is out, but Apply fails
immediately with a clear error rather than doing nothing silently — checking
and applying are independent, and only applying touches the socket.

**`group_add` and the separate `.env` file it needs.** This container runs as
a non-root user, and the socket is owned by `root:docker` on the host —
without joining that group, every call to the socket fails with "permission
denied" even though the mount itself succeeded. The `docker` group's GID is
different on every host, so `docker-compose.yml.example` reads it from
`${DOCKER_GID}`. That has to come from Compose's own variable substitution,
which reads a file literally named `.env` — **not** `xcp-pulse.env`, which is
deliberately named otherwise so this exact substitution never touches the
Argon2 hash inside it (see that file's own comment). So this one value goes
in an actual `.env` file next to `docker-compose.yml`:

```bash
echo "DOCKER_GID=$(getent group docker | cut -d: -f3)" > .env
```

### `XCP_PULSE_COMPOSE_PROJECT_DIR`

No default; required for self-update to apply anything. The directory on the
**host** — not inside the container — that `docker-compose.yml` lives in.
Applying an update runs `docker compose` against the host's Docker daemon
through the mounted socket, and it needs this to resolve the same project
name and the same relative volume paths (like the `./data` bind some
deployments use) that your original `docker compose up -d` did.

## Set in the web UI, not here

Xen Orchestra connection settings are entered in the web UI and stored encrypted
in the database, not set here — a token in an environment variable ends up in
`docker inspect` output and shell history. The URL, the API token and the
account type (admin or restricted) are given together; which XO account to use
is covered next.

Retention settings for collected bundles arrive with log collection.

## Which Xen Orchestra account to use

**A restricted account works, once it holds the right privilege.** Measured
against XO CE with `@xen-orchestra/rest-api` **0.39.0**: downloading a host's
logs requires `export:logs` on host — a **separate privilege from read**. Its
own API specification documents this:

```
/hosts/{id}/logs.tgz   Required privilege: resource: host, action: export:logs
/hosts/{id}/audit.txt  Required privilege: resource: host, action: export:logs
```

None of Xen Orchestra's eight **built-in** role templates grant it — **Read
only** stops at `host:read`/`pool:read`, and only **Administrator**'s `host:*`
happens to cover it, which is why `/rest/v0/acl-privileges` (the list of
privileges the built-in roles currently hold) can look like `export:logs`
isn't grantable at all. It's listing what exists, not what's possible: that
endpoint returns privilege rows already created on the instance, not a fixed
catalogue of what can be created.

**The fix is a custom role**, which any RBAC-capable instance accepts through
the REST API:

```
POST /rest/v0/acl-roles                {"name": "Log exporter"}
POST /rest/v0/acl-privileges           {"resource": "host", "action": "export:logs",
                                         "effect": "allow", "roleId": "<the role id>"}
PUT  /rest/v0/acl-roles/<id>/users/<userId>
```

Verified end to end against a live instance: a restricted account with no
prior host access, after being assigned only that custom role, was accepted
by `/hosts/{id}/audit.txt` (`200`) — the same privilege check `logs.tgz` uses.

| What XCP Pulse does | Privilege | Restricted account |
| --- | --- | --- |
| List pools and hosts | `read` on pool and host | Yes — the **Read only** role |
| Read alarms, messages, tasks, patches | `read` on those resources | Yes |
| Download logs and audit trail | `export:logs` on host | Yes — needs a **custom role**; no built-in template grants it |

So a restricted account can do everything XCP Pulse needs, including log
collection — it just needs that one custom role created once, rather than
being handed full host administration. XCP Pulse still asks which account
type it has been given and reports what the connected instance can actually
grant, since a restricted account with no such role will be refused with a
plain `403` until one is added.

> [!NOTE]
> **An existing XO 5 ACL does not grant this**, however the account is set up.
> ACLs (Settings → ACLs) and RBAC are two separate systems: ACLs only offer
> Viewer/Operator/Admin roles on an object, with no `export:logs` action to
> grant in the first place, and Xen Orchestra's own docs say plainly that
> ACLs apply to the JSON-RPC API behind the XO 5 interface, not to the REST
> API XCP Pulse uses. Only an RBAC role, created and assigned as above,
> reaches it.
>
> There is currently no UI for creating that role on either XO version: XO
> 5's ACLs page only knows the older ACL model, and XO 6's own Roles/Groups
> pages redirect back to that same XO 5 page rather than exposing RBAC v2.
> The three calls above, against the REST API directly, are the only way to
> create and assign a role today. That does not make this fragile: XCP Pulse
> reads the outcome of the role assignment, not the presence of any UI for
> making it, so nothing here needs to change once a Roles UI ships — it would
> just be a different way of making the same REST calls.
>
> **Account type (Administrator/Restricted) is a separate setting from all of
> this.** It reflects Xen Orchestra's account-level permission
> (Settings → Users → Admin/User in XO 6, "Permission" in XO 5), which groups,
> ACLs and RBAC roles do not change. Log export is checked independently of
> it: an admin account always has it; a restricted account has it only once
> the custom role above is assigned.

> [!IMPORTANT]
> **On XOA, restricted accounts additionally need Essential+, Pro or
> Enterprise.** Role-based access control is not available on the lower XOA
> tiers. Installations from the sources are not restricted.

## What XCP Pulse will not do

- **Agents on hosts.** Everything goes through the Xen Orchestra API. No
  software is installed on XCP-ng hosts.
- **Writing to your pool.** XCP Pulse reads. It does not start, stop, patch or
  reconfigure anything.
- **Sending data anywhere.** Bundles are downloaded by you and sent by you.
  XCP Pulse does not upload to Vates or anyone else.
