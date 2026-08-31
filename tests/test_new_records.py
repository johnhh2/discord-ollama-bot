"""Records added alongside the gambling categories: total artifacts owned,
longest daily command streak, and best scratchoff day.

Tie-break rules differ per category and are the interesting part:
  - total_artifacts / scratchoff_day — first to reach the value keeps it
    (the plain strict-`>` behavior every other category already has).
  - command_streak — an equal value goes to the LOWER user id, which can
    displace a sitting holder.
"""
import pytest

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy
import src.events as _events
from src.artifacts import owned_artifact_count
from src.cogs.shop_cog import ShopCog
from src.config import ARTIFACT_SLOTS_BLANK_COST, SCRATCH_SYMBOLS
from src.economy import add_balance
from src.gambling.scratchoff import play_scratchoffs
from src.persistence.records import UID_TIEBREAK_CATEGORIES

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeChannel


pytestmark = pytest.mark.asyncio

GID = 42
# Every card matches the goal on all four slots → the 100,000 🪙 tier.
FOUR_MATCH_PAYOUT = 100_000


class _StubBot:
    def __init__(self):
        self.user = type("U", (), {"id": 999_999_999})()
        self.cogs = {}


def _record_titles(channel) -> list[str]:
    """Titles of every embed announce_record pushed through channel.send."""
    return [
        kw["embed"].title
        for _, kw in channel.send.await_args_list
        if kw.get("embed") is not None
    ]


# ── tie-break semantics ───────────────────────────────────────────────────────

async def test_command_streak_tie_goes_to_lower_uid(db):
    assert "command_streak" in UID_TIEBREAK_CATEGORIES

    # Higher uid gets there first...
    assert await _persistence.try_set_record(GID, "command_streak", 12, 900, "late") is True
    # ...and a lower uid tying the value takes it.
    assert await _persistence.try_set_record(GID, "command_streak", 12, 100, "early") is True

    rec = (await _persistence.load_records(GID))["command_streak"]
    assert rec["holder_id"] == 100
    assert rec["holder_name"] == "early"


async def test_command_streak_tie_from_higher_uid_is_rejected(db):
    assert await _persistence.try_set_record(GID, "command_streak", 12, 100, "early") is True
    # Same value, higher uid: incumbent keeps it.
    assert await _persistence.try_set_record(GID, "command_streak", 12, 900, "late") is False
    # A strictly better streak still wins regardless of uid.
    assert await _persistence.try_set_record(GID, "command_streak", 13, 900, "late") is True

    rec = (await _persistence.load_records(GID))["command_streak"]
    assert rec["holder_id"] == 900
    assert rec["value"] == 13


async def test_same_holder_retying_own_streak_is_not_a_new_record(db):
    assert await _persistence.try_set_record(GID, "command_streak", 5, 100, "solo") is True
    assert await _persistence.try_set_record(GID, "command_streak", 5, 100, "solo") is False


@pytest.mark.parametrize("category", ["total_artifacts", "scratchoff_day"])
async def test_first_to_reach_keeps_non_tiebreak_records(db, category):
    """Artifacts and scratchoff days keep the plain first-come-wins rule: a
    tie never displaces the incumbent, even from a lower user id."""
    assert category not in UID_TIEBREAK_CATEGORIES
    assert await _persistence.try_set_record(GID, category, 4, 900, "first") is True
    assert await _persistence.try_set_record(GID, category, 4, 100, "second") is False

    rec = (await _persistence.load_records(GID))[category]
    assert rec["holder_name"] == "first"


