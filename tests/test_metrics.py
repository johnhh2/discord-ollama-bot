"""Tests for src/metrics.py.

These check metric shape (names, labels, types) and that render() returns
valid Prometheus text-format. Hook integration (commands, ollama) is covered
by their own modules' tests; here we just verify the registry surface.
"""
from prometheus_client.parser import text_string_to_metric_families

from src import metrics


def _families() -> dict:
    body, _ = metrics.render()
    return {f.name: f for f in text_string_to_metric_families(body.decode())}


def test_render_returns_prometheus_text_format():
    body, content_type = metrics.render()
    assert body.startswith(b"#")
    assert content_type.startswith("text/plain")


def test_command_invocations_records_with_labels():
    metrics.command_invocations.labels(command="ask", outcome="ok").inc()
    metrics.command_invocations.labels(command="ask", outcome="error").inc(2)
    fams = _families()
    samples = {(s.labels["command"], s.labels["outcome"]): s.value
               for s in fams["bot_command_invocations"].samples
               if s.name == "bot_command_invocations_total"}
    assert samples[("ask", "ok")] >= 1
    assert samples[("ask", "error")] >= 2


def test_command_latency_uses_custom_buckets():
    """Default buckets stop at 10s; AI commands need 30/60/120s buckets."""
    metric = metrics.command_latency.labels(command="ask")
    metric.observe(0.05)
    metric.observe(45.0)  # would land in +Inf with default buckets
    fams = _families()
    bucket_samples = [s for s in fams["bot_command_latency_seconds"].samples
                      if s.name == "bot_command_latency_seconds_bucket"
                      and s.labels.get("command") == "ask"]
    bucket_le = {s.labels["le"] for s in bucket_samples}
    assert "60.0" in bucket_le
    assert "120.0" in bucket_le


def test_ollama_stream_seconds_labeled_by_outcome():
    metrics.ollama_stream_seconds.labels(outcome="complete").observe(2.0)
    metrics.ollama_stream_seconds.labels(outcome="timeout").observe(120.0)
    fams = _families()
    outcomes = {s.labels["outcome"]
                for s in fams["bot_ollama_stream_seconds"].samples
                if s.name == "bot_ollama_stream_seconds_count"}
    assert {"complete", "timeout"}.issubset(outcomes)


def test_ollama_stream_in_flight_is_a_gauge():
    metrics.ollama_stream_in_flight.set(3)
    fams = _families()
    sample = next(s for s in fams["bot_ollama_stream_in_flight"].samples
                  if s.name == "bot_ollama_stream_in_flight")
    assert sample.value == 3


def test_db_pool_gauges_zero_when_pool_uninitialized(monkeypatch):
    """Render should never raise even if the aiomysql pool hasn't been built."""
    from src import db
    monkeypatch.setattr(db, "_pool", None, raising=False)
    fams = _families()
    in_use = next(s.value for s in fams["bot_db_pool_connections_in_use"].samples
                  if s.name == "bot_db_pool_connections_in_use")
    size = next(s.value for s in fams["bot_db_pool_size"].samples
                if s.name == "bot_db_pool_size")
    assert in_use == 0
    assert size == 0


def test_db_pool_gauges_compute_in_use_from_size_minus_free(monkeypatch):
    class FakePool:
        size = 10
        freesize = 3
    from src import db
    monkeypatch.setattr(db, "_pool", FakePool(), raising=False)
    fams = _families()
    in_use = next(s.value for s in fams["bot_db_pool_connections_in_use"].samples
                  if s.name == "bot_db_pool_connections_in_use")
    size = next(s.value for s in fams["bot_db_pool_size"].samples
                if s.name == "bot_db_pool_size")
    assert in_use == 7
    assert size == 10
