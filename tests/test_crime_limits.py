"""Take-based jail sentences for every crime, and the one-successful-heist-a-day
limit. Steal, mug and heist jail tests pinning a specific sentence live in
test_money_flows.py; this file covers the brackets and the heist claim."""
import asyncio
import random
import time

import pytest

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy
import src.cogs.economy_cog as _economy_cog
from src.cogs.economy_cog import EconomyCog, crime_jail_seconds, _heisted_today
from src.economy import _ct_today

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild
from tests.test_money_flows import (
    _StubBot, _grant_level, _seed_savings, _make_hstate, _make_ctx, _lobby_ctx,
)


HOUR = 3600
DAY = 86_400
_real_sleep = asyncio.sleep  # captured before the autouse fixture patches it


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop)


async def _read_db_heist_day(uid: int):
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT heist_success_day FROM economy_users WHERE user_id=?", (uid,))
            row = await cur.fetchone()
    return row[0] if row else None


def _mark_heisted(uid: int, day: str | None = None) -> None:
    _state.economy.setdefault("users", {}).setdefault(str(uid), {"balance": 0, "savings": []})
    _state.economy["users"][str(uid)]["heist_success_day"] = day or _ct_today()


# ── sentences ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("take, secs", [
    (0, 12 * HOUR), (4_999, 12 * HOUR),
    (5_000, DAY), (24_999, DAY),
    (25_000, 2 * DAY), (99_999, 2 * DAY),
    (100_000, 3 * DAY), (249_999, 3 * DAY),
    (250_000, 4 * DAY), (10 ** 9, 4 * DAY),
])
def test_sentence_brackets(take, secs):
    assert crime_jail_seconds(take) == secs


async def test_a_big_mug_gets_a_longer_sentence(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=5_001)
    victim = FakeMember(uid=5_002, display_name="victim")
    await _economy.add_balance(thief.id, 50_000)
    await _economy.add_balance(victim.id, 100_000)
    _grant_level(victim.id, 12)
    monkeypatch.setattr(random, "random", lambda: 0.10)  # jailed
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    before = time.time()
    ctx = FakeCtx(author=thief, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    await cog.cmd_mug.callback(cog, ctx, target=victim, amount="30000")

    jail_until = _state.economy["users"][str(thief.id)]["jail_until"]
    assert before + 2 * DAY - 60 < jail_until < before + 2 * DAY + 60


async def test_a_big_heist_share_gets_a_longer_sentence(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=5_011, display_name="host")
    target = FakeMember(uid=5_012, display_name="target")
    _seed_savings(target.id, 1_000_000)  # 20% = 200k, solo → the 3-day bracket
    monkeypatch.setattr(random, "random", lambda: 0.0)  # success, then jailed

    before = time.time()
    result = await cog._bankheist_resolve(_make_ctx(host, target, "!bankheist @target"), _make_hstate(host, target, []))

    jail_until = _state.economy["users"][str(host.id)]["jail_until"]
    assert before + 3 * DAY - 60 < jail_until < before + 3 * DAY + 60
    assert "jailed for 3d" in result.description


# ── one successful heist a day ───────────────────────────────────────────────

def test_a_success_on_an_earlier_day_does_not_count():
    _mark_heisted(5_020, "2000-01-01")
    assert not _heisted_today(5_020)
    _mark_heisted(5_020)
    assert _heisted_today(5_020)


async def test_a_successful_heist_claims_the_day_for_the_crew(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=5_031, display_name="host")
    j1 = FakeMember(uid=5_032, display_name="j1")
    target = FakeMember(uid=5_033, display_name="target")
    _seed_savings(target.id, 10_000)
    monkeypatch.setattr(random, "random", lambda: 0.0)

    await cog._bankheist_resolve(_make_ctx(host, target, "!bankheist @target"), _make_hstate(host, target, [j1]))

    for uid in (host.id, j1.id):
        assert _heisted_today(uid)
        assert await _read_db_heist_day(uid) == _ct_today()


async def test_a_failed_heist_leaves_the_day_open(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=5_041, display_name="host")
    target = FakeMember(uid=5_042, display_name="target")
    _seed_savings(target.id, 10_000)
    monkeypatch.setattr(random, "random", lambda: 0.99)

    result = await cog._bankheist_resolve(_make_ctx(host, target, "!bankheist @target"), _make_hstate(host, target, []))

    assert "Failed" in result.title
    assert not _heisted_today(host.id)


async def test_an_empty_vault_hands_the_day_back(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=5_051, display_name="host")
    target = FakeMember(uid=5_052, display_name="target")
    monkeypatch.setattr(random, "random", lambda: 0.0)

    result = await cog._bankheist_resolve(_make_ctx(host, target, "!bankheist @target"), _make_hstate(host, target, []))

    assert "Empty Vault" in result.title
    assert not _heisted_today(host.id)


async def test_a_host_who_heisted_today_cannot_open_a_lobby(db):
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=5_061, display_name="host")
    target = FakeMember(uid=5_062, display_name="target")
    _grant_level(target.id, 9)
    _mark_heisted(host.id)

    ctx = _make_ctx(host, target, "!bankheist @target")
    await cog.cmd_bankheist.callback(cog, ctx, target=target)

    assert any(getattr(e, "title", "") == "🏦 One Heist a Day" for e in ctx.sent_embeds)
    assert ctx.channel.id not in cog._active_heists


