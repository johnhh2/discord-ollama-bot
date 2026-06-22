"""VoiceCog: subscription pings and the per-subscriber ignore list.

The behavior under test is the empty→active DM and `!subscribe ignore`. The key
invariant for the ignore list: an ignored member triggering a channel does NOT
ping the subscriber AND does NOT consume the subscriber's per-channel cooldown,
so a later non-ignored trigger still pings.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import src.state as _state
from src.cogs.voice_cog import VoiceCog


_aio = pytest.mark.asyncio

GUILD_ID = 42
CHANNEL_ID = 100
SUBSCRIBER_ID = 7
TRIGGER_ID = 9


def _make_cog(user_dms):
    """Cog whose bot.get_user returns a stub user recording .send() calls."""
    sent_to = {}

    def _get_user(uid):
        if uid not in user_dms:
            return None
        u = SimpleNamespace()
        u.send = AsyncMock()
        sent_to[uid] = u.send
        return u

    fake_bot = SimpleNamespace(get_user=_get_user, fetch_user=AsyncMock())
    cog = VoiceCog(bot=fake_bot)
    return cog, sent_to


def _voice_channel(members):
    # spec= makes isinstance(ch, discord.VoiceChannel) true in the listener while
    # letting us set .members (a read-only property on the real class).
    ch = MagicMock(spec=discord.VoiceChannel)
    ch.id = CHANNEL_ID
    ch.name = "General"
    ch.members = members
    ch.guild = SimpleNamespace(id=GUILD_ID, name="Test Guild")
    return ch


def _member(uid, *, bot=False):
    return SimpleNamespace(id=uid, bot=bot, display_name=f"user{uid}")


async def _fire_join(cog, *, trigger_id, already_present=()):
    """Fire the listener for trigger_id joining.

    `already_present` is an iterable of member specs already in the channel
    *before* this join — each is an id, or a (id, {"bot": True}) tuple. The
    channel's member roster passed to the listener includes the just-joined
    trigger plus everyone in `already_present`.
    """
    trigger = _member(trigger_id)
    members = [trigger]
    for spec in already_present:
        if isinstance(spec, tuple):
            uid, opts = spec
            members.append(_member(uid, bot=opts.get("bot", False)))
        else:
            members.append(_member(spec))
    channel = _voice_channel(members)
    before = SimpleNamespace(channel=None)
    after = SimpleNamespace(channel=channel)
    await cog.on_voice_state_update(trigger, before, after)


@pytest.fixture(autouse=True)
def _seed_subscription(monkeypatch):
    """One subscriber to CHANNEL_ID, no ignores, with DB writes stubbed."""
    _state.voice_pings[(CHANNEL_ID, SUBSCRIBER_ID)] = {
        "guild_id": GUILD_ID,
        "last_pinged_at": None,
    }
    import src.cogs.voice_cog as _vc
    monkeypatch.setattr(_vc, "update_voice_ping_last_pinged", AsyncMock())
    yield


@_aio
async def test_trigger_pings_subscriber_and_sets_cooldown():
    cog, sent_to = _make_cog({SUBSCRIBER_ID})
    await _fire_join(cog, trigger_id=TRIGGER_ID)

    sent_to[SUBSCRIBER_ID].assert_awaited_once()
    assert _state.voice_pings[(CHANNEL_ID, SUBSCRIBER_ID)]["last_pinged_at"] is not None


@_aio
async def test_ignored_trigger_does_not_ping_and_does_not_burn_cooldown():
    _state.voice_ping_ignores[(GUILD_ID, SUBSCRIBER_ID)] = {TRIGGER_ID}
    cog, sent_to = _make_cog({SUBSCRIBER_ID})

    await _fire_join(cog, trigger_id=TRIGGER_ID)

    assert SUBSCRIBER_ID not in sent_to  # get_user never resolved into a send
    # Cooldown untouched: a later non-ignored trigger must still be able to ping.
    assert _state.voice_pings[(CHANNEL_ID, SUBSCRIBER_ID)]["last_pinged_at"] is None


@_aio
async def test_ignore_then_non_ignored_trigger_still_pings():
    _state.voice_ping_ignores[(GUILD_ID, SUBSCRIBER_ID)] = {TRIGGER_ID}
    cog, sent_to = _make_cog({SUBSCRIBER_ID})

    # Ignored member fills the channel — no ping, no cooldown.
    await _fire_join(cog, trigger_id=TRIGGER_ID)
    assert SUBSCRIBER_ID not in sent_to

    # A different (non-ignored) member fills it — ping fires because the cooldown
    # was never consumed by the ignored trigger.
    await _fire_join(cog, trigger_id=TRIGGER_ID + 1)
    sent_to[SUBSCRIBER_ID].assert_awaited_once()
    assert _state.voice_pings[(CHANNEL_ID, SUBSCRIBER_ID)]["last_pinged_at"] is not None


@_aio
async def test_ignore_is_per_subscriber_not_global():
    """Subscriber B ignoring the trigger must not suppress subscriber A's ping."""
    other_sub = SUBSCRIBER_ID + 50
    _state.voice_pings[(CHANNEL_ID, other_sub)] = {
        "guild_id": GUILD_ID, "last_pinged_at": None,
    }
    # other_sub ignores the trigger; SUBSCRIBER_ID does not.
    _state.voice_ping_ignores[(GUILD_ID, other_sub)] = {TRIGGER_ID}

    cog, sent_to = _make_cog({SUBSCRIBER_ID, other_sub})
    await _fire_join(cog, trigger_id=TRIGGER_ID)

    sent_to[SUBSCRIBER_ID].assert_awaited_once()   # A still pinged
    assert other_sub not in sent_to                # B suppressed


