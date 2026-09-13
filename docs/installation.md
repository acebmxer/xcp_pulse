# Installation

[← back to the README](../README.md)

## Requirements

- Docker with Compose v2 (`docker compose version`)
- About 200 MB of disk for the image, plus roughly
  **450 MB per host per log collection** on the data volume

## Install

The image is published to GitHub Container Registry, so two files are the whole
deployment — there is nothing to clone and nothing to build:

```bash
mkdir xcp-pulse && cd xcp-pulse
curl -o docker-compose.yml https://raw.githubusercontent.com/acebmxer/xcp_pulse/main/docker-compose.yml.example
curl -o xcp-pulse.env https://raw.githubusercontent.com/acebmxer/xcp_pulse/main/xcp-pulse.env.example
```

The env file must be called `xcp-pulse.env` and not `.env` — compose treats a
file of that name as its own variable source and mangles the password hash.

Prefer to build from source? Clone the repository and copy both samples:

```bash
git clone https://github.com/acebmxer/xcp_pulse.git
cd xcp_pulse
cp docker-compose.yml.example docker-compose.yml
cp xcp-pulse.env.example xcp-pulse.env
```

`docker-compose.dev.yml` is committed alongside it and adds `build: .`, so the
image is built from your clone rather than pulled. Add it to every compose
command:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

Generate the admin password hash. XCP Pulse stores a hash, never a password, and
will not start without one:

```bash
docker compose run --rm xcp-pulse python -m app.hashpw
```

From a clone, add the overlay here too — without it, compose pulls the
published image rather than running your build:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm xcp-pulse python -m app.hashpw
```

It prompts for a password twice, then prints a line beginning
`XCP_PULSE_ADMIN_PASSWORD_HASH=$argon2id$...`. Paste that into `xcp-pulse.env`, replacing
the empty entry already there.

Start it:

```bash
docker compose up -d
```

Open `http://<server>:8080` and sign in with `admin` and the password you
chose. On the Docker host itself, <http://localhost:8080> works too.

That account is an admin, and can add further accounts — operator or
viewer, for people who don't need to touch settings — from Settings →
Manage users. See
[Users and roles](user-guide/users-and-roles.md) for what each role can do.

## Checking it is healthy

```bash
curl -sf http://localhost:8080/healthz
```

Expected: `{"status":"ok","version":"0.8.0"}`. This endpoint needs no login — the
container healthcheck uses it.

```bash
docker compose ps       # should show "healthy"
docker compose logs -f  # application log
```

## Upgrading

The sample uses `:latest`, so upgrading is a pull and a restart:

```bash
docker compose pull
docker compose up -d
```

To stay on one version instead, pin the tag in `docker-compose.yml` — the
sample carries a commented example:

```yaml
image: ghcr.io/acebmxer/xcp_pulse:0.8.0
```

A pinned deployment then upgrades by editing that tag and running the two
commands above.

From a clone, it is `git pull` followed by a rebuild:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

The data volume is untouched by a rebuild. Database migrations run at startup.

## Uninstalling

```bash
docker compose down          # stop, keep collected data
docker compose down -v       # stop and delete the volume as well
```

> [!WARNING]
> `down -v` deletes the data volume: the database, the session key, and every
> collected log bundle. There is no undo.

## Where to expose it

The compose file publishes `8080` on all interfaces, so the app is reachable at
`http://<server>:8080` from your network as soon as it starts.

XCP Pulse holds a token that can read every log on your pool, so keep it on a
trusted management network and do not expose it to the internet. To restrict it
to the Docker host alone — a reverse proxy running on the same box — put the
loopback address in front of the mapping:

```yaml
      - "127.0.0.1:8080:8080"
```

