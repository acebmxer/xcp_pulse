# Configuration

[← back to the README](../README.md)

Every setting is an environment variable read from `xcp-pulse.env`. Copy `xcp-pulse.env.example`
to `xcp-pulse.env` and edit it; `xcp-pulse.env` is gitignored and must never be committed.

Settings are read once at startup, so a change needs `docker compose up -d`.

## Required

### `XCP_PULSE_ADMIN_PASSWORD_HASH`

An Argon2id hash of the admin password. No default — the app refuses to start
without it, and refuses a value that is not an Argon2 hash, so a plaintext
password pasted here is rejected rather than silently accepted.

Generate one with:

```bash
docker compose run --rm xcp-pulse python -m app.hashpw
```

## Optional

### `XCP_PULSE_ADMIN_USER`

Default `admin`. The username at the login screen.

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

## Set in the web UI, not here

Xen Orchestra connection settings are entered in the web UI and stored encrypted
in the database, not set here — a token in an environment variable ends up in
`docker inspect` output and shell history. The URL, the API token and the
account type (admin or restricted) are given together, and which XO account to
use is covered in [the roadmap](roadmap.md#which-xen-orchestra-account-to-use).

> [!IMPORTANT]
> **Log collection may need an admin XO account.** Downloading a host's logs
> requires the `export:logs` privilege. Xen Orchestra defines it, but some
> instances carry a privilege catalogue seeded before it was added and cannot
> grant it to a restricted account — on such an instance, log collection needs
> an admin account. XCP Pulse checks the connected instance and tells you which
> case you are in. A restricted account always works for inventory and
> API-based findings.
>
> Separately, **XOA below the Essential+ tier has no role-based access control
> at all**, so those users must use an admin account regardless. Installations
> from the sources are not restricted.

Retention settings for collected bundles arrive with log collection.
