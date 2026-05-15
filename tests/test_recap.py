"""Coverage for !recap — the daily server-activity summarizer.

The command has three parts worth testing:
  1. The per-user/per-guild/per-day cap (gate-and-claim against
     state.recap_usage, persisted via save_recap_usage).
  2. Message collection from @everyone-visible, non-NSFW channels.
  3. The Ollama call + finalize.

These tests focus on (1) and (2). The Ollama call is stubbed at the
`stream_ollama` seam — same strategy as test_ai_thread_flow.py's !tldr /
!rpg tests. `finalize` and `keep_typing` are stubbed so nothing reaches
Discord's API or spawns a stray typing task.

`save_recap_usage` is captured by ai_cog at import time via
`from src.persistence import save_recap_usage`, so it's patched directly
on the ai_cog module (the conftest patch on src.persistence wouldn't
reach the bound reference).
"""

import datetime

import pytest

import src.state as _state
import src.cogs.ai_cog as _ai_cog
from src.cogs.ai_cog import AICog
from src.economy import _ct_today

from tests.fakes.discord import FakeMember, FakeGuild, FakeTextChannel, FakeCtx, FakeMessage


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _stub_recap_deps(monkeypatch):
    """Stub the I/O seams: stream_ollama, finalize, keep_typing,
    check_ai_channel, and save_recap_usage. Returns the list of
    save_recap_usage calls so tests can assert the cap was committed."""
    async def _stub_stream(session, messages, placeholder, **kwargs):
        return "- Someone said something. Nobody cared."
    monkeypatch.setattr(_ai_cog, "stream_ollama", _stub_stream)

    async def _stub_finalize(placeholder, channel, text):
        return None
    monkeypatch.setattr(_ai_cog, "finalize", _stub_finalize)

    async def _no_typing(*a, **kw):
        return None
    monkeypatch.setattr(_ai_cog, "keep_typing", _no_typing)

    async def _stub_check_ai_channel(ctx):
        return False
    monkeypatch.setattr(_ai_cog, "check_ai_channel", _stub_check_ai_channel)

    save_calls = []
    async def _stub_save(guild_id, user_id, day):
        save_calls.append((guild_id, user_id, day))
    monkeypatch.setattr(_ai_cog, "save_recap_usage", _stub_save)

    return save_calls


def _make_guild_with_channels(*channels) -> FakeGuild:
    """FakeGuild whose `text_channels` is the given list. Each channel gets
    a `guild` backref, an `nsfw` flag, and a `permissions_for` that returns
    a perms object whose `read_messages` / `read_message_history` we control
    via the channel's `_visible` attribute."""
    guild = FakeGuild(gid=42)
    guild.text_channels = list(channels)
    guild.default_role = object()
    guild.me = FakeMember(uid=999_999, display_name="bot")
    for ch in channels:
        ch.guild = guild
    return guild


def _channel(ch_id, name="general", *, visible=True, nsfw=False, messages=None):
    ch = FakeTextChannel(ch_id=ch_id, name=name)
    ch.nsfw = nsfw
    ch._visible = visible
    ch._messages = messages or []

    def _permissions_for(role_or_member, _ch=ch):
        class _P:
            read_messages = _ch._visible
            read_message_history = _ch._visible
        return _P()
    ch.permissions_for = _permissions_for

    def _history(limit=100, after=None, oldest_first=False, _ch=ch):
        items = list(_ch._messages)

        class _It:
            def __init__(self, xs):
                self._it = iter(xs)
            def __aiter__(self):
                return self
            async def __anext__(self):
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration
        return _It(items)
    ch.history = _history
    return ch


def _msg(text, author_name="Joseph", uid=1, *, bot=False, minutes_ago=5):
    m = FakeMessage(content=text, author=FakeMember(uid=uid, display_name=author_name))
    m.author.bot = bot
    m.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return m


def _ctx(author, guild, run_channel):
    ctx = FakeCtx(author=author, guild=guild, channel=run_channel)
    ctx.message = FakeMessage(content="!recap", author=author, message_id=999)
    # FakeCtx.send returns None by default; recap needs the placeholder it
    # returns to have an awaitable .edit. Hand back a FakeMessage.
    placeholder = FakeMessage()
    ctx.send = _recording_send(ctx, placeholder)
    return ctx, placeholder


def _recording_send(ctx, placeholder):
    async def _send(content=None, *, embed=None, **kwargs):
        if embed is not None:
            ctx.sent_embeds.append(embed)
        if content is not None:
            ctx.sent_messages.append(content)
        return placeholder
    return _send


