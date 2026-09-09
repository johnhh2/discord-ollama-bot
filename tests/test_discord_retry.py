"""Transient Discord 503s: the bot's own send paths retry with an increasing
delay (1 s, 3 s), never when an attachment is present, and a 503 that
survives the retries reads as a Discord hiccup in on_command_error — no
audit-log entry, no "⚠️ Command Error" report, no re-raise."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import discord.ext.commands
import pytest

from src import discord_retry
from src import state as _state
from src.core import SilentContext
from src.discord_retry import retry_on_503, send_dm, send_with_retry


_aio = pytest.mark.asyncio


def _server_error(status: int = 503) -> discord.DiscordServerError:
    resp = SimpleNamespace(status=status, reason="Service Unavailable")
    return discord.DiscordServerError(
        resp, "upstream connect error or disconnect/reset before headers",
    )


@pytest.fixture
def sleeps(monkeypatch):
    """Stub the retry wait; returns the list of delays requested."""
    seen: list[float] = []

    async def _fake_sleep(delay):
        seen.append(delay)

    monkeypatch.setattr(discord_retry, "_sleep", _fake_sleep)
    return seen


# ── retry_on_503 ─────────────────────────────────────────────────────────────

@_aio
async def test_retry_succeeds_on_second_attempt_after_one_second(sleeps):
    op = AsyncMock(side_effect=[_server_error(503), "ok"])
    assert await retry_on_503(op) == "ok"
    assert op.await_count == 2
    assert sleeps == [1.0]


@_aio
async def test_retry_delays_increase_then_gives_up(sleeps):
    """Three attempts, sleeping 1 s then 3 s between them; the last 503 propagates."""
    op = AsyncMock(side_effect=[_server_error(503)] * 5)
    with pytest.raises(discord.DiscordServerError) as exc_info:
        await retry_on_503(op)
    assert exc_info.value.status == 503
    assert op.await_count == 3
    assert sleeps == [1.0, 3.0]


@_aio
async def test_non_503_server_error_is_not_retried(sleeps):
    """500/502/504 are already retried five times inside discord.py — don't stack."""
    op = AsyncMock(side_effect=[_server_error(500), "ok"])
    with pytest.raises(discord.DiscordServerError):
        await retry_on_503(op)
    assert op.await_count == 1
    assert sleeps == []


@_aio
async def test_client_errors_pass_straight_through(sleeps):
    """A 4xx (Forbidden here) is the bot's own problem; retrying would repeat it."""
    resp = SimpleNamespace(status=403, reason="Forbidden")
    op = AsyncMock(side_effect=[discord.Forbidden(resp, "Cannot send messages to this user")])
    with pytest.raises(discord.Forbidden):
        await retry_on_503(op)
    assert op.await_count == 1
    assert sleeps == []


@_aio
async def test_retry_calls_op_fresh_each_attempt(sleeps):
    """`op` is a factory: each attempt must produce a new awaitable, never
    re-await the first (a coroutine can only be awaited once)."""
    calls = 0

    async def _send():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _server_error(503)
        return calls

    assert await retry_on_503(_send) == 2
    assert calls == 2


# ── send_with_retry / attachments ────────────────────────────────────────────

@_aio
async def test_send_with_attachment_is_not_retried(sleeps):
    """discord.py closes File objects when a send returns, so a second call
    with the same File would fail on closed handles — skip the retry."""
    sender = AsyncMock(side_effect=[_server_error(503), "ok"])
    with pytest.raises(discord.DiscordServerError):
        await send_with_retry(sender, "board", what="test", file=MagicMock())
    assert sender.await_count == 1
    assert sleeps == []


@_aio
async def test_send_with_files_list_is_not_retried(sleeps):
    sender = AsyncMock(side_effect=[_server_error(503), "ok"])
    with pytest.raises(discord.DiscordServerError):
        await send_with_retry(sender, None, what="test", files=[MagicMock()])
    assert sender.await_count == 1
    assert sleeps == []


@_aio
async def test_send_without_attachment_retries_with_same_kwargs(sleeps):
    sender = AsyncMock(side_effect=[_server_error(503), "ok"])
    embed = MagicMock()
    assert await send_with_retry(sender, "hi", what="test", embed=embed, silent=True) == "ok"
    assert sender.await_count == 2
    for call in sender.await_args_list:
        args, kwargs = call
        assert args == ("hi",)
        assert kwargs == {"embed": embed, "silent": True}
    assert sleeps == [1.0]


# ── SilentContext.send ───────────────────────────────────────────────────────