# ── "first non-ignored arrival" — ping when a non-ignored user joins after an
#    ignored one already filled the channel ───────────────────────────────────

@_aio
async def test_non_ignored_second_joiner_pings_after_ignored_first():
    """Ignored user joins first (0→1), then a non-ignored user joins second
    (1→2). The subscriber should be pinged for the second joiner."""
    _state.voice_ping_ignores[(GUILD_ID, SUBSCRIBER_ID)] = {TRIGGER_ID}
    cog, sent_to = _make_cog({SUBSCRIBER_ID})

    # Ignored user is first — no ping, no cooldown.
    await _fire_join(cog, trigger_id=TRIGGER_ID)
    assert SUBSCRIBER_ID not in sent_to

    # Non-ignored user joins as the 2nd member (the ignored user is still there).
    other = TRIGGER_ID + 1
    await _fire_join(cog, trigger_id=other, already_present=[TRIGGER_ID])
    sent_to[SUBSCRIBER_ID].assert_awaited_once()
    assert _state.voice_pings[(CHANNEL_ID, SUBSCRIBER_ID)]["last_pinged_at"] is not None


@_aio
async def test_no_double_ping_for_third_relevant_joiner():
    """Once a non-ignored user is present and pinged, a further non-ignored
    joiner shouldn't re-ping (they aren't the first relevant arrival)."""
    cog, sent_to = _make_cog({SUBSCRIBER_ID})

    # First non-ignored user → ping.
    await _fire_join(cog, trigger_id=TRIGGER_ID)
    sent_to[SUBSCRIBER_ID].assert_awaited_once()

    # A second non-ignored user joins while the first is still present. Not the
    # first relevant arrival → no new ping (cooldown would also block it).
    await _fire_join(cog, trigger_id=TRIGGER_ID + 1, already_present=[TRIGGER_ID])
    sent_to[SUBSCRIBER_ID].assert_awaited_once()  # still exactly one


@_aio
async def test_bots_present_dont_block_first_relevant_arrival():
    """A bot already in the channel doesn't count as a relevant member, so a
    human joining second is still the first relevant arrival."""
    cog, sent_to = _make_cog({SUBSCRIBER_ID})
    bot_id = 555
    await _fire_join(
        cog, trigger_id=TRIGGER_ID,
        already_present=[(bot_id, {"bot": True})],
    )
    sent_to[SUBSCRIBER_ID].assert_awaited_once()


# ── !subscribe ignore command (round-trips through the real DB) ──────────────

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember  # noqa: E402
from src.persistence import load_voice_ping_ignores  # noqa: E402


@pytest.fixture
def _no_builtin_member_converter(monkeypatch):
    """Force src.helpers.MemberConverter to use its substring fallback.

    discord.py's built-in MemberConverter requires real discord.Member objects
    and a populated _state (it does isinstance(result, discord.Member) and a
    gateway query), neither of which our FakeMember/FakeGuild provide. In
    production it handles mentions/IDs; here we make it raise BadArgument so the
    project converter falls through to the case-insensitive substring match we
    actually want to exercise."""
    import discord.ext.commands as _c

    async def _always_bad(self, ctx, argument):
        raise _c.BadArgument(f"Member '{argument}' not found.")

    monkeypatch.setattr(_c.MemberConverter, "convert", _always_bad)


