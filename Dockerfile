# XCP Pulse container image.
#
# Single stage: the dependency set is small and pure-Python wheels, so a
# builder stage would add complexity without saving meaningful size.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    XCP_PULSE_DATA_DIR=/data

WORKDIR /srv/xcp-pulse

# Dependencies first: this layer is rebuilt only when pyproject.toml changes,
# not on every source edit.
COPY pyproject.toml README.md LICENSE ./
COPY app/__init__.py ./app/
RUN pip install --no-cache-dir .

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
