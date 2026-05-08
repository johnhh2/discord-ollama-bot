"""Tier 1 money-flow tests: !steal, !mug, !pay, !jailbreak.

These commands move real coins through multi-step transactions (deduct fee →
roll outcome → debit/refund → save). The user's balance can move in either
direction depending on rolls and insurance state. We drive each branch with
monkeypatched `random.random` and assert both in-memory state AND DB rows.

`asyncio.sleep` is patched to a no-op so the chase animations don't slow the
suite; the *outcome* is what matters, not the frame timing.
"""
import asyncio
import random
import time

import pytest

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy
from src.cogs.economy_cog import EconomyCog

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeMessage


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Skip the chase animation delays so steal/mug tests don't take seconds."""
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop)


class _StubBotUser:
    def __init__(self, uid: int = 999_999_999):
        self.id = uid


class _StubBot:
    def __init__(self):
        self.user = _StubBotUser()


async def _read_db_balance(uid: int) -> int | None:
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT balance FROM economy_users WHERE user_id=?", (uid,))
            row = await cur.fetchone()
    return row[0] if row else None


async def _read_db_jail_until(uid: int) -> float:
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT jail_until FROM economy_users WHERE user_id=?", (uid,))
            row = await cur.fetchone()
    return row[0] if row else 0.0


async def _read_db_jail_reason(uid: int) -> str | None:
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT jail_reason FROM economy_users WHERE user_id=?", (uid,))
            row = await cur.fetchone()
    return row[0] if row else None


def _make_ctx(thief: FakeMember, victim: FakeMember, content: str = "!steal @victim"):
    """Build a FakeCtx with the bot stubbed and ctx.message.content set so
    cmd_steal's tier-parsing can read it."""
    ctx = FakeCtx(author=thief, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    ctx.message = FakeMessage(content=content)
    # cmd_steal calls ctx.invoke(self.cmd_crime) when target is None — but our
    # tests always pass a target so that branch isn't exercised.
    return ctx


def _grant_level(uid: int, internal_level: int, gid: int = 42) -> None:
    """Force a user to a given internal level so they pass the !steal/!mug/!bankheist
    target-side level gate. Internal level N → display level N+1; steal needs
    display 10 (internal 9), mug needs display 13 (internal 12), bankheist needs
    display 15 (internal 14)."""
    _state.leveling.setdefault(str(gid), {})[str(uid)] = {"xp": 0, "level": internal_level}


# ── !steal ────────────────────────────────────────────────────────────────────

async def test_steal_success_transfers_coins_and_persists(db, monkeypatch):
    """Lucky steal: thief gets `steal_pct * victim_balance`, victim debited, both persisted."""
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=100, display_name="thief")
    victim = FakeMember(uid=200, display_name="victim")
    await _economy.add_balance(thief.id, 5000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 9)  # unlock !steal for victim

    # Tier 1: steal_chance=0.10, steal_pct=0.10. Roll < 0.10 -> success.
    monkeypatch.setattr(random, "random", lambda: 0.05)
    # randint is called for cop_steps decoration; keep it deterministic.
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    ctx = _make_ctx(thief, victim, content="!steal @victim")
    await cog.cmd_steal.callback(cog, ctx, target=victim)

    expected_steal = int(10_000 * 0.10)  # 1000
    assert await _economy.get_balance(thief.id) == 5000 + expected_steal
    assert await _economy.get_balance(victim.id) == 10_000 - expected_steal
    assert await _read_db_balance(thief.id) == 5000 + expected_steal
    assert await _read_db_balance(victim.id) == 10_000 - expected_steal
    assert thief.id not in cog._crime_active  # lock released