def _ctx(members=()):
    author = FakeMember(uid=SUBSCRIBER_ID, display_name="Xeph")
    guild = FakeGuild(gid=GUILD_ID)
    guild.members = list(members)
    ctx = FakeCtx(author=author, guild=guild, command_name="subscribe ignore")
    return ctx


def _guild_member(uid=TRIGGER_ID, name="Racist", *, bot=False):
    m = FakeMember(uid=uid, display_name=name)
    m.bot = bot
    return m


@_aio
async def test_ignore_command_adds_then_removes_by_name_substring(db, _no_builtin_member_converter):
    cog, _ = _make_cog(set())
    target = _guild_member(name="Racist")

    # Add — resolved by case-insensitive name substring ("raci" → "Racist").
    ctx = _ctx(members=[target])
    await cog.cmd_subscribe_ignore.callback(cog, ctx, query="raci")
    assert TRIGGER_ID in _state.voice_ping_ignores[(GUILD_ID, SUBSCRIBER_ID)]
    assert (await load_voice_ping_ignores())[(GUILD_ID, SUBSCRIBER_ID)] == {TRIGGER_ID}
    assert any("Ignoring" in (e.title or "") for e in ctx.sent_embeds)

    # Toggle off — same query.
    ctx2 = _ctx(members=[target])
    await cog.cmd_subscribe_ignore.callback(cog, ctx2, query="raci")
    assert (GUILD_ID, SUBSCRIBER_ID) not in _state.voice_ping_ignores
    assert (await load_voice_ping_ignores()) == {}
    assert any("No Longer Ignoring" in (e.title or "") for e in ctx2.sent_embeds)


@_aio
async def test_ignore_command_lists_when_no_query(db):
    cog, _ = _make_cog(set())
    _state.voice_ping_ignores[(GUILD_ID, SUBSCRIBER_ID)] = {TRIGGER_ID}
    ctx = _ctx(members=[_guild_member()])

    await cog.cmd_subscribe_ignore.callback(cog, ctx, query=None)
    assert any("Ignored Triggers" in (e.title or "") for e in ctx.sent_embeds)


@_aio
async def test_ignore_command_unknown_name_sends_not_found(db, _no_builtin_member_converter):
    """A name that matches nobody must NOT be treated as the list request, and
    must NOT raise (which would leak to the global error logger)."""
    cog, _ = _make_cog(set())
    ctx = _ctx(members=[_guild_member(name="Racist")])

    await cog.cmd_subscribe_ignore.callback(cog, ctx, query="Nobody")
    assert any("Not Found" in (e.title or "") for e in ctx.sent_embeds)
    assert (GUILD_ID, SUBSCRIBER_ID) not in _state.voice_ping_ignores


@_aio
async def test_ignore_command_ambiguous_name(db, _no_builtin_member_converter):
    cog, _ = _make_cog(set())
    ctx = _ctx(members=[
        _guild_member(uid=TRIGGER_ID, name="Racer One"),
        _guild_member(uid=TRIGGER_ID + 1, name="Racer Two"),
    ])

    await cog.cmd_subscribe_ignore.callback(cog, ctx, query="Racer")
    assert any("Ambiguous" in (e.title or "") for e in ctx.sent_embeds)
    assert (GUILD_ID, SUBSCRIBER_ID) not in _state.voice_ping_ignores


@_aio
async def test_ignore_command_rejects_self_and_bots(db, _no_builtin_member_converter):
    cog, _ = _make_cog(set())

    me = FakeMember(uid=SUBSCRIBER_ID, display_name="Xeph")
    ctx = _ctx(members=[me])
    await cog.cmd_subscribe_ignore.callback(cog, ctx, query="Xeph")
    assert any("Yourself" in (e.title or "") for e in ctx.sent_embeds)

    botm = _guild_member(uid=999, name="Botty", bot=True)
    ctx2 = _ctx(members=[botm])
    await cog.cmd_subscribe_ignore.callback(cog, ctx2, query="Botty")
    assert any("Bots" in (e.title or "") for e in ctx2.sent_embeds)
    assert (GUILD_ID, SUBSCRIBER_ID) not in _state.voice_ping_ignores