@pytest.fixture(autouse=True)
def _clear_recap_state():
    _state.recap_usage.clear()
    _state.godmode_users.clear()
    _state.bot_admins.clear()
    yield
    _state.recap_usage.clear()
    _state.godmode_users.clear()
    _state.bot_admins.clear()


# ── happy path ────────────────────────────────────────────────────────────────

async def test_recap_summarizes_and_commits_daily_cap(_stub_recap_deps):
    cog = AICog(bot=None)
    author = FakeMember(uid=1001, display_name="runner")
    chan = _channel(100, "general", messages=[
        _msg("anyone want to play RV There Yet", "Joseph", uid=1),
        _msg("fuck off", "Nick", uid=2),
    ])
    guild = _make_guild_with_channels(chan)
    ctx, placeholder = _ctx(author, guild, chan)

    await cog.cmd_recap.callback(cog, ctx, focus=None)

    # The daily-cap claim landed in state AND was persisted.
    assert _state.recap_usage[(42, 1001)] == _ct_today()
    assert _stub_recap_deps == [(42, 1001, _ct_today())]
    # finalize is stubbed, so success = no error embed was sent.
    assert not any(getattr(e, "title", "").startswith("⚠️") for e in ctx.sent_embeds)


async def test_recap_second_run_same_day_is_blocked(_stub_recap_deps):
    cog = AICog(bot=None)
    author = FakeMember(uid=1002, display_name="runner")
    chan = _channel(100, "general", messages=[_msg("hi", "Joseph", uid=1)])
    guild = _make_guild_with_channels(chan)

    # First run claims the slot.
    ctx1, _ = _ctx(author, guild, chan)
    await cog.cmd_recap.callback(cog, ctx1, focus=None)
    assert (42, 1002) in _state.recap_usage

    # Second run same day: blocked, no new save.
    _stub_recap_deps.clear()
    ctx2, _ = _ctx(author, guild, chan)
    await cog.cmd_recap.callback(cog, ctx2, focus=None)

    assert _stub_recap_deps == []
    assert any("Already Recapped" in getattr(e, "title", "") for e in ctx2.sent_embeds)


async def test_recap_godmode_user_bypasses_cap(_stub_recap_deps):
    cog = AICog(bot=None)
    author = FakeMember(uid=1003, display_name="admin")
    _state.godmode_users.add(1003)
    chan = _channel(100, "general", messages=[_msg("hi", "Joseph", uid=1)])
    guild = _make_guild_with_channels(chan)

    # Two runs back-to-back; godmode users are never capped or persisted.
    ctx1, _ = _ctx(author, guild, chan)
    await cog.cmd_recap.callback(cog, ctx1, focus=None)
    ctx2, _ = _ctx(author, guild, chan)
    await cog.cmd_recap.callback(cog, ctx2, focus=None)

    assert (42, 1003) not in _state.recap_usage
    assert _stub_recap_deps == []
    assert not any("Already Recapped" in getattr(e, "title", "") for e in ctx2.sent_embeds)


async def test_recap_bot_admin_bypasses_cap(_stub_recap_deps):
    """Bot admins (BOT_ADMIN_IDS / bot_admin override) skip the once-a-day
    cap just like godmode users — no claim, no persisted usage row."""
    cog = AICog(bot=None)
    author = FakeMember(uid=1009, display_name="botadmin")
    _state.bot_admins.add(1009)  # is_admin(ctx) → True
    chan = _channel(100, "general", messages=[_msg("hi", "Joseph", uid=1)])
    guild = _make_guild_with_channels(chan)

    # Two runs back-to-back; the second must not be blocked.
    ctx1, _ = _ctx(author, guild, chan)
    await cog.cmd_recap.callback(cog, ctx1, focus=None)
    ctx2, _ = _ctx(author, guild, chan)
    await cog.cmd_recap.callback(cog, ctx2, focus=None)

    assert (42, 1009) not in _state.recap_usage
    assert _stub_recap_deps == []
    assert not any("Already Recapped" in getattr(e, "title", "") for e in ctx2.sent_embeds)


# ── channel filtering ─────────────────────────────────────────────────────────

