FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY src/ ./src/
COPY assets/ ./assets/
COPY migrations/ ./migrations/
COPY main.py ./

RUN useradd --system --uid 1001 --no-create-home bot \
    && mkdir -p /app/data \
    && chown -R bot:bot /app

ENV MPLCONFIGDIR=/tmp/matplotlib
USER bot

CMD ["python", "main.py"]