async def test_steal_caught_and_jailed_deducts_fee_and_sets_jail(db, monkeypatch):
    """Failed steal + bad jail roll: thief pays fee, jail_until set 1 day out."""
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=101, display_name="thief")
    victim = FakeMember(uid=201, display_name="victim")
    starting = 5000
    await _economy.add_balance(thief.id, starting)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 9)

    # Tier 1: steal_chance=0.10, jail_chance=0.25, fee=1000, jail_days=1.
    # First random.random() = steal roll (need >= 0.10 to fail).
    # Second random.random() = jail roll (need < 0.25 to be jailed).
    rolls = iter([0.50, 0.10])
    monkeypatch.setattr(random, "random", lambda: next(rolls))
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    before = time.time()
    ctx = _make_ctx(thief, victim, content="!steal @victim")
    await cog.cmd_steal.callback(cog, ctx, target=victim)

    assert await _economy.get_balance(thief.id) == starting - 1000
    assert await _read_db_balance(thief.id) == starting - 1000
    # Victim not touched on failure
    assert await _economy.get_balance(victim.id) == 10_000

    jail_until = _state.economy["users"][str(thief.id)]["jail_until"]
    assert jail_until > before + 86000  # at least a day from now (give or take)
    assert jail_until < before + 87000
    assert await _read_db_jail_until(thief.id) == pytest.approx(jail_until)
    # Reason captured at jail time, persisted to DB
    assert _state.economy["users"][str(thief.id)]["jail_reason"] == "Tried to steal from victim"
    assert await _read_db_jail_reason(thief.id) == "Tried to steal from victim"


async def test_steal_caught_no_jail_just_fee(db, monkeypatch):
    """Failed steal but lucky jail roll: pay fee only."""
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=102)
    victim = FakeMember(uid=202)
    await _economy.add_balance(thief.id, 5000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 9)

    # Steal fails (>= 0.10), jail also fails (>= 0.25)
    rolls = iter([0.99, 0.99])
    monkeypatch.setattr(random, "random", lambda: next(rolls))
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    ctx = _make_ctx(thief, victim)
    await cog.cmd_steal.callback(cog, ctx, target=victim)

    assert await _economy.get_balance(thief.id) == 4000  # -1000 fee
    assert _state.economy["users"][str(thief.id)].get("jail_until", 0) == 0


async def test_steal_blocked_by_insurance_no_money_moves(db, monkeypatch):
    """Insured victim → thief sees the protected embed; no balance changes."""
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=103)
    victim = FakeMember(uid=203)
    await _economy.add_balance(thief.id, 5000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 9)
    _state.insurance[str(victim.id)] = {
        "expires_at": time.time() + 3600,
        "protected_from": ["steal"],
    }

    ctx = _make_ctx(thief, victim)
    await cog.cmd_steal.callback(cog, ctx, target=victim)

    assert await _economy.get_balance(thief.id) == 5000
    assert await _economy.get_balance(victim.id) == 10_000
    assert any("Protected" in (e.title or "") for e in ctx.sent_embeds)


async def test_steal_self_target_rejected(db):
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=104)
    await _economy.add_balance(thief.id, 5000)
    ctx = _make_ctx(thief, thief)
    await cog.cmd_steal.callback(cog, ctx, target=thief)
    assert await _economy.get_balance(thief.id) == 5000
    assert any("can't steal from yourself" in m for m in ctx.sent_messages)


async def test_steal_while_jailed_blocked(db):
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=105)
    victim = FakeMember(uid=205)
    await _economy.add_balance(thief.id, 5000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 9)
    _state.economy["users"][str(thief.id)]["jail_until"] = time.time() + 3600

    ctx = _make_ctx(thief, victim)
    await cog.cmd_steal.callback(cog, ctx, target=victim)

    assert await _economy.get_balance(thief.id) == 5000
    assert await _economy.get_balance(victim.id) == 10_000
    assert any("In Jail" in (e.title or "") or "in Jail" in (e.title or "")
               for e in ctx.sent_embeds)


async def test_steal_rigged_force_success(db, monkeypatch):
    """rigged_steal[uid] forces a guaranteed success and decrements the counter."""
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=106)
    victim = FakeMember(uid=206)
    await _economy.add_balance(thief.id, 5000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 9)
    _state.rigged_steal[thief.id] = 2

    # Even with random.random() = 0.99 (would normally fail), rigged forces success.
    monkeypatch.setattr(random, "random", lambda: 0.99)
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    ctx = _make_ctx(thief, victim)
    await cog.cmd_steal.callback(cog, ctx, target=victim)

    assert await _economy.get_balance(thief.id) > 5000  # gained from steal
    assert _state.rigged_steal[thief.id] == 1  # decremented


async def test_steal_concurrent_lock_blocks_second_attempt(db):
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=107)
    victim = FakeMember(uid=207)
    await _economy.add_balance(thief.id, 5000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 9)
    cog._crime_active.add(thief.id)  # simulate one already running

    ctx = _make_ctx(thief, victim)
    await cog.cmd_steal.callback(cog, ctx, target=victim)

    # Balance untouched; rejected with "Already Running" embed.
    assert await _economy.get_balance(thief.id) == 5000
    assert any("Already Running" in (e.title or "") for e in ctx.sent_embeds)


