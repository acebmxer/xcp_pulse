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

## Checking it is healthy

```bash
curl -sf http://localhost:8080/healthz
```

Expected: `{"status":"ok","version":"0.6.2"}`. This endpoint needs no login — the
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
image: ghcr.io/acebmxer/xcp_pulse:0.6.2
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

If you put TLS in front of it, set `XCP_PULSE_HTTPS=true` in `xcp-pulse.env` so
the session cookie carries the `Secure` flag.

Setting it to `false` while serving over HTTPS is safe but weaker; setting it to
`true` while serving plain HTTP makes login appear to fail silently, because the
browser discards the cookie.

## Troubleshooting

**"XCP_PULSE_ADMIN_PASSWORD_HASH is not set"** — the container is doing what it
should. Run `python -m app.hashpw` as above and put the result in `xcp-pulse.env`.

**Login succeeds but bounces back to the login page** — almost always
`XCP_PULSE_HTTPS=true` while serving over plain HTTP. Set it to `false`.

**Port already in use** — change the left-hand side of the port mapping in
`docker-compose.yml`, for example `"9090:8080"`. The right-hand side is the port
inside the container and never changes.

**Nothing loads at `http://<server>:8080`** — check `docker ps`. A mapping shown
as `127.0.0.1:8080->8080/tcp` answers only on the Docker host itself; drop the
`127.0.0.1:` prefix to reach it from other machines. `0.0.0.0:8080->8080/tcp` is
the one that answers on your network.
