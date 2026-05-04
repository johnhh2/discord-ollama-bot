"""stream_ollama: chunk parsing, edit cadence, error handling.

stream_ollama (src/ai.py) is the function that talks to the Ollama HTTP
endpoint. It POSTs a JSON payload with `stream: True`, then async-iterates
over `response.content` line-by-line, parsing each as a JSON object,
extracting the `message.content` token, and updating a placeholder
Discord message every ~0.8s.

This is the lowest-level production path that's normally only verified by
running against a real Ollama server. These tests mock aiohttp's response
object well enough to exercise the chunk loop, the EDIT_INTERVAL throttling,
the `done` terminator, and HTTPException swallowing.

What we DON'T test (out of scope, pure-real-world):
- The actual aiohttp ClientSession lifecycle (covered by aiohttp's own tests)
- Discord rate-limit backoff on placeholder.edit (handled by discord.py)
- Real Ollama JSON shape changes (it's an external service)
"""
import json
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import src.ai as _ai
import src.state as _state


pytestmark = pytest.mark.asyncio


def _make_chunks(*lines: str) -> list[bytes]:
    """Each chunk is one byte-string ending with \\n, like Ollama emits."""
    return [(l + "\n").encode("utf-8") for l in lines]


def _ollama_token(text: str, *, done: bool = False) -> str:
    """Build one Ollama-format streaming line."""
    return json.dumps({"message": {"content": text}, "done": done})


class _FakeStreamingResponse:
    """Mimics aiohttp's response context manager + .content async iterator."""
    def __init__(self, chunks: list[bytes], *, raise_for_status_exc: Exception = None):
        self._chunks = chunks
        self._raise_for_status_exc = raise_for_status_exc
        self.content = self  # `async for raw_line in resp.content`

    def raise_for_status(self):
        if self._raise_for_status_exc is not None:
            raise self._raise_for_status_exc

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

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    """Mimics aiohttp.ClientSession's `.post(url, json=...)` returning a
    response context manager. We don't bother with __aenter__/__aexit__
    on the session itself because stream_ollama doesn't use it that way."""
    def __init__(self, response: _FakeStreamingResponse):
        self._response = response
        self.post_calls: list[tuple[str, dict]] = []

    def post(self, url: str, *, json=None):
        self.post_calls.append((url, json))
        return self._response


@pytest.fixture(autouse=True)
def _reset_ai_enabled():
    """stream_ollama short-circuits if state.bot_settings["ai_enabled"] is
    False. Default to True so tests don't all hit that branch."""
    _state.bot_settings["ai_enabled"] = True
    yield
    _state.bot_settings.pop("ai_enabled", None)


@pytest.fixture(autouse=True)
def _reset_semaphore():
    """The module-level ollama_semaphore is async.Semaphore(1). Tests
    acquire it; if a previous test left it acquired, the next would block.
    Replace with a fresh semaphore per test."""
    import asyncio
    _ai.ollama_semaphore = asyncio.Semaphore(1)


# ── Happy path: token assembly ────────────────────────────────────────────────

async def test_stream_ollama_assembles_tokens_into_full_response():
    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()

    response = _FakeStreamingResponse(_make_chunks(
        _ollama_token("Hello"),
        _ollama_token(", "),
        _ollama_token("world"),
        _ollama_token("!", done=True),
    ))
    session = _FakeSession(response)

    full = await _ai.stream_ollama(session, [{"role": "user", "content": "hi"}], placeholder)

    assert full == "Hello, world!"
    # Exactly one POST to /api/chat with stream=True.
    assert len(session.post_calls) == 1
    url, payload = session.post_calls[0]
    assert url.endswith("/api/chat")
    assert payload["stream"] is True
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


async def test_stream_ollama_skips_blank_and_unparseable_lines():
    """Real Ollama can emit empty lines or partial-write garbage; the loop
    must skip them, not crash."""
    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()

    response = _FakeStreamingResponse(_make_chunks(
        _ollama_token("good "),
        "",                      # blank line
        "{not valid json",       # garbage
        _ollama_token("token"),
        _ollama_token("", done=True),
    ))
    session = _FakeSession(response)

    full = await _ai.stream_ollama(session, [], placeholder)

    assert full == "good token"


async def test_stream_ollama_breaks_on_done_token():
    """Even if more chunks would have come after `done`, the loop exits."""
    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()

    response = _FakeStreamingResponse(_make_chunks(
        _ollama_token("first "),
        _ollama_token("done", done=True),
        _ollama_token("UNREACHABLE"),  # would corrupt full_response if read
    ))
    session = _FakeSession(response)

    full = await _ai.stream_ollama(session, [], placeholder)

    assert full == "first done"
    assert "UNREACHABLE" not in full


# ── EDIT_INTERVAL debouncing ──────────────────────────────────────────────────

async def test_stream_ollama_edits_placeholder_with_cursor(monkeypatch):
    """The placeholder gets edited with `<partial>▌` while streaming.
    With time.monotonic mocked to advance past EDIT_INTERVAL on every
    token, every token triggers an edit."""
    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()

    # Make every token cross the 0.8s threshold.
    counter = {"t": 0.0}
    def _fast_clock():
        counter["t"] += 1.0  # always >= EDIT_INTERVAL (0.8)
        return counter["t"]
    monkeypatch.setattr(_ai.time, "monotonic", _fast_clock)

    response = _FakeStreamingResponse(_make_chunks(
        _ollama_token("a"),
        _ollama_token("b"),
        _ollama_token("c", done=True),
    ))
    session = _FakeSession(response)

    await _ai.stream_ollama(session, [], placeholder)

    # Each non-empty token triggered a placeholder.edit with cursor.
    edits = placeholder.edit.await_args_list
    assert len(edits) >= 2  # at least 2 mid-stream edits (the third is post-done)
    # The cursor character ▌ appears in mid-stream edits.
    sample = edits[0].kwargs.get("content") or (edits[0].args[0] if edits[0].args else "")
    assert "▌" in sample