# ── global error handler honors error.handled (no "⚠️ Command Error" report) ──

@_aio
async def test_global_handler_skips_reported_when_error_handled(monkeypatch):
    """A command-local handler that set error.handled=True must not also trigger
    the global audit log / "⚠️ Command Error" report."""
    import src.events as _events
    cog = _events.EventsCog(bot=SimpleNamespace())

    logged = []

    async def _spy_log(bot, ctx, error):
        logged.append(error)

    monkeypatch.setattr(_events, "_log_command_error", _spy_log)
    _state.audit_log.clear()

    err = discord.ext.commands.BadArgument("That doesn't look like a voice channel.")
    err.handled = True
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD_ID),
        author=SimpleNamespace(display_name="Xeph", id=SUBSCRIBER_ID),
        message=SimpleNamespace(content="!subscribe nope"),
        command=SimpleNamespace(qualified_name="subscribe"),
    )

    await cog.on_command_error(ctx, err)

    assert logged == []                 # no report sent
    assert len(_state.audit_log) == 0   # nothing recorded


@_aio
async def test_global_handler_reports_unhandled_error(monkeypatch):
    """Sanity check the opposite: an error without .handled still gets reported."""
    import src.events as _events
    cog = _events.EventsCog(bot=SimpleNamespace())

    logged = []

    async def _spy_log(bot, ctx, error):
        logged.append(error)

    monkeypatch.setattr(_events, "_log_command_error", _spy_log)
    _state.audit_log.clear()

    err = RuntimeError("boom")
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD_ID),
        author=SimpleNamespace(display_name="Xeph", id=SUBSCRIBER_ID),
        message=SimpleNamespace(content="!subscribe nope"),
        command=SimpleNamespace(qualified_name="subscribe"),
    )

    with pytest.raises(RuntimeError):
        await cog.on_command_error(ctx, err)
    assert logged == [err]              # reported
    assert len(_state.audit_log) == 1


@_aio
async def test_global_handler_shows_usage_on_bad_argument(monkeypatch):
    """Bad input to a typed param (e.g. `!scratch help` → BadArgument) must reply
    with the command's usage, NOT route to the "⚠️ Command Error" admin report."""
    import src.events as _events
    cog = _events.EventsCog(bot=SimpleNamespace())

    logged = []

    async def _spy_log(bot, ctx, error):
        logged.append(error)

    monkeypatch.setattr(_events, "_log_command_error", _spy_log)
    _state.audit_log.clear()

    sent = []

    async def _send(msg):
        sent.append(msg)

    err = discord.ext.commands.BadArgument(
        'Converting to "int" failed for parameter "count".'
    )
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD_ID),
        author=SimpleNamespace(display_name="Xeph", id=SUBSCRIBER_ID),
        message=SimpleNamespace(content="!scratch help"),
        command=SimpleNamespace(qualified_name="scratch", signature="[count]"),
        clean_prefix="!",
        send=_send,
    )

    # Must NOT raise (the old behavior re-raised the error).
    await cog.on_command_error(ctx, err)

    assert logged == []                 # no admin bug-report
    assert len(_state.audit_log) == 0   # nothing recorded
    assert sent == ["❌ Usage: `!scratch [count]`"]


@_aio
async def test_global_handler_shows_usage_on_missing_required_argument(monkeypatch):
    """MissingRequiredArgument (also a UserInputError) takes the same usage path."""
    import src.events as _events
    cog = _events.EventsCog(bot=SimpleNamespace())

    monkeypatch.setattr(_events, "_log_command_error", lambda *a: None)
    _state.audit_log.clear()

    sent = []

    async def _send(msg):
        sent.append(msg)

    # MissingRequiredArgument needs a Parameter; fabricate a minimal one.
    param = SimpleNamespace(name="opponent", displayed_name=None)
    err = discord.ext.commands.MissingRequiredArgument(param)
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD_ID),
        author=SimpleNamespace(display_name="Xeph", id=SUBSCRIBER_ID),
        message=SimpleNamespace(content="!ttt"),
        command=SimpleNamespace(qualified_name="ttt", signature="[opponent] [amount]"),
        clean_prefix="!",
        send=_send,
    )

    await cog.on_command_error(ctx, err)

    assert len(_state.audit_log) == 0
    assert sent == ["❌ Usage: `!ttt [opponent] [amount]`"]