Serving it over HTTPS needs one of the following. Either way, once it's
working, the session cookie needs to know: see
[HTTPS and the session cookie](#https-and-the-session-cookie) below.

### Built-in HTTPS (no reverse proxy)

Set `XCP_PULSE_ENABLE_HTTPS=true` in `xcp-pulse.env` and XCP Pulse serves
HTTPS itself, with a small nginx bundled into the image — nothing else to
install or run. On first start with the flag on, it generates a self-signed
certificate and:

- Listens on **8443** for HTTPS
- Listens on **8080** and redirects every request to the HTTPS port, so an
  old `http://` bookmark still lands
- Sets the session cookie's `Secure` flag for you — `XCP_PULSE_HTTPS` does
  not also need setting in this case

A self-signed certificate means your browser will warn about it once, until
you tell it to trust the exception. If you have your own certificate — from
an internal CA, or one already issued for this hostname — upload it from
**Settings** once logged in; nginx starts using it immediately, no restart
required. The compose sample publishes `8443` alongside `8080`; if you leave
`XCP_PULSE_ENABLE_HTTPS` off, nothing listens on it and the mapping is inert.

This is meant for a deployment with nothing in front of it already. If you
run nginx-proxy-manager, Caddy or Traefik for other services, front XCP
Pulse with that instead — see the next section.

### Behind your own reverse proxy

If you already run a reverse proxy for other services, point it at XCP
Pulse's `8080` (bind that port to the Docker host only, per
[the mapping above](#where-to-expose-it)) rather than turning on built-in
HTTPS as well — running two TLS terminators in front of the same app is
redundant, and only one of them can hold port 443.

**Nginx Proxy Manager** — add a Proxy Host pointing at the Docker host on
port `8080`, request a Let's Encrypt certificate (or upload your own) under
the SSL tab, and force SSL.

**Caddy** — a `Caddyfile` entry is enough for Caddy to obtain and renew a
certificate automatically:

```
xcp-pulse.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

**Traefik** — as a compose label alongside the `xcp-pulse` service (adjust
the entrypoint and certresolver names to match your own Traefik setup):

```yaml
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.xcp-pulse.rule=Host(`xcp-pulse.example.com`)"
      - "traefik.http.routers.xcp-pulse.entrypoints=websecure"
      - "traefik.http.routers.xcp-pulse.tls.certresolver=letsencrypt"
      - "traefik.http.services.xcp-pulse.loadbalancer.server.port=8080"
```

None of these need XCP Pulse's own built-in HTTPS — leave
`XCP_PULSE_ENABLE_HTTPS` unset. They do need the session cookie told it's
being served over HTTPS: see the next section.

### HTTPS and the session cookie

Set `XCP_PULSE_HTTPS=true` in `xcp-pulse.env` when something other than XCP
Pulse's own built-in nginx is terminating TLS in front of it — an external
reverse proxy — so the session cookie carries the `Secure` flag. Built-in
HTTPS (`XCP_PULSE_ENABLE_HTTPS=true`) sets this for you automatically and
does not need it set separately.

Setting it to `false` while actually served over HTTPS is safe but weaker;
setting it to `true` while served plain HTTP makes login appear to fail
silently, because the browser discards the cookie.

## Xen Orchestra behind a reverse proxy (known bug, unsolved)

This is about the proxy in front of **Xen Orchestra**, not the one in front of
XCP Pulse ([Where to expose it](#where-to-expose-it) above is the other
direction).

If XO is reached through a reverse proxy — Nginx Proxy Manager, Traefik, a
hand-written Nginx `server` block, anything — a log collection can come back
truncated, with the collection job reporting **"the bundle ends early"**. XCP
Pulse still salvages what arrived rather than failing outright, but the bundle
is incomplete.

**This is an open, unsolved bug, recorded here so someone with more insight
into Nginx/httpx internals — or a different proxy setup where it reproduces
more cleanly — has a starting point.** Two different fixes were tried against
a real deployment. Both **reduced** how often truncation happened. **Neither
eliminated it.**

**What is understood:** XCP-ng's log bundle (`logs.tgz`) is built on the fly,
so Xen Orchestra serves it as `Transfer-Encoding: chunked` with no
`Content-Length` — there is no way to know the final size in advance. Against
the deployment this was tested on (Nginx Proxy Manager), the proxy's
connection to XO sometimes closes before the final chunk arrives — observed
directly as `httpx.RemoteProtocolError: peer closed connection without
sending complete message body (incomplete chunked read)`.

**What is not understood:** what decides whether a given attempt succeeds or
truncates. Identical code, identical proxy configuration, run twice back to
back: one completed in 11 seconds with the full ~447 MB, the next truncated at
the same byte offset every earlier attempt had also stopped at.

**Option 1 — proxy configuration.** This block in the proxy host's
**Advanced → Custom Nginx Configuration** (Nginx Proxy Manager) reduced how
often truncation happened, and separately fixed a stall this deployment also
hit on large downloads:

```nginx
proxy_buffering off;
proxy_request_buffering off;
proxy_max_temp_file_size 0;
proxy_read_timeout 900s;
proxy_send_timeout 900s;
proxy_set_header Connection "";
```

`client_max_body_size 0;` is worth adding too if uploads to XO (ISO images,
templates) are ever routed through the same proxy host — it lifts the upload
size cap, and is unrelated to this download issue.

> [!WARNING]
> **Do not add `proxy_http_version 1.1;`.** It looks like the obvious pairing
> for `Connection ""`, but on the deployment this was tested against it broke
> the proxy outright (`SSL_do_handshake` / `TLSV1_UNRECOGNIZED_NAME`, the
> whole XO web UI unreachable) — for a reason not understood. Test any change
> here on a throwaway proxy host, not the one you rely on to reach XO.

**Option 2 — suppress `httpx`'s default `Accept-Encoding` header.** `httpx`
sends `Accept-Encoding: gzip, deflate` by default, asking the proxy to
transport-encode a body that is already a gzip file. Forcing
`Accept-Encoding: identity` on the download request completed a transfer in
11 seconds that otherwise took over a minute and often truncated — but a
second, identical attempt truncated anyway. Also reduces frequency, does not
eliminate it. **Not implemented in the codebase** — recorded here as a lead,
not a change that shipped, since it has not been shown to actually fix
anything.

**Until the real trigger is found, the reliable move when a collection
reports "the bundle ends early" is to run it again.** Both known mitigations
make a retry more likely to succeed, neither guarantees it.

If you run a different reverse proxy (Traefik, Caddy, a bare Nginx config),
the same chunked-transfer requirement applies, but none of this has been
tested against them.

## Troubleshooting

**"XCP_PULSE_ADMIN_PASSWORD_HASH is not set"** — the container is doing what it
should. Run `python -m app.hashpw` as above and put the result in `xcp-pulse.env`.

**Login succeeds but bounces back to the login page** — almost always
`XCP_PULSE_HTTPS=true` while actually serving over plain HTTP (this can't
happen with `XCP_PULSE_ENABLE_HTTPS=true`, which sets the cookie flag to
match on its own). Set `XCP_PULSE_HTTPS` to `false`, or fix what's actually
serving TLS.

**Browser warns the certificate is not trusted** — expected with built-in
HTTPS's self-signed certificate; accept the warning once, or upload your own
certificate from Settings to remove it. Unexpected with a certificate from a
real CA (Let's Encrypt via your own reverse proxy, say) — check that proxy's
own certificate, not XCP Pulse.

**A collection reports "the bundle ends early"** — see
[Xen Orchestra behind a reverse proxy](#xen-orchestra-behind-a-reverse-proxy-known-bug-unsolved)
above. This is XO's side of the connection, not XCP Pulse's.

**Port already in use** — change the left-hand side of the port mapping in
`docker-compose.yml`, for example `"9090:8080"`. The right-hand side is the port
inside the container and never changes.

**Nothing loads at `http://<server>:8080`** — check `docker ps`. A mapping shown
as `127.0.0.1:8080->8080/tcp` answers only on the Docker host itself; drop the
`127.0.0.1:` prefix to reach it from other machines. `0.0.0.0:8080->8080/tcp` is
the one that answers on your network.