async def test_stream_ollama_throttles_edits_when_clock_doesnt_advance(monkeypatch):
    """If time.monotonic() always returns the same value, the EDIT_INTERVAL
    gate keeps placeholder.edit from being called for each token. This
    catches Discord rate-limit regressions."""
    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()

    monkeypatch.setattr(_ai.time, "monotonic", lambda: 1000.0)

    response = _FakeStreamingResponse(_make_chunks(
        _ollama_token("a"),
        _ollama_token("b"),
        _ollama_token("c"),
        _ollama_token("d"),
        _ollama_token("e", done=True),
    ))
    session = _FakeSession(response)

    await _ai.stream_ollama(session, [], placeholder)

    # Clock never advanced past EDIT_INTERVAL, so no mid-stream edit fired.
    # (The very first edit gates on `now - last_edit >= EDIT_INTERVAL` and
    # last_edit starts at 0.0; with `now=1000.0`, the gate IS satisfied
    # once. So we expect <=1 edits.)
    assert placeholder.edit.await_count <= 1


# ── Error paths ───────────────────────────────────────────────────────────────

async def test_stream_ollama_returns_empty_when_ai_disabled():
    """If state.bot_settings["ai_enabled"] is False, stream_ollama edits
    the placeholder with an "AI Offline" embed and returns "" without
    making an HTTP call."""
    _state.bot_settings["ai_enabled"] = False

    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()
    session = _FakeSession(_FakeStreamingResponse([]))

    result = await _ai.stream_ollama(session, [], placeholder)

    assert result == ""
    # No POST happened.
    assert session.post_calls == []
    # Placeholder was edited with an offline embed.
    placeholder.edit.assert_awaited_once()


async def test_stream_ollama_swallows_http_exception_on_placeholder_edit(monkeypatch):
    """When Discord returns 429 or similar on placeholder.edit, the
    exception is swallowed so the rest of the stream keeps going."""
    placeholder = MagicMock(spec=discord.Message)
    # First edit raises HTTPException; subsequent calls succeed (we just
    # don't retry the failed one).
    edit_calls = {"count": 0}
    async def _flaky_edit(content=None, **kw):
        edit_calls["count"] += 1
        if edit_calls["count"] == 1:
            raise discord.HTTPException(MagicMock(status=429), "rate limited")
    placeholder.edit = AsyncMock(side_effect=_flaky_edit)

    # Force the EDIT_INTERVAL gate open every time so we attempt edits.
    counter = {"t": 0.0}
    def _fast_clock():
        counter["t"] += 1.0
        return counter["t"]
    monkeypatch.setattr(_ai.time, "monotonic", _fast_clock)

    response = _FakeStreamingResponse(_make_chunks(
        _ollama_token("first"),
        _ollama_token(" token"),
        _ollama_token(" final", done=True),
    ))
    session = _FakeSession(response)

    full = await _ai.stream_ollama(session, [], placeholder)

    # Stream completed despite the first edit failing.
    assert full == "first token final"


# ── Model resolution ──────────────────────────────────────────────────────────

async def test_stream_ollama_uses_explicit_model_when_given():
    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()
    response = _FakeStreamingResponse(_make_chunks(_ollama_token("x", done=True)))
    session = _FakeSession(response)

    await _ai.stream_ollama(session, [], placeholder, model="llama3:8b")

    payload = session.post_calls[0][1]
    assert payload["model"] == "llama3:8b"


async def test_stream_ollama_falls_back_to_default_model_when_no_guild():
    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()
    response = _FakeStreamingResponse(_make_chunks(_ollama_token("x", done=True)))
    session = _FakeSession(response)

    await _ai.stream_ollama(session, [], placeholder)

    # Default OLLAMA_MODEL from src.config.
    payload = session.post_calls[0][1]
    assert payload["model"] == _ai.OLLAMA_MODEL


# ── Long-response truncation in placeholder edits ────────────────────────────

async def test_stream_ollama_truncates_placeholder_to_fit_2000_char_limit(monkeypatch):
    """Discord message content cap is 2000 chars. The intermediate edits
    use [-1997:] + cursor to stay under the cap. Catches off-by-one
    regressions."""
    placeholder = MagicMock(spec=discord.Message)
    placeholder.edit = AsyncMock()

    # Force every token to trigger an edit so we see a long-content edit.
    counter = {"t": 0.0}
    monkeypatch.setattr(_ai.time, "monotonic", lambda: counter.__setitem__("t", counter["t"] + 1.0) or counter["t"])

    big_token = "x" * 2500
    response = _FakeStreamingResponse(_make_chunks(
        _ollama_token(big_token),
        _ollama_token("", done=True),
    ))
    session = _FakeSession(response)

    full = await _ai.stream_ollama(session, [], placeholder)

    # Final return value is the full untruncated string.
    assert len(full) == 2500
    # Every mid-stream edit content stays <= 2000 chars (1997 + cursor).
    for call in placeholder.edit.await_args_list:
        content = call.kwargs.get("content") or (call.args[0] if call.args else "")
        if content and "▌" in content:
            assert len(content) <= 1998, f"placeholder edit was {len(content)} chars (> 1998)"
