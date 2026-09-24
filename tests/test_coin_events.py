"""`!event` coin drops: a server admin's events pay from the guild's weekly
budget and end when it runs out; bot admins' events are unlimited; a drop
posted in the dailies channel is kept until the reset.
"""
import asyncio
import datetime
from types import SimpleNamespace

import pytest

import src.state as _state
from src.cogs.economy_cog import EconomyCog
from src.coin_events import event_week_key, event_budget_remaining, claim_event_budget
from src.config import EVENT_WEEKLY_BUDGET
from src.economy import get_balance
from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeChannel, FakeMessage

GID = 77
EVENT_MSG_ID = 424_242


def _titles(ctx) -> list[str]:
    return [e.title for e in ctx.sent_embeds]


async def _post(cog, ctx, amount: str, monkeypatch) -> FakeMessage:
    """Run `!event <amount>` and return the posted drop (or None if refused)."""
    posted = FakeMessage(message_id=EVENT_MSG_ID, channel=ctx.channel)
    ctx.channel.send.side_effect = lambda *a, **kw: posted

    async def _seed(message, emojis, *, what):
        return True
    monkeypatch.setattr("src.cogs.economy_cog.seed_reactions", _seed)
    await cog.cmd_event.callback(cog, ctx, amount)
    return posted if EVENT_MSG_ID in _state.active_events else None


def _react(message: FakeMessage):
    return SimpleNamespace(message=message, emoji="🪙")


@pytest.fixture
def ctx():
    guild = FakeGuild(gid=GID)
    return FakeCtx(author=FakeMember(uid=5, administrator=True), guild=guild,
                   channel=FakeChannel(ch_id=900, guild=guild), command_name="event")


def test_week_starts_on_monday_and_a_stale_row_resets(db):
    assert event_week_key(datetime.date(2026, 9, 24)) == "2026-09-21"   # a Thursday
    assert event_week_key(datetime.date(2026, 9, 21)) == "2026-09-21"   # the Monday itself
    _state.guild_settings[str(GID)] = {"event_budget": {"week": "2020-01-06", "spent": EVENT_WEEKLY_BUDGET}}
    assert event_budget_remaining(GID) == EVENT_WEEKLY_BUDGET
    assert claim_event_budget(GID, EVENT_WEEKLY_BUDGET)
    assert not claim_event_budget(GID, 1)
    assert event_budget_remaining(GID) == 0


@pytest.mark.asyncio
async def test_server_admin_event_over_the_budget_is_refused(db, ctx, monkeypatch):
    assert await _post(EconomyCog(bot=None), ctx, "15k", monkeypatch) is None
    assert _titles(ctx) == ["❌ Over Budget"]


@pytest.mark.asyncio
async def test_server_admin_event_ends_when_the_budget_runs_out(db, ctx, monkeypatch):
    cog = EconomyCog(bot=None)
    msg = await _post(cog, ctx, "4k", monkeypatch)
    assert msg is not None and _state.active_events[msg.id]["budget"]

    users = [FakeMember(uid=u) for u in (11, 12, 13)]
    for user in users:
        await cog.on_reaction_add(_react(msg), user)

    # 4k + 4k fits; the third reaction can't, so it pays nothing and closes the drop.
    assert [await get_balance(u.id) for u in users] == [4_000, 4_000, 0]
    assert msg.id not in _state.active_events
    assert _state.guild_settings[str(GID)]["event_budget"]["spent"] == 8_000
    assert "Event Ended" in msg.edit.await_args.kwargs["embed"].title


@pytest.mark.asyncio
async def test_a_second_event_this_week_only_gets_what_is_left(db, ctx, monkeypatch):
    _state.guild_settings[str(GID)] = {"event_budget": {"week": event_week_key(), "spent": 9_500}}
    cog = EconomyCog(bot=None)
    assert await _post(cog, ctx, "600", monkeypatch) is None
    msg = await _post(cog, ctx, "500", monkeypatch)
    assert msg is not None
    await cog.on_reaction_add(_react(msg), FakeMember(uid=21))
    assert await get_balance(21) == 500
    # Paying the last 500 emptied the budget, so the drop closed itself.
    assert msg.id not in _state.active_events


@pytest.mark.asyncio
async def test_bot_admin_event_is_unlimited(db, ctx, monkeypatch):
    _state.bot_admins.add(ctx.author.id)
    cog = EconomyCog(bot=None)
    msg = await _post(cog, ctx, "15k", monkeypatch)
    assert msg is not None and not _state.active_events[msg.id]["budget"]
    for uid in (31, 32):
        await cog.on_reaction_add(_react(msg), FakeMember(uid=uid))
    assert await get_balance(31) == await get_balance(32) == 15_000
    assert msg.id in _state.active_events
    assert "event_budget" not in _state.guild_settings.get(str(GID), {})


@pytest.mark.asyncio
async def test_racing_reactions_cannot_overdraw_the_budget(db, ctx, monkeypatch):
    """Two reactions interleaving inside add_balance: the budget is claimed
    synchronously before the await, so only one can be paid."""
    from src.cogs import economy_cog as mod
    real_add = mod.add_balance

    async def _yielding_add(uid, amount, **kw):
        await asyncio.sleep(0)
        return await real_add(uid, amount, **kw)
    monkeypatch.setattr(mod, "add_balance", _yielding_add)

    cog = EconomyCog(bot=None)
    msg = await _post(cog, ctx, "6k", monkeypatch)
    a, b = FakeMember(uid=41), FakeMember(uid=42)
    await asyncio.gather(cog.on_reaction_add(_react(msg), a), cog.on_reaction_add(_react(msg), b))

    assert sorted([await get_balance(a.id), await get_balance(b.id)]) == [0, 6_000]
    assert _state.guild_settings[str(GID)]["event_budget"]["spent"] == 6_000
    assert msg.id not in _state.active_events


@pytest.mark.asyncio
async def test_failed_payment_refunds_the_claim(db, ctx, monkeypatch):
    async def _boom(uid, amount, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr("src.cogs.economy_cog.add_balance", _boom)
    cog = EconomyCog(bot=None)
    msg = await _post(cog, ctx, "3k", monkeypatch)
    await cog.on_reaction_add(_react(msg), FakeMember(uid=51))
    assert _state.guild_settings[str(GID)]["event_budget"]["spent"] == 0
    assert 51 not in _state.active_events[msg.id]["rewarded"]


@pytest.mark.asyncio
async def test_event_in_the_dailies_channel_is_kept_until_the_reset(db, ctx, monkeypatch):
    _state.guild_settings[str(GID)] = {"dailies_channel": ctx.channel.id}
    msg = await _post(EconomyCog(bot=None), ctx, "1k", monkeypatch)
    assert _state.guild_settings[str(GID)]["dailies_keep_ids"] == [msg.id]


@pytest.mark.asyncio
async def test_event_elsewhere_is_not_added_to_the_keep_list(db, ctx, monkeypatch):
    _state.guild_settings[str(GID)] = {"dailies_channel": ctx.channel.id + 1}
    await _post(EconomyCog(bot=None), ctx, "1k", monkeypatch)
    assert "dailies_keep_ids" not in _state.guild_settings[str(GID)]