async def test_a_player_who_heisted_today_cannot_join(db):
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=5_071, display_name="host")
    victim = FakeMember(uid=5_072, display_name="victim")
    done = FakeMember(uid=5_073, display_name="done")
    free = FakeMember(uid=5_074, display_name="free")
    _grant_level(victim.id, 9)
    _mark_heisted(done.id)
    ctx, captured = _lobby_ctx(cog, host, victim, ("Join", done), ("Join", free), ("Cancel", host))

    await cog.cmd_bankheist.callback(cog, ctx, target=victim)

    slots = captured["hstate"]["slots"]
    assert slots[1] is free and done not in slots


async def test_resolve_benches_a_crew_member_who_heisted_since_joining(db, monkeypatch):
    """A joiner whose heist in another channel landed while this lobby was
    open sits this one out: no cut, no jail roll, and the pot isn't split
    their way."""
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=5_081, display_name="host")
    j1 = FakeMember(uid=5_082, display_name="j1")
    target = FakeMember(uid=5_083, display_name="target")
    _seed_savings(target.id, 10_001)
    _mark_heisted(j1.id)
    monkeypatch.setattr(random, "random", lambda: 0.0)

    result = await cog._bankheist_resolve(_make_ctx(host, target, "!bankheist @target"), _make_hstate(host, target, [j1]))

    assert "Successful" in result.title
    assert "sat this one out" in result.description
    assert await _economy.get_balance(host.id) == 2_000  # the whole pot
    assert await _economy.get_balance(j1.id) == 0
    assert _state.economy["users"][str(j1.id)].get("jail_until", 0) <= time.time()


async def test_resolve_is_called_off_when_the_host_heisted_since_opening(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=5_091, display_name="host")
    target = FakeMember(uid=5_092, display_name="target")
    _seed_savings(target.id, 10_000)
    _mark_heisted(host.id)
    monkeypatch.setattr(random, "random", lambda: 0.0)

    result = await cog._bankheist_resolve(_make_ctx(host, target, "!bankheist @target"), _make_hstate(host, target, []))

    assert result.title == "🏦 Heist Called Off"
    assert await _economy.get_balance(host.id) == 0
    assert 9_999 <= await _economy.get_savings_value(target.id) <= 10_001


async def test_two_heists_resolving_at_once_pay_the_host_once(db, monkeypatch):
    """Two lobbies in different channels with the same host resolve
    together. The savings read yields, so both reach the limit check; the
    claim on the winning roll must stop the second."""
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=5_101, display_name="host")
    t1 = FakeMember(uid=5_102, display_name="t1")
    t2 = FakeMember(uid=5_103, display_name="t2")
    _seed_savings(t1.id, 10_000)
    _seed_savings(t2.id, 10_000)
    monkeypatch.setattr(random, "random", lambda: 0.0)
    real = _economy_cog.get_savings_value

    async def _yielding(uid):
        await _real_sleep(0)  # a real event-loop yield; asyncio.sleep is patched out
        return await real(uid)
    monkeypatch.setattr(_economy_cog, "get_savings_value", _yielding)

    results = await asyncio.gather(
        cog._bankheist_resolve(_make_ctx(host, t1, "!bankheist @t1"), _make_hstate(host, t1, [])),
        cog._bankheist_resolve(_make_ctx(host, t2, "!bankheist @t2"), _make_hstate(host, t2, [])),
    )

    titles = sorted(r.title for r in results)
    assert titles == ["🏦 Heist Called Off", "🏦 Heist Successful!"]
    assert await _economy.get_balance(host.id) == 2_000
