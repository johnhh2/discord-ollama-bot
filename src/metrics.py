"""Prometheus metrics for the bot.

Cardinality is the single thing to watch with Prometheus labels — every
unique label combination becomes a separate timeseries in storage. Keep
labels to bounded sets (command names, fixed outcomes). Never label by
user_id or guild_id.

Metrics live on a dedicated CollectorRegistry rather than the global default
so tests can build a fresh registry per case and avoid the "duplicated
timeseries" error you get from re-registering on the default one.
"""
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

# Latency buckets tuned for Discord command response times: the bulk of
# commands return in 10-500 ms, AI commands in 1-30 s, with a long tail
# from network/DB hiccups. The default buckets stop at 10 s which would
# crush all AI calls into the +Inf bucket.
_LATENCY_BUCKETS = (
    0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0,
)

command_invocations = Counter(
    "bot_command_invocations_total",
    "Discord commands invoked, by qualified name and outcome.",
    labelnames=("command", "outcome"),
    registry=REGISTRY,
)

command_latency = Histogram(
    "bot_command_latency_seconds",
    "End-to-end command handler wall-clock latency.",
    labelnames=("command",),
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)

ollama_stream_seconds = Histogram(
    "bot_ollama_stream_seconds",
    "Ollama streaming response duration, by outcome.",
    labelnames=("outcome",),
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)

ollama_stream_in_flight = Gauge(
    "bot_ollama_stream_in_flight",
    "AI streams currently being processed.",
    registry=REGISTRY,
)

db_pool_in_use = Gauge(
    "bot_db_pool_connections_in_use",
    "aiomysql connections checked out from the pool right now.",
    registry=REGISTRY,
)

db_pool_size = Gauge(
    "bot_db_pool_size",
    "Total aiomysql connections (free + in-use) currently held by the pool.",
    registry=REGISTRY,
)


def _refresh_db_pool_gauges() -> None:
    """Sample the aiomysql pool on /metrics scrape. No-op if the pool isn't built yet."""
    try:
        from src import db
        pool = db._pool  # type: ignore[attr-defined]
    except Exception:
        pool = None
    if pool is None:
        db_pool_in_use.set(0)
        db_pool_size.set(0)
        return
    size = getattr(pool, "size", 0) or 0
    free = getattr(pool, "freesize", 0) or 0
    db_pool_in_use.set(max(0, size - free))
    db_pool_size.set(size)


def render() -> tuple[bytes, str]:
    """Snapshot the registry as Prometheus text-format. Returns (body, content_type)."""
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    _refresh_db_pool_gauges()
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