async def test_global_command_streak_tie_across_guilds_uses_lower_uid(db):
    """load_global_records must apply the same tie-break, otherwise the
    global view could name a different holder than the per-guild one."""
    await _persistence.save_records(1, {
        "command_streak": {"value": 30, "holder_id": 900, "holder_name": "late"},
    })
    await _persistence.save_records(2, {
        "command_streak": {"value": 30, "holder_id": 100, "holder_name": "early"},
    })
    await _persistence.save_records(3, {
        "command_streak": {"value": 29, "holder_id": 1, "holder_name": "shorter"},
    })

    g = await _persistence.load_global_records()
    assert g["command_streak"]["value"] == 30
    assert g["command_streak"]["holder_name"] == "early"


# ── total artifacts ───────────────────────────────────────────────────────────

async def test_owned_artifact_count_sums_quantities():
    _state.user_artifacts[5001] = {"slots_blank_remover": 1, "bail_discount": 2}
    assert owned_artifact_count(5001) == 3
    # Unknown user owns nothing rather than raising.
    assert owned_artifact_count(5002) == 0


async def test_buying_an_artifact_sets_the_total_artifacts_record(db):
    cog = ShopCog(bot=None)
    uid = 5100
    _state.leveling.setdefault(str(GID), {})[str(uid)] = {"level": 4}  # display level 5
    await add_balance(uid, ARTIFACT_SLOTS_BLANK_COST)

    ctx = FakeCtx(author=FakeMember(uid=uid, display_name="collector"),
                  guild=FakeGuild(gid=GID))
    await cog.shop_artifacts.callback(cog, ctx, "buy", "1")

    rec = (await _persistence.load_records(GID))["total_artifacts"]
    assert rec["value"] == 1
    assert rec["holder_id"] == uid
    assert "🏆 New Record!" in _record_titles(ctx.channel)


async def test_artifact_purchase_outside_a_guild_sets_no_record(db):
    cog = ShopCog(bot=None)
    uid = 5101
    await add_balance(uid, ARTIFACT_SLOTS_BLANK_COST)

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=GID))
    ctx.guild = None
    await cog.shop_artifacts.callback(cog, ctx, "buy", "1")

    assert "total_artifacts" not in await _persistence.load_records(GID)


# ── command streak ────────────────────────────────────────────────────────────

async def _complete_command(uid: int, channel, display_name="runner", gid=GID):
    cog = _events.EventsCog(bot=_StubBot())
    ctx = FakeCtx(
        author=FakeMember(uid=uid, display_name=display_name),
        guild=FakeGuild(gid=gid),
        channel=channel,
    )
    # on_command_completion buckets stats by ctx.command.cog; FakeCtx's stub
    # command object only carries name/qualified_name.
    ctx.command.cog = None
    await cog.on_command_completion(ctx)
    return ctx


async def test_first_command_of_the_day_sets_the_command_streak_record(db, monkeypatch):
    today = "2026-05-02"
    monkeypatch.setattr(_events, "_ct_today", lambda: today)
    _state.command_streak["6100"] = {"date": "2026-05-01", "count": 7}

    channel = FakeChannel(ch_id=100)
    await _complete_command(6100, channel, display_name="streaker")

    rec = (await _persistence.load_records(GID))["command_streak"]
    assert rec["value"] == 8
    assert rec["holder_name"] == "streaker"
    assert "🏆 New Record!" in _record_titles(channel)


async def test_later_commands_the_same_day_dont_re_announce(db, monkeypatch):
    today = "2026-05-02"
    monkeypatch.setattr(_events, "_ct_today", lambda: today)
    _state.command_streak["6101"] = {"date": "2026-05-01", "count": 3}

    channel = FakeChannel(ch_id=100)
    await _complete_command(6101, channel)
    first_count = len(_record_titles(channel))
    assert first_count == 1

    # Same day, second command: the streak is already banked, so no second
    # record attempt and no duplicate embed.
    await _complete_command(6101, channel)
    assert len(_record_titles(channel)) == first_count
    assert (await _persistence.load_records(GID))["command_streak"]["value"] == 4


