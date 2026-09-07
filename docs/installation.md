# Installation

[← back to the README](../README.md)

## Requirements

- Docker with Compose v2 (`docker compose version`)
- About 200 MB of disk for the image; once log collection ships, roughly
  **450 MB per host per log collection** on the data volume

## Install

The image is published to GitHub Container Registry, so two files are the whole
deployment — there is nothing to clone and nothing to build:

```bash
mkdir xcp-pulse && cd xcp-pulse
curl -O https://raw.githubusercontent.com/acebmxer/xcp_pulse/main/compose.yaml
curl -o xcp-pulse.env https://raw.githubusercontent.com/acebmxer/xcp_pulse/main/xcp-pulse.env.example
```

The env file must be called `xcp-pulse.env` and not `.env` — compose treats a
file of that name as its own variable source and mangles the password hash.

Prefer to build from source? Clone the repository instead, uncomment `build: .`
in `compose.yaml`, and add `--build` to the `up` command below:

```bash
git clone https://github.com/acebmxer/xcp_pulse.git
cd xcp_pulse
cp xcp-pulse.env.example xcp-pulse.env
```

Generate the admin password hash. XCP Pulse stores a hash, never a password, and
will not start without one:

```bash
docker compose run --rm xcp-pulse python -m app.hashpw
```

It prompts for a password twice, then prints a line beginning
`XCP_PULSE_ADMIN_PASSWORD_HASH=$argon2id$...`. Paste that into `xcp-pulse.env`, replacing
the empty entry already there.

Start it:

```bash
docker compose up -d
```

Open <http://localhost:8080> and sign in with `admin` and the password you chose.

## Checking it is healthy

```bash
curl -sf http://localhost:8080/healthz
```

Expected: `{"status":"ok","version":"0.1.0"}`. This endpoint needs no login — the
container healthcheck uses it.

```bash
docker compose ps       # should show "healthy"
docker compose logs -f  # application log
```

## Upgrading

Edit the image tag in `compose.yaml` to the version you want, then:

```bash
docker compose pull
docker compose up -d
```

From a clone, it is `git pull` followed by `docker compose up -d --build`.

The data volume is untouched by a rebuild. Database migrations run at startup.

## Uninstalling

```bash
docker compose down          # stop, keep collected data
docker compose down -v       # stop and delete the volume as well
```

> [!WARNING]
> `down -v` deletes the data volume: the database, the session key, and every
> collected log bundle. There is no undo.

## Exposing it beyond localhost

The compose file binds to `127.0.0.1:8080` on purpose. XCP Pulse holds a token
that can read every log on your pool, so it belongs behind a reverse proxy on a
trusted management network. If you put TLS in front of it, set
`XCP_PULSE_HTTPS=true` in `xcp-pulse.env` so the session cookie carries the `Secure` flag.

Setting it to `false` while serving over HTTPS is safe but weaker; setting it to
`true` while serving plain HTTP makes login appear to fail silently, because the
browser discards the cookie.

## Troubleshooting

**"XCP_PULSE_ADMIN_PASSWORD_HASH is not set"** — the container is doing what it
should. Run `python -m app.hashpw` as above and put the result in `xcp-pulse.env`.

**Login succeeds but bounces back to the login page** — almost always
`XCP_PULSE_HTTPS=true` while serving over plain HTTP. Set it to `false`.

**Port already in use** — change the left-hand side of the port mapping in
`compose.yaml`, for example `"127.0.0.1:9090:8080"`.
