"""The shared `crime` record: !steal, !mug and !bankheist all compete for one
"biggest crime payout" row rather than three separate ones.

The interesting parts are (a) that the three crimes share a single row and the
winner is whoever took the most, regardless of which crime did it, and (b) that
a bankheist is attributed to the whole crew — every participant who split the
cut — not just the host who opened the lobby.
"""
import asyncio
import random
import time

import pytest

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy
from src.cogs.economy_cog import (
    EconomyCog, CRIME_RECORD_CATEGORY, format_crime_record_detail,
)

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeMessage


pytestmark = pytest.mark.asyncio

GID = 42


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Skip the steal/mug chase animation delays."""
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop)


class _StubBot:
    def __init__(self):
        self.user = type("U", (), {"id": 999_999_999})()


def _grant_level(uid: int, internal_level: int) -> None:
    """Clear the level-10 crime-target gate (internal 9 → display 10)."""
    _state.leveling.setdefault(str(GID), {})[str(uid)] = {"xp": 0, "level": internal_level}
    if internal_level >= 9:
        _state.economy.setdefault("users", {}).setdefault(str(uid), {
            "balance": 0, "savings": [],
        })
        _state.economy["users"][str(uid)]["crime_eligible"] = True


def _seed_savings(uid: int, amount: int) -> None:
    """Drop a savings deposit straight into state; now() means ~zero interest."""
    _state.economy.setdefault("users", {}).setdefault(str(uid), {
        "balance": 0, "savings": [],
    })
    _state.economy["users"][str(uid)].setdefault("savings", []).append(
        {"amount": amount, "deposited_at": time.time()},
    )


def _make_ctx(author, content: str = "!steal @victim 1") -> FakeCtx:
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=GID))
    ctx.bot = _StubBot()
    ctx.message = FakeMessage(content=content)
    return ctx


def _make_hstate(host, target, joiners: list) -> dict:
    slots: list = [host, None, None, None]
    for i, j in enumerate(joiners[:3]):
        slots[i + 1] = j
    return {
        "host": host, "target": target, "slots": slots, "message": None,
        "opened_at": 0.0, "opened_at_wall": time.time(),
        "warned": False, "started": True, "cancelled": False,
    }


def _announced(channel) -> list:
    """Embeds announce_record pushed through channel.send (an AsyncMock)."""
    return [
        kw["embed"] for _, kw in channel.send.await_args_list
        if kw.get("embed") is not None
    ]


async def _crime_record() -> dict | None:
    return (await _persistence.load_records(GID)).get(CRIME_RECORD_CATEGORY)


# ── !steal ────────────────────────────────────────────────────────────────────

async def test_successful_steal_sets_the_crime_record(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=9100, display_name="thief")
    victim = FakeMember(uid=9200, display_name="victim")
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 9)

    # Tier 1: steal_chance 0.10, steal_pct 0.10 → roll 0.05 succeeds, 1,000 taken.
    monkeypatch.setattr(random, "random", lambda: 0.05)
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    ctx = _make_ctx(thief)
    await cog.cmd_steal.callback(cog, ctx, target=victim)

    rec = await _crime_record()
    assert rec["value"] == 1_000
    assert rec["holder_id"] == thief.id
    assert rec["holder_name"] == "thief"
    assert rec["crime_type"] == "steal"
    assert rec["victim"] == "victim"
    # A solo crime carries no crew.
    assert "crew" not in rec

    announced = _announced(ctx.channel)
    assert any(e.title == "🏆 New Record!" for e in announced)
    body = next(e.description for e in announced if e.title == "🏆 New Record!")
    assert "biggest crime payout" in body
    assert "1,000 🪙" in body
    assert "Steal • robbed victim" in body


async def test_caught_steal_sets_no_crime_record(db, monkeypatch):
    """A failed steal takes nothing off the victim, so it never competes."""
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=9101, display_name="thief")
    victim = FakeMember(uid=9201, display_name="victim")
    await _economy.add_balance(thief.id, 50_000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 9)

    rolls = iter([0.50, 0.99])  # steal roll fails, jail roll misses
    monkeypatch.setattr(random, "random", lambda: next(rolls))
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    await cog.cmd_steal.callback(cog, _make_ctx(thief), target=victim)

    assert await _crime_record() is None


async def test_steal_from_a_broke_victim_sets_no_crime_record(db, monkeypatch):
    """Success against an empty wallet moves 0 coins — not a record."""
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=9102, display_name="thief")
    victim = FakeMember(uid=9202, display_name="victim")
    await _economy._ensure_user(victim.id)
    _grant_level(victim.id, 9)

    monkeypatch.setattr(random, "random", lambda: 0.05)
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    await cog.cmd_steal.callback(cog, _make_ctx(thief), target=victim)

    assert await _crime_record() is None


# ── !mug ──────────────────────────────────────────────────────────────────────

async def test_successful_mug_sets_the_crime_record(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=9300, display_name="mugger")
    victim = FakeMember(uid=9400, display_name="mark")
    await _economy.add_balance(thief.id, 5_000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 12)

    monkeypatch.setattr(random, "random", lambda: 0.99)  # clean getaway
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    ctx = _make_ctx(thief, content="!mug @mark 1000")
    await cog.cmd_mug.callback(cog, ctx, target=victim, amount="1000")

    rec = await _crime_record()
    assert rec["value"] == 1_000
    assert rec["holder_id"] == thief.id
    assert rec["crime_type"] == "mug"
    assert rec["victim"] == "mark"
    assert "Mug • robbed mark" in next(
        e.description for e in _announced(ctx.channel) if e.title == "🏆 New Record!"
    )


async def test_a_caught_mug_still_sets_the_crime_record(db, monkeypatch):
    """Getting jailed doesn't give the victim their coins back, so the take
    still counts — same as it does for the crime graph."""
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=9301, display_name="mugger")
    victim = FakeMember(uid=9401, display_name="mark")
    await _economy.add_balance(thief.id, 5_000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 12)

    monkeypatch.setattr(random, "random", lambda: 0.10)  # witness calls the cops
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    ctx = _make_ctx(thief, content="!mug @mark 1000")
    await cog.cmd_mug.callback(cog, ctx, target=victim, amount="1000")

    assert _state.economy["users"][str(thief.id)]["jail_until"] > time.time()
    rec = await _crime_record()
    assert rec["value"] == 1_000
    assert rec["crime_type"] == "mug"


async def test_mug_records_only_what_the_victim_actually_lost(db, monkeypatch):
    """The victim's balance can drop during the ~5s animation; the record
    tracks the coins that actually moved, not the amount paid for."""
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=9302, display_name="mugger")
    victim = FakeMember(uid=9402, display_name="mark")
    await _economy.add_balance(thief.id, 20_000)
    await _economy.add_balance(victim.id, 5_000)
    _grant_level(victim.id, 12)

    monkeypatch.setattr(random, "random", lambda: 0.99)
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    real_sleep = asyncio.sleep

    async def _drain_victim(*a, **kw):
        # Empty most of the victim's wallet mid-chase.
        bal = await _economy.get_balance(victim.id)
        if bal > 2_000:
            await _economy.deduct_balance(victim.id, bal - 2_000)
        return await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _drain_victim)

    ctx = _make_ctx(thief, content="!mug @mark 5000")
    await cog.cmd_mug.callback(cog, ctx, target=victim, amount="5000")

    rec = await _crime_record()
    assert rec["value"] == 2_000


# ── !bankheist ────────────────────────────────────────────────────────────────

async def test_bankheist_credits_the_whole_crew(db, monkeypatch):
    """The pot is the record's value and every participant who split it is
    named, host first — a group score has a group holder."""
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=9500, display_name="host")
    j1 = FakeMember(uid=9501, display_name="j1")
    j2 = FakeMember(uid=9502, display_name="j2")
    j3 = FakeMember(uid=9503, display_name="j3")
    target = FakeMember(uid=9504, display_name="target")
    _seed_savings(target.id, 10_000)  # 20% = 2,000 → 500 each, no remainder

    monkeypatch.setattr(random, "random", lambda: 0.0)  # succeed, jail everyone

    ctx = _make_ctx(host, content="!bankheist @target")
    await cog._bankheist_resolve(ctx, _make_hstate(host, target, [j1, j2, j3]))

    rec = await _crime_record()
    assert rec["value"] == 2_000          # the whole pot, not one cut
    assert rec["holder_id"] == host.id    # a real uid for the tie-break
    assert rec["holder_name"] == "host, j1, j2, j3"
    assert rec["crime_type"] == "bankheist"
    assert rec["victim"] == "target"
    assert rec["crew"] == [
        {"id": 9500, "name": "host", "cut": 500},
        {"id": 9501, "name": "j1", "cut": 500},
        {"id": 9502, "name": "j2", "cut": 500},
        {"id": 9503, "name": "j3", "cut": 500},
    ]

    body = next(
        e.description for e in _announced(ctx.channel) if e.title == "🏆 New Record!"
    )
    assert "**host, j1, j2, j3** just set a new biggest crime payout record" in body
    assert "crew: host, j1, j2, j3 — 500 🪙 each" in body


async def test_solo_bankheist_still_records_a_one_person_crew(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=9510, display_name="loner")
    target = FakeMember(uid=9511, display_name="target")
    _seed_savings(target.id, 10_000)

    monkeypatch.setattr(random, "random", lambda: 0.0)

    ctx = _make_ctx(host, content="!bankheist @target")
    await cog._bankheist_resolve(ctx, _make_hstate(host, target, []))

    rec = await _crime_record()
    assert rec["holder_name"] == "loner"
    assert rec["crew"] == [{"id": 9510, "name": "loner", "cut": 2_000}]


async def test_failed_bankheist_sets_no_crime_record(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=9520, display_name="host")
    target = FakeMember(uid=9521, display_name="target")
    _seed_savings(target.id, 10_000)

    monkeypatch.setattr(random, "random", lambda: 0.99)  # miss the chance roll

    ctx = _make_ctx(host, content="!bankheist @target")
    await cog._bankheist_resolve(ctx, _make_hstate(host, target, []))

    assert await _crime_record() is None


async def test_empty_vault_bankheist_sets_no_crime_record(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=9530, display_name="host")
    target = FakeMember(uid=9531, display_name="target")
    await _economy._ensure_user(target.id)

    monkeypatch.setattr(random, "random", lambda: 0.0)

    ctx = _make_ctx(host, content="!bankheist @target")
    result = await cog._bankheist_resolve(ctx, _make_hstate(host, target, []))

    assert "Empty Vault" in result.title
    assert await _crime_record() is None


# ── one row, three crimes ─────────────────────────────────────────────────────

async def test_a_bigger_mug_takes_the_record_from_a_steal(db, monkeypatch):
    """All three crimes write the same row, so the biggest haul wins outright
    no matter which crime produced it."""
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=9600, display_name="thief")
    victim = FakeMember(uid=9601, display_name="victim")
    await _economy.add_balance(thief.id, 50_000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 12)

    monkeypatch.setattr(random, "random", lambda: 0.05)  # steal succeeds
    monkeypatch.setattr(random, "randint", lambda a, b: a)
    await cog.cmd_steal.callback(cog, _make_ctx(thief), target=victim)
    assert (await _crime_record())["crime_type"] == "steal"

    # Victim is down to 9,000; mug 3,000 of it — beats the 1,000 steal.
    mugger = FakeMember(uid=9602, display_name="mugger")
    await _economy.add_balance(mugger.id, 5_000)
    monkeypatch.setattr(random, "random", lambda: 0.99)  # clean getaway
    await cog.cmd_mug.callback(
        cog, _make_ctx(mugger, content="!mug @victim 3000"),
        target=victim, amount="3000",
    )

    rec = await _crime_record()
    assert rec["value"] == 3_000
    assert rec["crime_type"] == "mug"
    assert rec["holder_name"] == "mugger"


async def test_a_smaller_crime_leaves_the_record_alone(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    await _persistence.save_records(GID, {
        CRIME_RECORD_CATEGORY: {
            "value": 500_000, "holder_id": 1, "holder_name": "kingpin",
            "crime_type": "bankheist", "victim": "whale",
        },
    })

    thief = FakeMember(uid=9610, display_name="thief")
    victim = FakeMember(uid=9611, display_name="victim")
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 9)
    monkeypatch.setattr(random, "random", lambda: 0.05)
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    ctx = _make_ctx(thief)
    await cog.cmd_steal.callback(cog, ctx, target=victim)

    rec = await _crime_record()
    assert rec["holder_name"] == "kingpin"
    assert rec["value"] == 500_000
    assert not any(e.title == "🏆 New Record!" for e in _announced(ctx.channel))


# ── detail rendering ──────────────────────────────────────────────────────────

async def test_detail_collapses_equal_cuts_into_one_each_figure():
    detail = format_crime_record_detail({
        "crime_type": "bankheist", "victim": "target",
        "crew": [{"name": "a", "cut": 500}, {"name": "b", "cut": 500}],
    })
    assert detail == "Bank Heist • robbed target • crew: a, b — 500 🪙 each"


async def test_detail_spells_out_cuts_when_the_host_remainder_splits_them():
    """seized % party_size goes to the host, so the cuts aren't always equal."""
    detail = format_crime_record_detail({
        "crime_type": "bankheist", "victim": "target",
        "crew": [{"name": "host", "cut": 335}, {"name": "b", "cut": 334}],
    })
    assert detail == "Bank Heist • robbed target • crew: host 335 🪙, b 334 🪙"