@_aio
async def test_ctx_send_retries_503_and_stays_silent(sleeps):
    ctx = SilentContext.__new__(SilentContext)
    with patch("discord.ext.commands.Context.send",
               new=AsyncMock(side_effect=[_server_error(503), "sent"])) as send:
        assert await SilentContext.send(ctx, "hello") == "sent"
    assert send.await_count == 2
    for call in send.await_args_list:
        assert call.kwargs.get("silent") is True
    assert sleeps == [1.0]


@_aio
async def test_ctx_send_with_file_does_not_retry(sleeps):
    ctx = SilentContext.__new__(SilentContext)
    with patch("discord.ext.commands.Context.send",
               new=AsyncMock(side_effect=[_server_error(503), "sent"])) as send:
        with pytest.raises(discord.DiscordServerError):
            await SilentContext.send(ctx, file=MagicMock())
    assert send.await_count == 1
    assert sleeps == []


# ── send_dm ──────────────────────────────────────────────────────────────────

@_aio
async def test_send_dm_retries_503(sleeps):
    user = SimpleNamespace(id=42, send=AsyncMock(side_effect=[_server_error(503), "dm"]))
    embed = MagicMock()
    assert await send_dm(user, embed=embed) == "dm"
    assert user.send.await_count == 2
    assert user.send.await_args_list[-1].kwargs == {"embed": embed}
    assert sleeps == [1.0]


@_aio
async def test_send_dm_keeps_keyword_only_call_shape(sleeps):
    """`send_dm(user, embed=e)` must call `user.send(embed=e)` — not
    `user.send(None, embed=e)` — so a keyword-only send still works."""
    seen = []

    class _User:
        id = 42

        async def send(self, *, embed=None, **kw):
            seen.append((embed, kw))
            return "dm"

    embed = MagicMock()
    assert await send_dm(_User(), embed=embed) == "dm"
    assert seen == [(embed, {})]


@_aio
async def test_send_dm_forbidden_propagates_unchanged(sleeps):
    """DMs closed must still raise Forbidden so the call sites' except clauses run."""
    resp = SimpleNamespace(status=403, reason="Forbidden")
    user = SimpleNamespace(id=42, send=AsyncMock(side_effect=[discord.Forbidden(resp, "closed")]))
    with pytest.raises(discord.Forbidden):
        await send_dm(user, embed=MagicMock())
    assert user.send.await_count == 1
    assert sleeps == []


# ── on_command_error ─────────────────────────────────────────────────────────

def _ctx(send):
    return SimpleNamespace(
        guild=SimpleNamespace(id=1),
        author=SimpleNamespace(display_name="Xeph", id=7),
        message=SimpleNamespace(content="!daily"),
        command=SimpleNamespace(qualified_name="daily"),
        send=send,
    )


@_aio
async def test_command_error_503_is_a_hiccup_not_a_bug_report(monkeypatch):
    import src.events as _events
    cog = _events.EventsCog(bot=SimpleNamespace())
    logged = []

    async def _spy_log(bot, ctx, error):
        logged.append(error)

    monkeypatch.setattr(_events, "_log_command_error", _spy_log)
    _state.audit_log.clear()
    sent = []

    async def _send(*args, **kwargs):
        sent.append((args, kwargs))

    err = discord.ext.commands.CommandInvokeError(_server_error(503))
    await cog.on_command_error(_ctx(_send), err)  # must not raise

    assert logged == []
    assert len(_state.audit_log) == 0
    assert len(sent) == 1
    assert "try again" in sent[0][0][0]
    assert sent[0][1].get("silent") is False


@_aio
async def test_command_error_503_reply_failure_is_swallowed(monkeypatch):
    """If Discord is still down, the hiccup reply fails too — that must not
    become a second error out of the handler."""
    import src.events as _events
    cog = _events.EventsCog(bot=SimpleNamespace())
    monkeypatch.setattr(_events, "_log_command_error", AsyncMock())
    _state.audit_log.clear()

    err = discord.ext.commands.CommandInvokeError(_server_error(503))
    await cog.on_command_error(_ctx(AsyncMock(side_effect=_server_error(503))), err)
    assert len(_state.audit_log) == 0


@_aio
async def test_command_error_other_5xx_still_reports(monkeypatch):
    """A 500 that survived discord.py's own five retries is not a blip we
    understand — keep the bug report and the re-raise."""
    import src.events as _events
    cog = _events.EventsCog(bot=SimpleNamespace())
    logged = []

    async def _spy_log(bot, ctx, error):
        logged.append(error)

    monkeypatch.setattr(_events, "_log_command_error", _spy_log)
    _state.audit_log.clear()

    err = discord.ext.commands.CommandInvokeError(_server_error(500))
    with pytest.raises(discord.ext.commands.CommandInvokeError):
        await cog.on_command_error(_ctx(AsyncMock()), err)
    assert logged == [err]
    assert len(_state.audit_log) == 1
