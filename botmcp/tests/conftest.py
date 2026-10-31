"""Sidecar tests run against the bot's SQLite schema fake (tests/fakes/db.py),
built from the real migrations, so the catalog is checked against the live
schema and every tool's SQL is actually executed. The bot's /state views
are stubbed at `state_client.fetch_json`."""
import sys
from pathlib import Path

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.fakes.db import make_fake_pool  # noqa: E402

from botmcp import db as _db  # noqa: E402
from botmcp import state_client  # noqa: E402


@pytest_asyncio.fixture
async def pool(monkeypatch):
    fake = await make_fake_pool()

    async def _get_pool():
        return fake

    monkeypatch.setattr(_db, "get_pool", _get_pool)
    try:
        yield fake
    finally:
        await fake.close()


async def seed(pool, sql: str, params=()):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)


OVERVIEW = {
    "version": "abc123",
    "ready": True,
    "uptime_seconds": 120,
    "guilds": [{"id": 1, "name": "Alpha", "member_count": 10, "text_channels": 3, "voice_channels": 1}],
    "counters": {"commands_ran": 5},
    "ai": {"enabled": True, "streams_in_flight": 0},
    "minecraft": {"online": True, "ping_ms": 12.5},
}
ACTIVITY = {"games": {"chess": {"count": 1, "channels": [42]}}, "heists": {"count": 0, "channels": []}}


@pytest.fixture
def bot_state(monkeypatch):
    """Stub the bot's loopback views; tests mutate the dicts to vary them."""
    views = {"/state/overview": dict(OVERVIEW), "/state/activity": dict(ACTIVITY),
             "/healthz": {"status": "healthy", "discord": "ok", "db": "ok", "ollama": "ok"}}

    async def fetch(path):
        if path not in views:
            raise RuntimeError("down")
        return views[path]

    monkeypatch.setattr(state_client, "fetch_json", fetch)

    async def health():
        return views["/healthz"]

    monkeypatch.setattr(state_client, "health", health)
    return views
