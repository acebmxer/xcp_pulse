# XCP Pulse container image.
#
# Two stages: dependencies are installed into a virtualenv in the build stage
# and only that virtualenv is copied forward. The runtime image therefore
# carries no pip, no setuptools and no wheel — a container that cannot install
# packages cannot be made to install one, and pip's vendored bundle stops
# appearing in vulnerability scans of the published image.
FROM python:3.14-slim AS build

# Pull in Debian's security point-release fixes for the base OS packages
# (glibc, perl, util-linux, etc.) that python:3.14-slim ships. The app never
# apt-installs anything itself, so without this the image only gets these
# fixes whenever Docker Hub happens to refresh the slim tag — this makes it
# happen on every build instead. apt's own lists are dropped afterwards so
# they don't sit in the image (and can't go stale in a way that matters,
# since nothing here uses them again).
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/xcp-pulse

# The virtualenv is built at the same path it will occupy at runtime, so the
# shebangs and the recorded prefix stay correct after the copy.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependencies first: this layer is rebuilt only when pyproject.toml changes,
# not on every source edit.
COPY pyproject.toml README.md LICENSE ./
COPY app/__init__.py ./app/
RUN pip install --no-cache-dir .

# Nothing installs packages after this point, so the tooling that does is
# removed rather than copied forward.
RUN pip uninstall --yes pip setuptools wheel 2>/dev/null || true


FROM python:3.14-slim

# Same Debian security point-release upgrade as the build stage — this is the
# stage that actually ships, so this is the copy that matters for scans.
#
# nginx-light and openssl are for built-in HTTPS (XCP_PULSE_ENABLE_HTTPS,
# opt-in and off by default — see docker/entrypoint.sh): nginx terminates
# TLS in front of uvicorn, openssl generates a self-signed certificate on
# first run if none is supplied. Both are skipped entirely, at no image-size
# cost beyond their own package weight, when the feature is off.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends nginx-light openssl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    XCP_PULSE_DATA_DIR=/data

WORKDIR /srv/xcp-pulse

# The base image ships its own pip; this app never installs anything at
# runtime, so it goes too.
RUN python -m pip uninstall --yes pip 2>/dev/null || true \
    && rm -rf /usr/local/lib/python3.14/site-packages/pip \
              /usr/local/lib/python3.14/site-packages/pip-*.dist-info \
              /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.14

COPY --from=build /opt/venv /opt/venv

COPY app/ ./app/

# The in-app Docs section reads these at runtime, so what renders there is
# exactly what shipped with this build rather than whatever is newest on
# GitHub. docs/roadmap.md is intentionally excluded — see app/docs_render.py.
COPY README.md ./
COPY docs/ ./docs/

# The built-in HTTPS config and entrypoint — see docker/entrypoint.sh for what
# decides which of uvicorn-alone or nginx-in-front actually runs.
COPY docker/nginx.conf /etc/xcp-pulse/nginx.conf
COPY docker/entrypoint.sh /usr/local/bin/xcp-pulse-entrypoint.sh

# Run as a non-root user throughout, including nginx: both HTTPS ports (8080,
# 8443) are unprivileged, so nginx never needs the traditional root-then-
# drop-privileges start it conventionally uses. The data volume is chowned so
# the container can write bundles, the SQLite database and (when HTTPS is on)
# the TLS certificate to it.
RUN useradd --system --create-home --uid 10001 pulse \
    && mkdir -p /data \
    && chmod +x /usr/local/bin/xcp-pulse-entrypoint.sh \
    && chown -R pulse:pulse /data /srv/xcp-pulse
USER pulse

VOLUME ["/data"]
# 8080 is always the app's port — plain HTTP directly, or nginx's
# redirect-to-HTTPS listener once XCP_PULSE_ENABLE_HTTPS is set. 8443 only
# answers when that flag is on.
EXPOSE 8080 8443

# Checks nginx itself when built-in HTTPS is on, not just uvicorn behind it —
# a wedged nginx with a live uvicorn would otherwise still report healthy.
# ssl._create_unverified_context(): the healthcheck runs inside the same
# container as the (possibly self-signed) certificate it would otherwise have
# to trust, which proves nothing about whether a browser should trust it too.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "\
import os, ssl, sys, urllib.request; \
https = os.environ.get('XCP_PULSE_ENABLE_HTTPS', '').strip().lower() == 'true'; \
url = 'https://127.0.0.1:8443/healthz' if https else 'http://127.0.0.1:8080/healthz'; \
ctx = ssl._create_unverified_context() if https else None; \
sys.exit(0 if urllib.request.urlopen(url, timeout=4, context=ctx).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/xcp-pulse-entrypoint.sh"]
