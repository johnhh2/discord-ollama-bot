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

    discord.py's built-in MemberConverter needs real discord.Member objects and
    a populated _state (an isinstance check and a gateway query), which
    FakeMember/FakeGuild don't provide. In production it handles mentions/IDs;
    here it raises BadArgument so the project converter falls through to the
    case-insensitive substring match under test."""
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
async def test_ignore_command_unknown_name_offers_the_user_picker(db, _no_builtin_member_converter, monkeypatch):
    """A name that matches nobody must NOT be treated as the list request, and
    must NOT raise (which would leak to the global error logger). It opens the
    user picker: a decline stays silent (the closed prompt is the reply), a
    pick is used as the target."""
    cog, _ = _make_cog(set())
    target = _guild_member(name="Racist")
    asked = []

    async def _declined(ctx, argument):
        asked.append(argument)
        return None
    monkeypatch.setattr("src.pickers.find_member", _declined)
    ctx = _ctx(members=[target])
    await cog.cmd_subscribe_ignore.callback(cog, ctx, query="Nobody")
    assert asked == ["Nobody"]
    assert ctx.sent_embeds == []
    assert (GUILD_ID, SUBSCRIBER_ID) not in _state.voice_ping_ignores

    async def _picked(ctx, argument):
        return target
    monkeypatch.setattr("src.pickers.find_member", _picked)
    ctx2 = _ctx(members=[target])
    await cog.cmd_subscribe_ignore.callback(cog, ctx2, query="Nobody")
    assert _state.voice_ping_ignores[(GUILD_ID, SUBSCRIBER_ID)] == {TRIGGER_ID}
    assert any("Ignoring Triggers" in (e.title or "") for e in ctx2.sent_embeds)


@_aio
async def test_ignore_command_ambiguous_name_offers_the_candidates(db, _no_builtin_member_converter, monkeypatch):
    cog, _ = _make_cog(set())
    one = _guild_member(uid=TRIGGER_ID, name="Racer One")
    two = _guild_member(uid=TRIGGER_ID + 1, name="Racer Two")
    offered = []

    async def _declined(ctx, argument, matches):
        offered.append(list(matches))
        return None
    monkeypatch.setattr("src.pickers.choose_member", _declined)
    ctx = _ctx(members=[one, two])
    await cog.cmd_subscribe_ignore.callback(cog, ctx, query="Racer")
    assert offered == [[one, two]]
    assert ctx.sent_embeds == []  # "None of the above": the closed prompt says it all
    assert (GUILD_ID, SUBSCRIBER_ID) not in _state.voice_ping_ignores

    async def _picked(ctx, argument, matches):
        return two
    monkeypatch.setattr("src.pickers.choose_member", _picked)
    ctx2 = _ctx(members=[one, two])
    await cog.cmd_subscribe_ignore.callback(cog, ctx2, query="Racer")
    assert _state.voice_ping_ignores[(GUILD_ID, SUBSCRIBER_ID)] == {TRIGGER_ID + 1}


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


# ── !unsubscribe <channel|all> (round-trips through the real DB) ─────────────

from src.persistence import load_voice_pings, save_voice_ping  # noqa: E402

OTHER_CHANNEL_ID = 101
OTHER_GUILD_ID = 43


def _voice_stub(cid, name):
    return SimpleNamespace(id=cid, name=name, mention=f"<#{cid}>")


@pytest.fixture
def _voice_channel_converter(monkeypatch):
    """Force src.helpers.VoiceChannelConverter onto its name fallback.

    discord.py's own converter needs a real guild cache (an isinstance check
    on the channel) that FakeGuild doesn't provide. In production it handles
    IDs, mentions and exact names; here it raises so the project converter's
    case-insensitive match over ctx.guild.voice_channels is what runs —
    the same trick as `_no_builtin_member_converter`."""
    import discord.ext.commands as _c

    async def _always_missing(self, ctx, argument):
        raise _c.ChannelNotFound(argument)

    monkeypatch.setattr(_c.VoiceChannelConverter, "convert", _always_missing)


async def _seed_three_subscriptions():
    """Two in GUILD_ID, one in OTHER_GUILD_ID — `all` must leave the third."""
    for gid, cid in ((GUILD_ID, CHANNEL_ID), (GUILD_ID, OTHER_CHANNEL_ID), (OTHER_GUILD_ID, 300)):
        _state.voice_pings[(cid, SUBSCRIBER_ID)] = {"guild_id": gid, "last_pinged_at": None}
        await save_voice_ping(gid, cid, SUBSCRIBER_ID)


def _unsub_ctx(channels=()):
    ctx = _ctx()
    ctx.guild.channels = ctx.guild.voice_channels = list(channels)
    ctx.command.qualified_name = ctx.command.name = "unsubscribe"
    return ctx


@_aio
async def test_unsubscribe_all_drops_every_subscription_in_this_guild_only(db):
    cog, _ = _make_cog(set())
    await _seed_three_subscriptions()
    ctx = _unsub_ctx(channels=[_voice_stub(CHANNEL_ID, "General")])

    await cog.cmd_unsubscribe.callback(cog, ctx, channel="ALL")

    assert set(_state.voice_pings) == {(300, SUBSCRIBER_ID)}
    assert set(await load_voice_pings()) == {(300, SUBSCRIBER_ID)}
    done = next(e for e in ctx.sent_embeds if "Unsubscribed From All" in (e.title or ""))
    assert "2 voice-channel subscriptions" in done.description
    assert f"<#{CHANNEL_ID}>" in done.description
    assert f"`#{OTHER_CHANNEL_ID}` (deleted)" in done.description


@_aio
async def test_unsubscribe_all_with_nothing_subscribed(db):
    cog, _ = _make_cog(set())
    _state.voice_pings.clear()
    ctx = _unsub_ctx()

    await cog.cmd_unsubscribe.callback(cog, ctx, channel="all")
    assert any("Nothing To Unsubscribe" in (e.title or "") for e in ctx.sent_embeds)


@_aio
async def test_unsubscribe_one_channel_by_name(db, _voice_channel_converter):
    cog, _ = _make_cog(set())
    await _seed_three_subscriptions()
    general = _voice_stub(CHANNEL_ID, "General")
    ctx = _unsub_ctx(channels=[general, _voice_stub(OTHER_CHANNEL_ID, "Games")])

    await cog.cmd_unsubscribe.callback(cog, ctx, channel="general")

    assert (CHANNEL_ID, SUBSCRIBER_ID) not in _state.voice_pings
    assert (OTHER_CHANNEL_ID, SUBSCRIBER_ID) in _state.voice_pings
    assert (CHANNEL_ID, SUBSCRIBER_ID) not in await load_voice_pings()
    assert (OTHER_CHANNEL_ID, SUBSCRIBER_ID) in await load_voice_pings()
    assert any("Unsubscribed" in (e.title or "") for e in ctx.sent_embeds)

    # Not subscribed to it → say so, nothing else dropped.
    ctx2 = _unsub_ctx(channels=[general])
    await cog.cmd_unsubscribe.callback(cog, ctx2, channel="General")
    assert any("Not Subscribed" in (e.title or "") for e in ctx2.sent_embeds)
    assert (OTHER_CHANNEL_ID, SUBSCRIBER_ID) in _state.voice_pings


@_aio
async def test_unsubscribe_unknown_channel_and_no_argument(db, _voice_channel_converter):
    cog, _ = _make_cog(set())
    await _seed_three_subscriptions()

    # An unknown name opens the channel picker (declined by conftest's default)
    # and the command adds nothing; an unknown id never prompts and is answered.
    ctx = _unsub_ctx(channels=[_voice_stub(CHANNEL_ID, "General")])
    await cog.cmd_unsubscribe.callback(cog, ctx, channel="Nowhere")
    assert ctx.sent_embeds == []

    ctx1 = _unsub_ctx(channels=[_voice_stub(CHANNEL_ID, "General")])
    await cog.cmd_unsubscribe.callback(cog, ctx1, channel="999")
    assert any("Not Found" in (e.title or "") for e in ctx1.sent_embeds)

    ctx2 = _unsub_ctx()
    await cog.cmd_unsubscribe.callback(cog, ctx2, channel=None)
    assert any("Say which" in (e.description or "") for e in ctx2.sent_embeds)

    assert len(_state.voice_pings) == 3


# ── voice-channel name matching (mirrors MemberConverter) ────────────────────

from src.helpers import VoiceChannelConverter  # noqa: E402


@_aio
async def test_voice_channel_converter_matches_name_like_a_username(_voice_channel_converter, monkeypatch):
    import discord.ext.commands as _c
    general = _voice_stub(CHANNEL_ID, "🔊 General")
    general2 = _voice_stub(OTHER_CHANNEL_ID, "General 2")
    games = _voice_stub(102, "Games")
    room42 = _voice_stub(103, "Room 42")
    ctx = _unsub_ctx(channels=[general, general2, games, room42])
    conv = VoiceChannelConverter()
    prompts = []

    async def _choose(ctx, argument, matches):
        prompts.append(("choose", argument, list(matches)))
        return None
    async def _find(ctx, argument):
        prompts.append(("find", argument))
        return None
    monkeypatch.setattr("src.pickers.choose_voice_channel", _choose)
    monkeypatch.setattr("src.pickers.find_voice_channel", _find)

    # Case-insensitive substring: nobody types the emoji.
    assert await conv.convert(ctx, "gam") is games
    assert await conv.convert(ctx, "GENERAL 2") is general2
    # A whole-name match beats channels that merely contain the query.
    assert await conv.convert(ctx, "general 2") is general2
    assert prompts == []

    # Ambiguous: the candidates go to a dropdown; declined → a handled BadArgument.
    with pytest.raises(_c.BadArgument, match="matched multiple voice channels: 🔊 General, General 2") as info:
        await conv.convert(ctx, "gener")
    assert info.value.handled is True
    assert prompts == [("choose", "gener", [general, general2])]
    # Unknown: the channel picker; declined → a handled ChannelNotFound.
    with pytest.raises(_c.ChannelNotFound) as info:
        await conv.convert(ctx, "Nowhere")
    assert info.value.handled is True
    assert prompts[-1] == ("find", "Nowhere")
    # A bare number is an ID, never a name fragment ("Room 42" stays out) — and no prompt.
    with pytest.raises(_c.ChannelNotFound) as info:
        await conv.convert(ctx, "42")
    assert not getattr(info.value, "handled", False)
    assert len(prompts) == 2


@_aio
async def test_voice_channel_converter_returns_the_pick(_voice_channel_converter, monkeypatch):
    general = _voice_stub(CHANNEL_ID, "🔊 General")
    general2 = _voice_stub(OTHER_CHANNEL_ID, "General 2")
    ctx = _unsub_ctx(channels=[general, general2])
    conv = VoiceChannelConverter()

    async def _choose(ctx, argument, matches):
        return general2
    async def _find(ctx, argument):
        return general
    monkeypatch.setattr("src.pickers.choose_voice_channel", _choose)
    monkeypatch.setattr("src.pickers.find_voice_channel", _find)
    assert await conv.convert(ctx, "gener") is general2
    assert await conv.convert(ctx, "Nowhere") is general


@_aio
async def test_voice_channel_converter_dm_has_no_guild(_voice_channel_converter):
    import discord.ext.commands as _c
    ctx = _unsub_ctx()
    ctx.guild = None
    with pytest.raises(_c.ChannelNotFound):
        await VoiceChannelConverter().convert(ctx, "general")


@_aio
async def test_unsubscribe_by_name_substring_and_ambiguous_name(db, _voice_channel_converter, monkeypatch):
    cog, _ = _make_cog(set())
    await _seed_three_subscriptions()
    general = _voice_stub(CHANNEL_ID, "🔊 General")
    games = _voice_stub(OTHER_CHANNEL_ID, "Games")

    # "g" matches both: the dropdown is declined (conftest's default) → the
    # command says nothing more and drops nothing.
    ctx = _unsub_ctx(channels=[general, games])
    await cog.cmd_unsubscribe.callback(cog, ctx, channel="g")
    assert ctx.sent_embeds == []
    assert len(_state.voice_pings) == 3

    # … and a pick from it is the channel to drop.
    async def _choose(ctx, argument, matches):
        return games
    monkeypatch.setattr("src.pickers.choose_voice_channel", _choose)
    ctx2 = _unsub_ctx(channels=[general, games])
    await cog.cmd_unsubscribe.callback(cog, ctx2, channel="g")
    assert (OTHER_CHANNEL_ID, SUBSCRIBER_ID) not in _state.voice_pings
    assert (CHANNEL_ID, SUBSCRIBER_ID) in _state.voice_pings

    ctx3 = _unsub_ctx(channels=[general, games])
    await cog.cmd_unsubscribe.callback(cog, ctx3, channel="gen")
    assert (CHANNEL_ID, SUBSCRIBER_ID) not in _state.voice_pings
    assert any("Unsubscribed" in (e.title or "") for e in ctx3.sent_embeds)


@_aio
async def test_subscribe_error_handler_stays_quiet_after_a_declined_picker():
    """`!subscribe <name>` converts in discord.py's parser, so its failures
    land in the local error handler. One the converter already prompted for
    (`handled`) gets no second reply; a plain miss (a numeric id) does, and
    without suggesting a #mention — voice channels can't be #mentioned."""
    import discord.ext.commands as _c
    cog, _ = _make_cog(set())

    ctx = _ctx()
    err = _c.ChannelNotFound("Nowhere")
    err.handled = True
    await cog.cmd_subscribe_error(ctx, err)
    assert ctx.sent_embeds == []

    ctx2 = _ctx()
    err2 = _c.ChannelNotFound("123")
    await cog.cmd_subscribe_error(ctx2, err2)
    assert err2.handled is True
    missing = next(e for e in ctx2.sent_embeds if "Channel Not Found" in (e.title or ""))
    assert "#mention" not in missing.description


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
