"""Per-user input-token rate limit on AI calls (src/ai.py).

The bucket holds 2048 tokens max and refills at 512 / 60s. Token cost is
estimated at ~4 chars/token from the prompt content. stream_ollama gates
on this when given a user_id; user_id=None bypasses (used by internal
non-user-driven AI calls like roast/ragebait/quote-ranker).
"""
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import src.ai as _ai
import src.state as _state


# Async tests are individually marked below; sync helper tests are not.


@pytest.fixture(autouse=True)
def _reset():
    _state.bot_settings["ai_enabled"] = True
    _state.godmode_users.clear()
    _ai._user_token_buckets.clear()
    import asyncio
    _ai.ollama_semaphore = asyncio.Semaphore(1)
    yield
    _state.bot_settings.pop("ai_enabled", None)
    _ai._user_token_buckets.clear()


def test_estimate_tokens_uses_4_chars_per_token():
    msgs = [{"role": "user", "content": "x" * 400}]
    assert _ai._estimate_tokens(msgs) == 100


def test_estimate_tokens_min_one():
    assert _ai._estimate_tokens([{"role": "user", "content": ""}]) == 1


def test_check_token_budget_allows_first_call():
    assert _ai._check_token_budget(42, 100) is None
    tokens, _ = _ai._user_token_buckets[42]
    assert tokens == pytest.approx(_ai.TOKEN_BUCKET_MAX - 100)


def test_check_token_budget_denies_when_exhausted(monkeypatch):
    t = {"v": 1000.0}
    monkeypatch.setattr(_ai.time, "monotonic", lambda: t["v"])
    # Drain the bucket
    assert _ai._check_token_budget(7, _ai.TOKEN_BUCKET_MAX) is None
    # Next call: 0 tokens available, costs 512 → wait until refilled
    wait = _ai._check_token_budget(7, 512)
    assert wait is not None
    assert wait == pytest.approx(512 / _ai.TOKEN_BUCKET_REFILL_PER_SEC)


def test_check_token_budget_refills_over_time(monkeypatch):
    t = {"v": 1000.0}
    monkeypatch.setattr(_ai.time, "monotonic", lambda: t["v"])
    assert _ai._check_token_budget(9, _ai.TOKEN_BUCKET_MAX) is None
    # 60s later, 512 tokens have refilled.
    t["v"] += 60.0
    assert _ai._check_token_budget(9, 500) is None


def test_check_token_budget_caps_at_max(monkeypatch):
    t = {"v": 1000.0}
    monkeypatch.setattr(_ai.time, "monotonic", lambda: t["v"])
    assert _ai._check_token_budget(11, 100) is None
    # Idle a long time — bucket should not exceed TOKEN_BUCKET_MAX.
    t["v"] += 10_000.0
    assert _ai._check_token_budget(11, _ai.TOKEN_BUCKET_MAX) is None
    # And immediately spending another full bucket should fail.
    wait = _ai._check_token_budget(11, _ai.TOKEN_BUCKET_MAX)
    assert wait is not None and wait > 0


def test_godmode_user_bypasses_budget():
    _state.godmode_users.add(99)
    # Way over the cap — godmode still passes, bucket stays empty/untouched.
    assert _ai._check_token_budget(99, _ai.TOKEN_BUCKET_MAX * 10) is None
    assert 99 not in _ai._user_token_buckets


# ── Integration: stream_ollama with user_id gate ─────────────────────────────

class _FakeStreamingResponse:
    def __init__(self, chunks):
        self._chunks = chunks
        self.content = self

    def raise_for_status(self):
        pass

    def __aiter__(self):
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.post_calls = []

    def post(self, url, *, json=None, timeout=None):
        self.post_calls.append((url, json))
        return self._response


def _done_token():
    import json
    return (json.dumps({"message": {"content": "ok"}, "done": True}) + "\n").encode("utf-8")


@pytest.mark.asyncio
async def test_stream_ollama_skips_request_when_rate_limited(monkeypatch):
    """When user_id is given and the bucket is empty, stream_ollama edits
    the placeholder with a rate-limit notice and returns "" without POSTing."""
    t = {"v": 5000.0}
    monkeypatch.setattr(_ai.time, "monotonic", lambda: t["v"])
    # Pre-drain bucket for user 123.
    _ai._user_token_buckets[123] = (0.0, t["v"])

    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()
    session = _FakeSession(_FakeStreamingResponse([_done_token()]))

    big_msgs = [{"role": "user", "content": "x" * 4000}]  # ~1000 tokens
    result = await _ai.stream_ollama(session, big_msgs, placeholder, user_id=123)

    assert result == ""
    assert session.post_calls == []
    placeholder.edit.assert_awaited_once()
    # Embed should mention the rate limit.
    kwargs = placeholder.edit.await_args.kwargs
    embed = kwargs.get("embed")
    assert embed is not None
    assert "Rate Limit" in embed.title


@pytest.mark.asyncio
async def test_stream_ollama_no_user_id_bypasses_budget(monkeypatch):
    """user_id=None (internal calls like roast/ragebait/quote ranker) must
    not be gated."""
    t = {"v": 5000.0}
    monkeypatch.setattr(_ai.time, "monotonic", lambda: t["v"])
    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()
    session = _FakeSession(_FakeStreamingResponse([_done_token()]))

    big_msgs = [{"role": "user", "content": "x" * 100_000}]  # way over cap
    result = await _ai.stream_ollama(session, big_msgs, placeholder)

    assert result == "ok"
    assert len(session.post_calls) == 1


@pytest.mark.asyncio
async def test_stream_ollama_consumes_tokens_when_allowed(monkeypatch):
    t = {"v": 5000.0}
    monkeypatch.setattr(_ai.time, "monotonic", lambda: t["v"])
    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()
    session = _FakeSession(_FakeStreamingResponse([_done_token()]))

    msgs = [{"role": "user", "content": "x" * 400}]  # ~100 tokens
    result = await _ai.stream_ollama(session, msgs, placeholder, user_id=55)

    assert result == "ok"
    tokens, _ = _ai._user_token_buckets[55]
    assert tokens == pytest.approx(_ai.TOKEN_BUCKET_MAX - 100)
