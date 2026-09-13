#!/bin/bash
# Container entrypoint. Decides, once, whether XCP Pulse serves plain HTTP
# directly (today's behaviour, unchanged) or built-in HTTPS via nginx.
#
# set -e: any failed step (cert generation, nginx config test) must stop the
# container rather than silently falling back to a state nobody asked for.
set -e

if [ "$#" -gt 0 ]; then
    # An explicit command was given (`docker run <image> <cmd>`, or the
    # hashpw invocation `docker compose run --rm xcp-pulse python -m
    # app.hashpw`) — run exactly that and nothing else, the same as if this
    # entrypoint script did not exist. The HTTPS decision below only applies
    # to the container's own default startup.
    exec "$@"
fi

if [ "$(printf '%s' "${XCP_PULSE_ENABLE_HTTPS:-}" | tr '[:upper:]' '[:lower:]')" != "true" ]; then
    # No HTTPS: exactly the pre-nginx behaviour, uvicorn owns the port
    # directly. Nothing in this branch changes when this feature is off.
    exec uvicorn app.main:app --host 0.0.0.0 --port 8080
fi

TLS_DIR="${XCP_PULSE_DATA_DIR:-/data}/tls"
mkdir -p "$TLS_DIR"

if [ ! -f "$TLS_DIR/cert.pem" ] || [ ! -f "$TLS_DIR/key.pem" ]; then
    echo "xcp-pulse: no certificate at $TLS_DIR, generating a self-signed one"
    # 10 years: this is a self-signed cert for a LAN tool, not a publicly
    # trusted one — its expiry is not a security boundary, and re-prompting
    # an operator to regenerate it periodically would only train people to
    # click through browser warnings faster. Uploading a real cert through
    # Settings replaces it at any time regardless of this expiry.
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$TLS_DIR/key.pem" -out "$TLS_DIR/cert.pem" \
        -subj "/CN=xcp-pulse" >/dev/null 2>&1
    chmod 600 "$TLS_DIR/key.pem"
fi

mkdir -p /tmp/nginx/logs /tmp/nginx/run /tmp/nginx/client_temp \
    /tmp/nginx/proxy_temp /tmp/nginx/fastcgi_temp /tmp/nginx/uwsgi_temp \
    /tmp/nginx/scgi_temp

# uvicorn moves to a loopback-only port that nginx proxies to; nginx alone
# holds 8080 (redirect) and 8443 (TLS) so the app is never reachable except
# through it once HTTPS is enabled.
uvicorn app.main:app --host 127.0.0.1 --port 8081 &
UVICORN_PID=$!

# Either process dying takes the container down, rather than limping along
# with only half of it working — supervisord-style automatic restart was
# considered and rejected as more than this needs; a crashed container gets
# restarted by Docker's own restart policy instead, per xcp-pulse's `restart:
# unless-stopped` in docker-compose.yml.
trap 'kill "$UVICORN_PID" 2>/dev/null' EXIT INT TERM

nginx -c /etc/xcp-pulse/nginx.conf -g "daemon off;" &
NGINX_PID=$!

wait -n "$UVICORN_PID" "$NGINX_PID"
