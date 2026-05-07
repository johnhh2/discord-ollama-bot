"""Tests for the /healthz endpoint."""
import pytest
from aiohttp.test_utils import TestClient, TestServer

import src.health as health


class FakeBot:
    def __init__(self, ready: bool = True, latency: float = 0.05):
        self._ready = ready
        self.latency = latency

    def is_ready(self) -> bool:
        return self._ready


@pytest.fixture
def patch_db_and_ollama(monkeypatch):
    """Default: DB ok, Ollama ok. Tests override per-case."""
    async def db_ok():
        return ("ok", "")

    async def ollama_ok():
        return True

    monkeypatch.setattr(health, "_check_db", db_ok)
    monkeypatch.setattr(health.ai, "check_ollama_connected", ollama_ok)
    return monkeypatch


async def _request(bot, monkeypatch=None):
    app = health.build_app(bot)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_healthy_all_deps_ok(patch_db_and_ollama):
    status, body = await _request(FakeBot())
    assert status == 200
    assert body["status"] == "healthy"
    assert body["discord"] == "ok"
    assert body["db"] == "ok"
    assert body["ollama"] == "ok"


@pytest.mark.asyncio
async def test_degraded_when_ollama_down(patch_db_and_ollama):
    async def ollama_down():
        return False
    patch_db_and_ollama.setattr(health.ai, "check_ollama_connected", ollama_down)

    status, body = await _request(FakeBot())
    # Soft-fail on Ollama: still 200, body marks it degraded
    assert status == 200
    assert body["status"] == "degraded"
    assert body["discord"] == "ok"
    assert body["db"] == "ok"
    assert body["ollama"] == "down: unreachable"


@pytest.mark.asyncio
async def test_unhealthy_when_discord_not_ready(patch_db_and_ollama):
    status, body = await _request(FakeBot(ready=False))
    assert status == 503
    assert body["status"] == "unhealthy"
    assert body["discord"] == "down: not ready"
    assert body["db"] == "ok"


@pytest.mark.asyncio
async def test_unhealthy_when_discord_latency_infinite(patch_db_and_ollama):
    status, body = await _request(FakeBot(latency=float("inf")))
    assert status == 503
    assert body["discord"] == "down: no heartbeat"


@pytest.mark.asyncio
async def test_unhealthy_when_db_down(patch_db_and_ollama):
    async def db_down():
        return ("down", "OperationalError")
    patch_db_and_ollama.setattr(health, "_check_db", db_down)

    status, body = await _request(FakeBot())
    assert status == 503
    assert body["status"] == "unhealthy"
    assert body["db"] == "down: OperationalError"


@pytest.mark.asyncio
async def test_unhealthy_takes_priority_over_degraded(patch_db_and_ollama):
    """If both Ollama AND a hard dep are down, status is unhealthy, not degraded."""
    async def db_down():
        return ("down", "timeout")

    async def ollama_down():
        return False

    patch_db_and_ollama.setattr(health, "_check_db", db_down)
    patch_db_and_ollama.setattr(health.ai, "check_ollama_connected", ollama_down)

    status, body = await _request(FakeBot())
    assert status == 503
    assert body["status"] == "unhealthy"
    assert body["db"] == "down: timeout"
    assert body["ollama"] == "down: unreachable"


@pytest.mark.asyncio
async def test_db_check_times_out(monkeypatch):
    """If the DB probe hangs longer than DB_TIMEOUT_SECS, _check_db returns down."""
    import asyncio

    async def slow_pool():
        await asyncio.sleep(10)
        return None

    monkeypatch.setattr(health, "get_pool", slow_pool)
    monkeypatch.setattr(health, "DB_TIMEOUT_SECS", 0.05)

    code, reason = await health._check_db()
    assert code == "down"
    assert reason == "timeout"