async def test_recap_skips_private_and_nsfw_channels(_stub_recap_deps, monkeypatch):
    """The transcript handed to Ollama must exclude messages from channels
    the @everyone role can't read, and from NSFW-flagged channels."""
    captured = {}
    async def _capture_stream(session, messages, placeholder, **kwargs):
        captured["messages"] = messages
        return "- recap"
    monkeypatch.setattr(_ai_cog, "stream_ollama", _capture_stream)

    cog = AICog(bot=None)
    author = FakeMember(uid=1004, display_name="runner")
    public = _channel(100, "general", visible=True, messages=[_msg("public msg", "Joe", uid=1)])
    private = _channel(101, "staff", visible=False, messages=[_msg("secret msg", "Mod", uid=2)])
    nsfw = _channel(102, "spicy", visible=True, nsfw=True, messages=[_msg("nsfw msg", "Bob", uid=3)])
    guild = _make_guild_with_channels(public, private, nsfw)
    ctx, _ = _ctx(author, guild, public)

    await cog.cmd_recap.callback(cog, ctx, focus=None)

    transcript = captured["messages"][1]["content"]
    assert "public msg" in transcript
    assert "secret msg" not in transcript
    assert "nsfw msg" not in transcript


async def test_recap_skips_bot_messages(_stub_recap_deps, monkeypatch):
    captured = {}
    async def _capture_stream(session, messages, placeholder, **kwargs):
        captured["messages"] = messages
        return "- recap"
    monkeypatch.setattr(_ai_cog, "stream_ollama", _capture_stream)

    cog = AICog(bot=None)
    author = FakeMember(uid=1005, display_name="runner")
    chan = _channel(100, "general", messages=[
        _msg("a real human message", "Joseph", uid=1),
        _msg("beep boop I am a bot", "BotUser", uid=777, bot=True),
    ])
    guild = _make_guild_with_channels(chan)
    ctx, _ = _ctx(author, guild, chan)

    await cog.cmd_recap.callback(cog, ctx, focus=None)

    transcript = captured["messages"][1]["content"]
    assert "a real human message" in transcript
    assert "beep boop" not in transcript


async def test_recap_with_no_messages_does_not_consume_daily_slot(_stub_recap_deps):
    """If there's nothing to recap, the user's once-a-day slot is rolled
    back so they aren't burned for an empty server."""
    cog = AICog(bot=None)
    author = FakeMember(uid=1006, display_name="runner")
    empty = _channel(100, "general", messages=[])
    guild = _make_guild_with_channels(empty)
    ctx, placeholder = _ctx(author, guild, empty)

    await cog.cmd_recap.callback(cog, ctx, focus=None)

    assert (42, 1006) not in _state.recap_usage
    assert _stub_recap_deps == []
    # The "Nothing to Recap" notice is sent by editing the placeholder.
    embeds = [
        kw.get("embed") for _, kw in placeholder.edit.await_args_list
        if kw.get("embed") is not None
    ]
    assert any("Nothing to Recap" in getattr(e, "title", "") for e in embeds)


# ── focus argument ────────────────────────────────────────────────────────────

async def test_recap_focus_arg_is_passed_into_prompt(_stub_recap_deps, monkeypatch):
    captured = {}
    async def _capture_stream(session, messages, placeholder, **kwargs):
        captured["messages"] = messages
        return "- recap"
    monkeypatch.setattr(_ai_cog, "stream_ollama", _capture_stream)

    cog = AICog(bot=None)
    author = FakeMember(uid=1007, display_name="runner")
    chan = _channel(100, "general", messages=[_msg("hi", "Joseph", uid=1)])
    guild = _make_guild_with_channels(chan)
    ctx, _ = _ctx(author, guild, chan)

    await cog.cmd_recap.callback(cog, ctx, focus="Joseph")

    user_prompt = captured["messages"][1]["content"]
    assert "focus" in user_prompt.lower()
    assert "Joseph" in user_prompt


# ── failure rollback ──────────────────────────────────────────────────────────

async def test_recap_rolls_back_cap_when_ollama_returns_empty(_stub_recap_deps, monkeypatch):
    """stream_ollama returning '' means AI disabled / rate-limited — the
    user's daily slot must be refunded so they can retry later."""
    async def _empty_stream(session, messages, placeholder, **kwargs):
        return ""
    monkeypatch.setattr(_ai_cog, "stream_ollama", _empty_stream)

    cog = AICog(bot=None)
    author = FakeMember(uid=1008, display_name="runner")
    chan = _channel(100, "general", messages=[_msg("hi", "Joseph", uid=1)])
    guild = _make_guild_with_channels(chan)
    ctx, _ = _ctx(author, guild, chan)

    await cog.cmd_recap.callback(cog, ctx, focus=None)

    assert (42, 1008) not in _state.recap_usage
    assert _stub_recap_deps == []