async def test_tied_streak_from_lower_uid_takes_the_record(db, monkeypatch):
    today = "2026-05-02"
    monkeypatch.setattr(_events, "_ct_today", lambda: today)
    _state.command_streak["6300"] = {"date": "2026-05-01", "count": 9}
    _state.command_streak["6200"] = {"date": "2026-05-01", "count": 9}

    channel = FakeChannel(ch_id=100)
    await _complete_command(6300, channel, display_name="higher-uid")
    assert (await _persistence.load_records(GID))["command_streak"]["holder_id"] == 6300

    await _complete_command(6200, channel, display_name="lower-uid")
    rec = (await _persistence.load_records(GID))["command_streak"]
    assert rec["value"] == 10
    assert rec["holder_id"] == 6200


# ── best scratchoff day ───────────────────────────────────────────────────────

def _always_match(monkeypatch):
    """Force goal and card to the same symbols → a 4-match card every time."""
    monkeypatch.setattr(
        "src.gambling.scratchoff.random.choices",
        lambda population, k: [SCRATCH_SYMBOLS[0]] * k,
    )


def _never_match(monkeypatch):
    """First draw is the daily goal; every card after it misses on all four."""
    calls = {"n": 0}

    def _choices(population, k):
        calls["n"] += 1
        symbol = SCRATCH_SYMBOLS[0] if calls["n"] == 1 else SCRATCH_SYMBOLS[1]
        return [symbol] * k

    monkeypatch.setattr("src.gambling.scratchoff.random.choices", _choices)


async def _play(uid: int, channel, count: int, display_name="scratcher"):
    return await play_scratchoffs(
        _StubBot(),
        FakeMember(uid=uid, display_name=display_name),
        channel,
        FakeGuild(gid=GID),
        count=count,
    )


async def test_scratchoff_day_record_combines_the_whole_day(db, monkeypatch):
    """Three separate !scratchoff calls must record the same combined total
    the batch path (!scratches / the dailies button) would produce."""
    monkeypatch.setattr("src.gambling.scratchoff._ct_today", lambda: "2026-05-02")
    _always_match(monkeypatch)

    uid = 7100
    channel = FakeChannel(ch_id=100)
    for _ in range(3):
        await _play(uid, channel, 1)

    expected = 3 * FOUR_MATCH_PAYOUT
    assert _state.economy["users"][str(uid)]["scratch_won_today"] == expected
    rec = (await _persistence.load_records(GID))["scratchoff_day"]
    assert rec["value"] == expected
    assert rec["holder_id"] == uid


async def test_scratchoff_day_batch_matches_the_one_at_a_time_total(db, monkeypatch):
    """The !scratches / dailies-button path burns the whole allowance in one
    call; it must land on the same number as three single plays."""
    monkeypatch.setattr("src.gambling.scratchoff._ct_today", lambda: "2026-05-02")
    _always_match(monkeypatch)

    channel = FakeChannel(ch_id=100)
    await _play(7200, channel, 3)

    rec = (await _persistence.load_records(GID))["scratchoff_day"]
    assert rec["value"] == 3 * FOUR_MATCH_PAYOUT
    # One announcement for the batch, not one per card.
    assert _record_titles(channel).count("🏆 New Record!") == 1


async def test_scratchoff_day_resets_on_the_next_gameplay_day(db, monkeypatch):
    _always_match(monkeypatch)
    uid = 7101
    channel = FakeChannel(ch_id=100)

    monkeypatch.setattr("src.gambling.scratchoff._ct_today", lambda: "2026-05-02")
    await _play(uid, channel, 3)
    assert _state.economy["users"][str(uid)]["scratch_won_today"] == 3 * FOUR_MATCH_PAYOUT

    monkeypatch.setattr("src.gambling.scratchoff._ct_today", lambda: "2026-05-03")
    await _play(uid, channel, 1)
    # Day two starts from zero rather than inheriting day one's total.
    assert _state.economy["users"][str(uid)]["scratch_won_today"] == FOUR_MATCH_PAYOUT
    # ...and it didn't beat day one, so the record still reads day one's number.
    rec = (await _persistence.load_records(GID))["scratchoff_day"]
    assert rec["value"] == 3 * FOUR_MATCH_PAYOUT


