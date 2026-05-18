FROM python:3.14-slim@sha256:5b3879b6f3cb77e712644d50262d05a7c146b7312d784a18eff7ff5462e77033 AS builder
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.lock

FROM python:3.14-slim@sha256:5b3879b6f3cb77e712644d50262d05a7c146b7312d784a18eff7ff5462e77033
WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends libcairo2 stockfish ca-certificates curl xz-utils \
 && (apt-get install -y --no-install-recommends lc0 \
     || ( \
       curl -fsSL -o /tmp/lc0.tar.gz \
         "https://github.com/LeelaChessZero/lc0/releases/download/v0.31.2/lc0-v0.31.2-linux-cpu-eigen.tar.gz" \
       && tar -xzf /tmp/lc0.tar.gz -C /tmp \
       && install -m0755 /tmp/lc0 /usr/local/bin/lc0 \
       && rm -rf /tmp/lc0 /tmp/lc0.tar.gz \
     )) \
 && apt-get purge -y curl xz-utils \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*
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
