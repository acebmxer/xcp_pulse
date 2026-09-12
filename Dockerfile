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
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

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

# Run as a non-root user. The data volume is chowned so the container can write
# bundles and the SQLite database to it.
RUN useradd --system --create-home --uid 10001 pulse \
    && mkdir -p /data \
    && chown -R pulse:pulse /data /srv/xcp-pulse
USER pulse

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