# ── !mug ──────────────────────────────────────────────────────────────────────

async def test_mug_clean_getaway_target_loses_amount(db, monkeypatch):
    """Successful mug: thief pays cost, target loses `amount`; muggers keep the
    payment (i.e. the upfront cost is gone, not refunded to thief)."""
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=300)
    victim = FakeMember(uid=400)
    cog_bot = _StubBot()

    await _economy.add_balance(thief.id, 5000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 12)  # unlock !mug for victim

    # No jail (random >= 0.5).
    monkeypatch.setattr(random, "random", lambda: 0.99)
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    ctx = FakeCtx(author=thief, guild=FakeGuild(gid=42))
    ctx.bot = cog_bot
    await cog.cmd_mug.callback(cog, ctx, target=victim, amount="1000")

    # Thief paid 1000 upfront; that's gone. Doesn't get the 1000 back.
    assert await _economy.get_balance(thief.id) == 5000 - 1000
    assert await _economy.get_balance(victim.id) == 10_000 - 1000
    assert await _read_db_balance(thief.id) == 4000


async def test_mug_caught_jails_thief_for_one_day(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=301)
    victim = FakeMember(uid=401, display_name="victim")
    await _economy.add_balance(thief.id, 5000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 12)

    # First random.random < 0.5 -> jailed.
    monkeypatch.setattr(random, "random", lambda: 0.10)
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    before = time.time()
    ctx = FakeCtx(author=thief, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    await cog.cmd_mug.callback(cog, ctx, target=victim, amount="1000")

    # Thief still pays AND is jailed AND target still loses (per the cog logic).
    assert await _economy.get_balance(thief.id) == 4000
    assert await _economy.get_balance(victim.id) == 9000
    jail_until = _state.economy["users"][str(thief.id)]["jail_until"]
    assert before + 86000 < jail_until < before + 87000
    # Reason captured at jail time, persisted to DB
    assert _state.economy["users"][str(thief.id)]["jail_reason"] == "Mugged victim for 1,000 coins"
    assert await _read_db_jail_reason(thief.id) == "Mugged victim for 1,000 coins"


async def test_mug_blocked_by_insurance_no_charge(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=302)
    victim = FakeMember(uid=402)
    await _economy.add_balance(thief.id, 5000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 12)
    _state.insurance[str(victim.id)] = {
        "expires_at": time.time() + 3600,
        "protected_from": ["steal"],
    }

    ctx = FakeCtx(author=thief, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    await cog.cmd_mug.callback(cog, ctx, target=victim, amount="500")

    # Insurance check happens before shop_charge — no money moves.
    assert await _economy.get_balance(thief.id) == 5000
    assert await _economy.get_balance(victim.id) == 10_000


async def test_mug_target_too_poor_relative_to_thief(db, monkeypatch):
    """Target with <20% of thief's balance is rejected."""
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=303)
    victim = FakeMember(uid=403)
    await _economy.add_balance(thief.id, 10_000)
    await _economy.add_balance(victim.id, 100)  # 1% of thief's
    _grant_level(victim.id, 12)

    ctx = FakeCtx(author=thief, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    await cog.cmd_mug.callback(cog, ctx, target=victim, amount="50")

    assert await _economy.get_balance(thief.id) == 10_000
    assert any("Too Easy" in (e.title or "") for e in ctx.sent_embeds)


async def test_mug_insufficient_funds_blocked_before_action(db):
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=304)
    victim = FakeMember(uid=404)
    await _economy.add_balance(thief.id, 100)  # not enough to pay muggers
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 12)

    ctx = FakeCtx(author=thief, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    await cog.cmd_mug.callback(cog, ctx, target=victim, amount="500")

    # shop_charge sends Insufficient Funds, no money moves.
    assert await _economy.get_balance(thief.id) == 100
    assert await _economy.get_balance(victim.id) == 10_000


# ── !steal / !mug crime-event recording (powers !graph crime) ────────────────
#
# These pin the side-effect that the standalone test_crime_graph.py can't —
# that cmd_steal and cmd_mug actually CALL record_crime_event at every outcome
# branch. If a future refactor accidentally drops one of these calls, the
# unit-level "record_crime_event aggregates correctly" tests would still pass
# while the graph silently goes blank in prod.


async def test_steal_success_records_thief_gained_and_victim_lost(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=1100, display_name="thief")
    victim = FakeMember(uid=1200, display_name="victim")
    await _economy.add_balance(thief.id, 5000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 9)

    monkeypatch.setattr(random, "random", lambda: 0.05)  # tier 1 success
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    ctx = _make_ctx(thief, victim)
    await cog.cmd_steal.callback(cog, ctx, target=victim)

    expected_steal = int(10_000 * 0.10)
    thief_rec = _state.crime_today_by_user.get(str(thief.id), {})
    victim_rec = _state.crime_today_by_user.get(str(victim.id), {})
    assert thief_rec.get("gained") == expected_steal
    assert thief_rec.get("lost", 0) == 0
    assert victim_rec.get("lost") == expected_steal
    assert victim_rec.get("gained", 0) == 0


async def test_steal_fail_records_thief_lost_only(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=1101)
    victim = FakeMember(uid=1201)
    await _economy.add_balance(thief.id, 5000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 9)

    # Steal fails (>= 0.10), jail also fails — thief just pays the 1000 fee.
    rolls = iter([0.99, 0.99])
    monkeypatch.setattr(random, "random", lambda: next(rolls))
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    ctx = _make_ctx(thief, victim)
    await cog.cmd_steal.callback(cog, ctx, target=victim)

    thief_rec = _state.crime_today_by_user.get(str(thief.id), {})
    assert thief_rec.get("lost") == 1000
    assert thief_rec.get("gained", 0) == 0
    # Victim's row should not exist on a failed steal — they weren't touched.
    assert str(victim.id) not in _state.crime_today_by_user


async def test_mug_records_both_attacker_lost_and_victim_lost(db, monkeypatch):
    """Mug semantics: attacker pays the muggers' fee (lost), victim is robbed
    (lost), nobody gains — the muggers keep it all."""
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=1300)
    victim = FakeMember(uid=1400)
    await _economy.add_balance(thief.id, 5000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 12)

    monkeypatch.setattr(random, "random", lambda: 0.99)  # no jail
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    ctx = FakeCtx(author=thief, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    await cog.cmd_mug.callback(cog, ctx, target=victim, amount="1000")

    thief_rec = _state.crime_today_by_user.get(str(thief.id), {})
    victim_rec = _state.crime_today_by_user.get(str(victim.id), {})
    assert thief_rec.get("lost") == 1000   # paid muggers
    assert thief_rec.get("gained", 0) == 0
    assert victim_rec.get("lost") == 1000  # robbed
    assert victim_rec.get("gained", 0) == 0


# ── !pay ──────────────────────────────────────────────────────────────────────

async def test_pay_transfers_coins_and_persists(db):
    cog = EconomyCog(bot=_StubBot())
    sender = FakeMember(uid=500)
    recipient = FakeMember(uid=600)
    await _economy.add_balance(sender.id, 5000)

    ctx = FakeCtx(author=sender, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    await cog.cmd_pay.callback(cog, ctx, recipient=recipient, amount="1500")

    assert await _economy.get_balance(sender.id) == 3500
    assert await _economy.get_balance(recipient.id) == 1500
    assert await _read_db_balance(sender.id) == 3500
    assert await _read_db_balance(recipient.id) == 1500


async def test_pay_percent_amount_uses_caller_balance(db):
    """`!pay @user 50%` resolves to half of the sender's current balance."""
    cog = EconomyCog(bot=_StubBot())
    sender = FakeMember(uid=501)
    recipient = FakeMember(uid=601)
    await _economy.add_balance(sender.id, 4000)

    ctx = FakeCtx(author=sender, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    await cog.cmd_pay.callback(cog, ctx, recipient=recipient, amount="50%")

    assert await _economy.get_balance(sender.id) == 2000
    assert await _economy.get_balance(recipient.id) == 2000


async def test_pay_self_blocked(db):
    cog = EconomyCog(bot=_StubBot())
    sender = FakeMember(uid=502)
    await _economy.add_balance(sender.id, 5000)

    ctx = FakeCtx(author=sender, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    await cog.cmd_pay.callback(cog, ctx, recipient=sender, amount="100")

    assert await _economy.get_balance(sender.id) == 5000


async def test_pay_insufficient_funds_no_transfer(db):
    cog = EconomyCog(bot=_StubBot())
    sender = FakeMember(uid=503)
    recipient = FakeMember(uid=603)
    await _economy.add_balance(sender.id, 100)

    ctx = FakeCtx(author=sender, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    await cog.cmd_pay.callback(cog, ctx, recipient=recipient, amount="5000")

    assert await _economy.get_balance(sender.id) == 100
    assert await _economy.get_balance(recipient.id) == 0


async def test_pay_to_bot_user_credits_guild_house(db):
    """Paying the bot routes to the guild house pot, not to a user balance."""
    cog = EconomyCog(bot=_StubBot())
    sender = FakeMember(uid=504)
    bot_user = FakeMember(uid=999_999_999)
    await _economy.add_balance(sender.id, 5000)

    ctx = FakeCtx(author=sender, guild=FakeGuild(gid=77))
    ctx.bot = _StubBot()  # bot.user.id == 999_999_999, matches recipient

    await cog.cmd_pay.callback(cog, ctx, recipient=bot_user, amount="1000")

    assert await _economy.get_balance(sender.id) == 4000
    assert _economy.get_guild_house_balance(77) == 1000


# ── !jailbreak ────────────────────────────────────────────────────────────────

async def test_jailbreak_success_clears_jail_until(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    user = FakeMember(uid=700)
    await _economy._ensure_user(user.id)
    _state.economy["users"][str(user.id)]["jail_until"] = time.time() + 86400
    _state.economy["users"][str(user.id)]["jail_reason"] = "Tried to steal from someone"

    monkeypatch.setattr(random, "random", lambda: 0.10)  # < 0.20 -> success

    ctx = FakeCtx(author=user, guild=FakeGuild(gid=42))
    await cog.cmd_jailbreak.callback(cog, ctx)

    user_data = _state.economy["users"][str(user.id)]
    assert user_data["jail_until"] == 0
    assert user_data["jailbreak_used"] is True
    assert user_data["jail_reason"] is None
    # Persisted
    assert await _read_db_jail_until(user.id) == 0
    assert await _read_db_jail_reason(user.id) is None


async def test_jailbreak_failure_keeps_jail_and_marks_used(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    user = FakeMember(uid=701)
    await _economy._ensure_user(user.id)
    jail_until = time.time() + 86400
    _state.economy["users"][str(user.id)]["jail_until"] = jail_until
    _state.economy["users"][str(user.id)]["jail_reason"] = "Tried to steal from someone"

    monkeypatch.setattr(random, "random", lambda: 0.99)  # >= 0.20 -> failure

    ctx = FakeCtx(author=user, guild=FakeGuild(gid=42))
    await cog.cmd_jailbreak.callback(cog, ctx)

    user_data = _state.economy["users"][str(user.id)]
    assert user_data["jail_until"] == pytest.approx(jail_until)
    assert user_data["jailbreak_used"] is True
    # Reason still set on a failed jailbreak — only success clears it
    assert user_data["jail_reason"] == "Tried to steal from someone"


async def test_jailbreak_when_not_jailed_rejected(db):
    cog = EconomyCog(bot=_StubBot())
    user = FakeMember(uid=702)
    await _economy._ensure_user(user.id)
    # jail_until defaults to 0 = not jailed

    ctx = FakeCtx(author=user, guild=FakeGuild(gid=42))
    await cog.cmd_jailbreak.callback(cog, ctx)

    user_data = _state.economy["users"][str(user.id)]
    # jailbreak_used stays False — the daily attempt isn't burned.
    assert user_data.get("jailbreak_used", False) is False


async def test_jailbreak_already_used_today_rejected(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    user = FakeMember(uid=703)
    await _economy._ensure_user(user.id)
    _state.economy["users"][str(user.id)]["jail_until"] = time.time() + 86400
    _state.economy["users"][str(user.id)]["jailbreak_used"] = True
    # Make sure today's reset has already happened so do_daily_reset isn't
    # triggered (which would clear jailbreak_used).
    _state.economy["last_daily_reset"] = _economy._ct_today()

    monkeypatch.setattr(random, "random", lambda: 0.0)  # would succeed if allowed

    ctx = FakeCtx(author=user, guild=FakeGuild(gid=42))
    await cog.cmd_jailbreak.callback(cog, ctx)

    # Still jailed — the attempt was rejected before the random roll.
    assert _state.economy["users"][str(user.id)]["jail_until"] > time.time()


# ── !bankheist ────────────────────────────────────────────────────────────────
# These exercise the resolution path (`_bankheist_resolve`) directly rather
# than going through the reaction lobby loop. The lobby plumbing is just
# `bot.wait_for` glue; the interesting logic — chance formula, slot exclusion,
# loot split, savings drain, jail-on-fail — all lives in helpers we can call.

def _seed_savings(uid: int, amount: int):
    """Drop a savings deposit directly into state, bypassing the wallet
    deduction in `add_savings`. Uses now() so 1% daily interest is ~zero."""
    _state.economy.setdefault("users", {}).setdefault(str(uid), {
        "balance": 0, "savings": [],
    })
    _state.economy["users"][str(uid)].setdefault("savings", []).append(
        {"amount": amount, "deposited_at": time.time()},
    )


def _make_hstate(host, target, joiners: list):
    """Build the heist state dict that _bankheist_resolve consumes."""
    slots: list = [host, None, None, None]
    for i, j in enumerate(joiners[:3]):
        slots[i + 1] = j
    return {
        "host": host,
        "target": target,
        "slots": slots,
        "message": None,
        "opened_at": 0.0,
        "opened_at_wall": time.time(),
        "warned": False,
        "started": True,
        "cancelled": False,
    }


async def test_bankheist_self_target_rejected(db):
    """Hosting a heist on yourself short-circuits before any state mutates."""
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=800, display_name="host")
    _grant_level(host.id, 14)  # display level 15 — enough for !bankheist gate

    ctx = _make_ctx(host, host, content="!bankheist @self")
    await cog.cmd_bankheist.callback(cog, ctx, target=host)

    assert ctx.sent_messages == ["You can't rob yourself."]
    assert ctx.channel.id not in cog._active_heists


async def test_bankheist_bot_target_rejected(db):
    """Targeting the bot user is rejected up front."""
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=801, display_name="host")
    bot_member = FakeMember(uid=999_999_999, display_name="bot")
    bot_member.bot = True

    ctx = _make_ctx(host, bot_member, content="!bankheist @bot")
    await cog.cmd_bankheist.callback(cog, ctx, target=bot_member)

    assert ctx.sent_messages == ["You can't rob the house."]


async def test_bankheist_chance_formula_party_size_and_levels(db):
    """The chance table: base by party size + 0–10% for host, 0–2% per joiner."""
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=810)
    j1 = FakeMember(uid=811)
    j2 = FakeMember(uid=812)
    j3 = FakeMember(uid=813)

    # All level 1 (display) — bonuses are min(0.10, 1/1000)≈0.001 / min(0.02, 1/5000)≈0.0002.
    assert cog._bankheist_chance(host, [], 42) == pytest.approx(0.01 + 1 / 1000.0)
    assert cog._bankheist_chance(host, [j1], 42) == pytest.approx(
        0.10 + 1 / 1000.0 + 1 / 5000.0,
    )
    assert cog._bankheist_chance(host, [j1, j2], 42) == pytest.approx(
        0.15 + 1 / 1000.0 + 2 * (1 / 5000.0),
    )
    assert cog._bankheist_chance(host, [j1, j2, j3], 42) == pytest.approx(
        0.25 + 1 / 1000.0 + 3 * (1 / 5000.0),
    )

    # Maxed-out: host at internal level 99 → display 100 → 10% bonus capped.
    _grant_level(host.id, 99)
    for j in (j1, j2, j3):
        _grant_level(j.id, 99)  # display 100 → 2% capped each
    assert cog._bankheist_chance(host, [j1, j2, j3], 42) == pytest.approx(
        0.25 + 0.10 + 3 * 0.02,
    )


async def test_bankheist_resolve_success_splits_loot_evenly(db, monkeypatch):
    """4-player success path: 20% of victim savings drained, split among
    host + 3 joiners. Host gets the integer-division remainder."""
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=820, display_name="host")
    j1 = FakeMember(uid=821, display_name="j1")
    j2 = FakeMember(uid=822, display_name="j2")
    j3 = FakeMember(uid=823, display_name="j3")
    target = FakeMember(uid=824, display_name="target")

    await _economy._ensure_user(host.id)
    await _economy._ensure_user(j1.id)
    await _economy._ensure_user(j2.id)
    await _economy._ensure_user(j3.id)
    _seed_savings(target.id, 10_001)  # 20% = 2000 → split 4 → 500 each, +1 to host

    monkeypatch.setattr(random, "random", lambda: 0.0)  # always under chance

    hstate = _make_hstate(host, target, [j1, j2, j3])
    ctx = _make_ctx(host, target, content="!bankheist @target")
    result = await cog._bankheist_resolve(ctx, hstate)

    assert "Successful" in result.title
    # 10001 * 0.20 = 2000 (int truncation)
    assert await _economy.get_balance(host.id) == 500 + 0  # remainder is 0 here (2000/4)
    assert await _economy.get_balance(j1.id) == 500
    assert await _economy.get_balance(j2.id) == 500
    assert await _economy.get_balance(j3.id) == 500
    # Victim savings drained by 2000 → about 8001 left.
    remaining = await _economy.get_savings_value(target.id)
    assert 8000 <= remaining <= 8002
    # DB persistence
    assert await _read_db_balance(host.id) == 500
    assert await _read_db_balance(j1.id) == 500


async def test_bankheist_resolve_success_remainder_goes_to_host(db, monkeypatch):
    """When the seized amount doesn't divide evenly, host pockets the leftover."""
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=830, display_name="host")
    j1 = FakeMember(uid=831)
    j2 = FakeMember(uid=832)
    target = FakeMember(uid=833, display_name="target")

    # Savings of 5015 → 20% = 1003 → split among 3 → 334/334/334 with remainder 1.
    _seed_savings(target.id, 5015)
    monkeypatch.setattr(random, "random", lambda: 0.0)

    hstate = _make_hstate(host, target, [j1, j2])
    ctx = _make_ctx(host, target, content="!bankheist @target")
    await cog._bankheist_resolve(ctx, hstate)

    assert await _economy.get_balance(host.id) == 334 + 1
    assert await _economy.get_balance(j1.id) == 334
    assert await _economy.get_balance(j2.id) == 334


async def test_bankheist_resolve_failure_no_balance_moves(db, monkeypatch):
    """Failed roll: nobody's balance changes and victim savings are untouched."""
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=840, display_name="host")
    j1 = FakeMember(uid=841)
    target = FakeMember(uid=842, display_name="target")

    await _economy.add_balance(host.id, 100)
    await _economy.add_balance(j1.id, 200)
    _seed_savings(target.id, 10_000)

    monkeypatch.setattr(random, "random", lambda: 0.99)  # always above chance

    hstate = _make_hstate(host, target, [j1])
    ctx = _make_ctx(host, target, content="!bankheist @target")
    result = await cog._bankheist_resolve(ctx, hstate)

    assert "Failed" in result.title
    assert await _economy.get_balance(host.id) == 100
    assert await _economy.get_balance(j1.id) == 200
    remaining = await _economy.get_savings_value(target.id)
    assert 9999 <= remaining <= 10_001  # untouched (modulo float interest)


async def test_bankheist_resolve_empty_savings_fizzles(db, monkeypatch):
    """Lucky roll, but the victim has no savings — heist resolves cleanly with
    no balance changes and a 'Empty Vault' embed."""
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=850, display_name="host")
    target = FakeMember(uid=851, display_name="target")

    monkeypatch.setattr(random, "random", lambda: 0.0)

    hstate = _make_hstate(host, target, [])
    ctx = _make_ctx(host, target, content="!bankheist @target")
    result = await cog._bankheist_resolve(ctx, hstate)

    assert "Empty Vault" in result.title
    assert await _economy.get_balance(host.id) == 0


async def test_bankheist_active_heist_blocks_second_in_same_channel(db):
    """Opening a second bankheist while one is already running in the channel
    is rejected. Preempt the first by directly seeding _active_heists."""
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=860, display_name="host")
    target = FakeMember(uid=861, display_name="target")
    _grant_level(host.id, 14)
    _grant_level(target.id, 14)

    ctx = _make_ctx(host, target, content="!bankheist @target")
    cog._active_heists[ctx.channel.id] = {"placeholder": True}

    await cog.cmd_bankheist.callback(cog, ctx, target=target)

    # Still the original placeholder — the second invocation didn't replace it.
    assert cog._active_heists[ctx.channel.id] == {"placeholder": True}
    # Rejection embed sent
    assert any(
        getattr(e, "title", "") == "⏳ Heist Already Running"
        for e in ctx.sent_embeds
    )
