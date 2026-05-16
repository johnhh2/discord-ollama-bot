FROM python:3.10-slim@sha256:cdbf8193cee2e31639ea8ea85ffdd8fa5cce98ee9abfde96ea5f329490048831 AS builder
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.lock

FROM python:3.10-slim@sha256:cdbf8193cee2e31639ea8ea85ffdd8fa5cce98ee9abfde96ea5f329490048831
WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends libcairo2 stockfish \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /install /usr/local
COPY src/ ./src/
COPY assets/ ./assets/
COPY migrations/ ./migrations/
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