async def test_losing_scratchoff_day_sets_no_record(db, monkeypatch):
    monkeypatch.setattr("src.gambling.scratchoff._ct_today", lambda: "2026-05-02")
    _never_match(monkeypatch)

    await _play(7102, FakeChannel(ch_id=100), 3)

    assert _state.economy["users"]["7102"]["scratch_won_today"] == 0
    assert "scratchoff_day" not in await _persistence.load_records(GID)


async def test_scratch_won_today_persists_to_the_db(db, monkeypatch):
    """The running total round-trips through economy_users, so a restart
    mid-day can't hand someone a second shot at the same record."""
    monkeypatch.setattr("src.gambling.scratchoff._ct_today", lambda: "2026-05-02")
    _always_match(monkeypatch)
    uid = 7103
    await _play(uid, FakeChannel(ch_id=100), 2)

    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT scratch_won_today FROM economy_users WHERE user_id=?", (uid,)
            )
            row = await cur.fetchone()
    assert row[0] == 2 * FOUR_MATCH_PAYOUT


async def test_daily_reset_zeroes_scratch_winnings(db):
    uid = 7104
    await _economy._ensure_user(uid)
    _state.economy["users"][str(uid)]["scratch_won_today"] = 12_345
    await _economy.do_daily_reset()
    assert _state.economy["users"][str(uid)]["scratch_won_today"] == 0


# ── !records rendering ────────────────────────────────────────────────────────

async def test_records_embed_renders_units_for_the_new_categories(db):
    from src.cogs.economy_cog import EconomyCog

    await _persistence.save_records(GID, {
        "total_artifacts": {"value": 5, "holder_id": 1, "holder_name": "collector"},
        "command_streak": {"value": 1, "holder_id": 2, "holder_name": "newbie"},
        "scratchoff_day": {"value": 300_000, "holder_id": 3, "holder_name": "lucky"},
    })

    cog = EconomyCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=1), guild=FakeGuild(gid=GID))
    await cog.cmd_records.callback(cog, ctx)

    desc = ctx.sent_embeds[-1].description
    # Artifacts are a bare count, streaks are days (singular at 1), and the
    # scratchoff day is coins like every other economy record.
    assert "**Artifacts Owned:** 5 — **collector**" in desc
    assert "**Command Streak:** 1 day — **newbie**" in desc
    assert "**Scratchoff Day Payout:** 300,000 🪙 — **lucky**" in desc


async def test_records_embed_pluralizes_multi_day_streaks(db):
    from src.cogs.economy_cog import EconomyCog

    await _persistence.save_records(GID, {
        "command_streak": {"value": 42, "holder_id": 2, "holder_name": "veteran"},
    })
    cog = EconomyCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=1), guild=FakeGuild(gid=GID))
    await cog.cmd_records.callback(cog, ctx)

    assert "**Command Streak:** 42 days — **veteran**" in ctx.sent_embeds[-1].description


async def test_records_embed_renders_chess_categories(db):
    from src.cogs.economy_cog import EconomyCog

    await _persistence.save_records(GID, {
        "highest_bot_chess_elo_defeated": {"value": 1900, "holder_id": 4, "holder_name": "kasparov"},
        "chess_pvp_wins": {"value": 1, "holder_id": 5, "holder_name": "duelist"},
    })
    cog = EconomyCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=1), guild=FakeGuild(gid=GID))
    await cog.cmd_records.callback(cog, ctx)

    desc = ctx.sent_embeds[-1].description
    assert "**Highest Elo Defeated:** 1,900 Elo — **kasparov**" in desc
    # Singular at exactly one win, like the streak line.
    assert "**PvP Chess Wins:** 1 win — **duelist**" in desc
