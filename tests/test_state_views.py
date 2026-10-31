"""The /state/* projections the MCP sidecar reads: JSON-safe, allowlisted."""
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

import src.health as health
from src import state, state_views


class FakeGuild:
    def __init__(self, gid: int, name: str):
        self.id = gid
        self.name = name
        self.member_count = 12
        self.text_channels = [object(), object()]
        self.voice_channels = [object()]


class FakeBot:
    def __init__(self, cog=None):
        self.latency = 0.042
        self.guilds = [FakeGuild(1, "Alpha"), FakeGuild(2, "Beta")]
        self._cog = cog

    def is_ready(self) -> bool:
        return True

    def get_cog(self, name):
        return self._cog


# Anything a user typed, or a discord object, goes under one of these keys
# somewhere in state; none of them may surface in a view.
_FORBIDDEN_KEYS = {"answer", "question", "history", "prompt", "system_prompt",
                   "condition", "message", "host", "target", "board", "token"}


def _keys(obj, out=None) -> set:
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k))
            _keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, out)
    return out


def test_overview_is_json_safe_and_counts_state():
    state.stats_commands_ran = 7
    state.stats_messages_seen = 40
    state.economy["users"]["1"] = {"balance": 5}
    state.idle_characters[1] = {10: {}, 11: {}}
    body = state_views.overview(FakeBot())
    json.dumps(body)
    assert body["ready"] is True
    assert body["discord_latency_ms"] == 42.0
    assert body["counters"]["commands_ran"] == 7
    assert body["counters"]["messages_seen"] == 40
    assert body["loaded"]["economy_users"] == 1
    assert body["loaded"]["idle_characters"] == 2
    assert [g["name"] for g in body["guilds"]] == ["Alpha", "Beta"]
    assert body["guilds"][0]["text_channels"] == 2


def test_overview_tolerates_no_bot():
    body = state_views.overview(None)
    assert body["ready"] is False
    assert body["discord_latency_ms"] is None
    assert body["guilds"] == []


def test_activity_reports_counts_and_channel_ids_only():
    # A puzzle carries its answer and a heist its host Member: neither may leak.
    state.active_puzzles[555] = {"question": "q?", "answer": "secret", "reward": 1, "user_id": 9}
    state.active_hangman_games[777] = {"word": "hidden", "board": object()}
    state.ai_threads[1] = {"kind": "story", "history": [{"role": "user", "content": "x"}]}
    state.ai_threads[2] = {"kind": "ask", "history": []}
    cog = SimpleNamespace(_active_heists={333: {"host": object(), "message": object()}}, _crime_active={1, 2})
    body = state_views.activity(FakeBot(cog))
    json.dumps(body)
    assert body["games"]["puzzles"] == {"count": 1, "channels": [555]}
    assert body["games"]["hangman"]["channels"] == [777]
    assert body["heists"] == {"count": 1, "channels": [333]}
    assert body["crimes_in_progress"] == 2
    assert body["ai_threads"] == {"count": 2, "by_kind": {"story": 1, "ask": 1}}
    assert not (_keys(body) & _FORBIDDEN_KEYS)
    assert "secret" not in json.dumps(body)


def test_activity_without_economy_cog():
    body = state_views.activity(FakeBot(cog=None))
    assert body["heists"] == {"count": 0, "channels": []}


@pytest.mark.asyncio
async def test_state_routes(monkeypatch):
    app = health.build_app(FakeBot())
    async with TestClient(TestServer(app)) as client:
        index = await client.get("/state")
        assert index.status == 200
        assert (await index.json())["views"] == ["activity", "overview"]
        resp = await client.get("/state/overview")
        assert resp.status == 200
        assert (await resp.json())["ready"] is True
        missing = await client.get("/state/secrets")
        assert missing.status == 404
