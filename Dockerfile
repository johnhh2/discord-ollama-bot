FROM python:3.10-slim@sha256:cdbf8193cee2e31639ea8ea85ffdd8fa5cce98ee9abfde96ea5f329490048831 AS builder
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.lock

FROM python:3.10-slim@sha256:cdbf8193cee2e31639ea8ea85ffdd8fa5cce98ee9abfde96ea5f329490048831
WORKDIR /app
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
CMD ["python", "main.py"]
