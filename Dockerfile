FROM python:3.10-slim@sha256:cdbf8193cee2e31639ea8ea85ffdd8fa5cce98ee9abfde96ea5f329490048831 AS builder
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.lock

# ── lc0 build stage ──────────────────────────────────────────────────────────
# lc0 isn't packaged in Debian apt and the project doesn't publish Linux
# binaries (only Windows/macOS/Android). Build from source with the OpenBLAS
# CPU backend (no GPU/CUDA). Pinned to a specific tag for reproducibility.
FROM python:3.10-slim@sha256:cdbf8193cee2e31639ea8ea85ffdd8fa5cce98ee9abfde96ea5f329490048831 AS lc0-builder
ARG LC0_VERSION=v0.31.2
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates build-essential gcc g++ \
      zlib1g-dev libopenblas-dev ninja-build python3-pip \
 && pip install --no-cache-dir meson \
 && git clone --branch ${LC0_VERSION} --depth 1 --recurse-submodules \
      https://github.com/LeelaChessZero/lc0.git /tmp/lc0 \
 && cd /tmp/lc0 \
 && INSTALL_PREFIX=/usr/local ./build.sh \
 && rm -rf /tmp/lc0

# ── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.10-slim@sha256:cdbf8193cee2e31639ea8ea85ffdd8fa5cce98ee9abfde96ea5f329490048831
WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends libcairo2 stockfish libopenblas0 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=lc0-builder /usr/local/bin/lc0 /usr/local/bin/lc0
COPY --from=builder /install /usr/local
COPY src/ ./src/
COPY assets/ ./assets/
COPY migrations/ ./migrations/
COPY maia_weights/ ./maia_weights/
COPY main.py ./
RUN useradd --system --uid 1001 --no-create-home bot
ENV HOME=/tmp \
    MPLCONFIGDIR=/tmp/matplotlib \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER bot
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9090/healthz', timeout=2).status == 200 else 1)" || exit 1
CMD ["python", "main.py"]
