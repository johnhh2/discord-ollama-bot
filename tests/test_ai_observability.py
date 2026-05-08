"""Correlation IDs and graceful-shutdown drain.

Covers two adjacent behaviors added together:

1. stream_ollama / respond emit structured log lines tagged with a stable
   request_id, so every line of one !ask invocation can be grepped together.
2. ai.drain_in_flight blocks until the in-flight set is empty or the timeout
   expires, so the SIGTERM path in src/core.py can wait for streams to finish
   before tearing down the DB pool.
"""
import asyncio
import json
import logging

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import src.ai as _ai
import src.state as _state


pytestmark = pytest.mark.asyncio


def _make_chunks(*lines: str) -> list[bytes]:
    return [(line + "\n").encode("utf-8") for line in lines]


def _ollama_token(text: str, *, done: bool = False) -> str:
    return json.dumps({"message": {"content": text}, "done": done})


class _FakeStreamingResponse:
    def __init__(self, chunks: list[bytes]):
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

    def post(self, url, *, json=None, timeout=None):
        return self._response


@pytest.fixture(autouse=True)
def _reset_ai_state():
    _state.bot_settings["ai_enabled"] = True
    _ai.ollama_semaphore = asyncio.Semaphore(1)
    _ai._in_flight.clear()
    yield
    _state.bot_settings.pop("ai_enabled", None)
    _ai._in_flight.clear()


# ── Correlation IDs ───────────────────────────────────────────────────────────

async def test_stream_ollama_logs_started_and_complete_with_same_request_id(caplog):
    """One stream produces ollama_stream_started and ollama_stream_complete,
    both tagged with the same request_id."""
    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()
    response = _FakeStreamingResponse(_make_chunks(_ollama_token("hi", done=True)))
    session = _FakeSession(response)

    with caplog.at_level(logging.INFO, logger="src.ai"):
        await _ai.stream_ollama(session, [], placeholder, request_id="abc123")

    msgs = {r.message: r for r in caplog.records if r.name == "src.ai"}
    assert "ollama_stream_started" in msgs
    assert "ollama_stream_complete" in msgs
    assert msgs["ollama_stream_started"].request_id == "abc123"
    assert msgs["ollama_stream_complete"].request_id == "abc123"
    # Complete includes elapsed_ms and response_chars for histogram-style queries.
    assert hasattr(msgs["ollama_stream_complete"], "elapsed_ms")
    assert msgs["ollama_stream_complete"].response_chars == 2


async def test_stream_ollama_auto_generates_request_id_when_missing(caplog):
    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()
    response = _FakeStreamingResponse(_make_chunks(_ollama_token("x", done=True)))
    session = _FakeSession(response)

    with caplog.at_level(logging.INFO, logger="src.ai"):
        await _ai.stream_ollama(session, [], placeholder)

    rid = next(r.request_id for r in caplog.records if r.message == "ollama_stream_started")
    assert rid and len(rid) == 12  # new_request_id() returns 12-char hex


async def test_stream_ollama_logs_token_budget_denial_with_request_id(caplog):
    """Per-user budget denial logs ai_token_budget_denied with request_id +
    user_id so a denied request still threads in audits."""
    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()
    session = _FakeSession(_FakeStreamingResponse([]))

    # Bucket exists but is depleted: previously charged tokens leave 0 remaining.
    _ai._user_token_buckets[42] = (0.0, _ai.time.monotonic())
    big = [{"role": "user", "content": "x" * 4000}]

    with caplog.at_level(logging.INFO, logger="src.ai"):
        result = await _ai.stream_ollama(session, big, placeholder, user_id=42, request_id="rid42")

    assert result == ""
    denial = next(r for r in caplog.records if r.message == "ai_token_budget_denied")
    assert denial.request_id == "rid42"
    assert denial.user_id == 42


# ── In-flight tracking + drain ────────────────────────────────────────────────

async def test_drain_in_flight_returns_zero_when_idle():
    drained, remaining = await _ai.drain_in_flight(timeout=0.5)
    assert (drained, remaining) == (0, 0)


async def test_drain_in_flight_waits_for_running_request():
    """Mark a request in-flight, finish it after a short delay, and verify
    drain returns once the set empties (well within the timeout)."""
    _ai._in_flight.add("rid-a")

    async def _finish_soon():
        await asyncio.sleep(0.2)
        _ai._in_flight.discard("rid-a")

    asyncio.create_task(_finish_soon())
    drained, remaining = await _ai.drain_in_flight(timeout=2.0)
    assert drained == 1
    assert remaining == 0


async def test_drain_in_flight_times_out_when_request_hangs():
    """If a request never finishes, drain returns after the timeout with
    remaining > 0 so the caller can log how many got cut off."""
    _ai._in_flight.add("rid-stuck")
    drained, remaining = await _ai.drain_in_flight(timeout=0.3)
    assert drained == 1
    assert remaining == 1
    # Cleanup so other tests don't see this leftover.
    _ai._in_flight.discard("rid-stuck")


async def test_stream_ollama_clears_in_flight_after_completion():
    """The in-flight set must shrink back to empty after a normal stream."""
    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()
    response = _FakeStreamingResponse(_make_chunks(_ollama_token("x", done=True)))
    session = _FakeSession(response)

    await _ai.stream_ollama(session, [], placeholder, request_id="rid-cleanup")
    assert "rid-cleanup" not in _ai._in_flight
    assert len(_ai._in_flight) == 0


async def test_stream_ollama_clears_in_flight_after_error():
    """Even when the AI is disabled and the function returns "" early, the
    in-flight set must not leak the request_id."""
    _state.bot_settings["ai_enabled"] = False
    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()
    session = _FakeSession(_FakeStreamingResponse([]))

    await _ai.stream_ollama(session, [], placeholder, request_id="rid-err")
    assert "rid-err" not in _ai._in_flight
