# Build context is botmcp/ (see docker-compose.yml). Its own image and lock:
# the bot's image never carries the MCP SDK, and this one never carries the
# Discord token.
FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217 AS builder
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.lock

FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217
WORKDIR /app
# The base image is pinned by digest, so `upgrade` is what brings in Debian's
# security fixes between bumps — CI's Trivy scan fails on the stale ones.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /install /usr/local
COPY . ./botmcp/
RUN useradd --system --uid 1002 --no-create-home botmcp
ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER botmcp
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % (os.environ.get('MCP_PORT') or 8765), timeout=2).status == 200 else 1)" || exit 1
CMD ["python", "-m", "botmcp"]