async def test_detail_renders_solo_crimes_without_a_crew_clause():
    assert format_crime_record_detail(
        {"crime_type": "steal", "victim": "victim"},
    ) == "Steal • robbed victim"
    assert format_crime_record_detail(
        {"crime_type": "mug", "victim": "mark"},
    ) == "Mug • robbed mark"


async def test_detail_degrades_gracefully_on_a_row_missing_its_meta():
    assert format_crime_record_detail({}) == "Crime"
    assert format_crime_record_detail({"crime_type": "steal"}) == "Steal"


# ── !records rendering ────────────────────────────────────────────────────────

async def test_records_embed_renders_the_crime_line_with_the_crew(db):
    await _persistence.save_records(GID, {
        CRIME_RECORD_CATEGORY: {
            "value": 2_000, "holder_id": 9500, "holder_name": "host, j1, j2",
            "crime_type": "bankheist", "victim": "target",
            "crew": [
                {"id": 9500, "name": "host", "cut": 666},
                {"id": 9501, "name": "j1", "cut": 667},
                {"id": 9502, "name": "j2", "cut": 667},
            ],
        },
    })

    cog = EconomyCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=1), guild=FakeGuild(gid=GID))
    await cog.cmd_records.callback(cog, ctx)

    desc = ctx.sent_embeds[-1].description
    assert "**Crime Payout:** 2,000 🪙 — **host, j1, j2**" in desc
    assert "↳ Bank Heist • robbed target • crew: host 666 🪙, j1 667 🪙, j2 667 🪙" in desc


async def test_records_embed_shows_the_crime_line_as_empty_before_any_crime(db):
    cog = EconomyCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=1), guild=FakeGuild(gid=GID))
    await cog.cmd_records.callback(cog, ctx)

    assert "**Crime Payout:** *none yet*" in ctx.sent_embeds[-1].description
