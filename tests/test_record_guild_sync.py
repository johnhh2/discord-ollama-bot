"""Records backed by a global per-user stat must read the same in every server.

Your artifact count, balance, command streak and best Stockfish Elo are one
number that doesn't care which server you're looking from — so buying an
artifact in server A has to move server B's "most artifacts owned" record too.
A slots jackpot is the opposite: an event that happened in A and belongs to A.

Two halves here: `try_set_record` mirroring the write at runtime, and the two
backfill migrations that fix rows written before the mirror existed.
"""
from pathlib import Path

import pytest

import src.state as _state
from src import migrations as _migrations
from src.db import with_cursor
from src.persistence.records import (
    GLOBAL_STAT_CATEGORIES, try_set_record, load_records,
)


pytestmark = pytest.mark.asyncio

GUILD_A = 111
GUILD_B = 222
GUILD_C = 333


# ── seeding helpers ───────────────────────────────────────────────────────────

def _active_in(uid: int, *guild_ids: int) -> None:
    """Give `uid` an in-memory leveling row in each guild — what the runtime
    mirror reads to decide which servers a user is present in."""
    for gid in guild_ids:
        _state.leveling.setdefault(str(gid), {})[str(uid)] = {"xp": 0, "level": 0}


async def _db_active_in(uid: int, *guild_ids: int) -> None:
    """Same, but as `leveling` rows in the DB — what the migrations read."""
    async with with_cursor() as cur:
        for gid in guild_ids:
            await cur.execute(
                "INSERT INTO leveling (guild_id, user_id, data) VALUES (%s,%s,%s)",
                (gid, uid, "{}"),
            )


async def _seed_record(gid: int, category: str, value: int, uid: int, name: str) -> None:
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO records (guild_id, category, value, holder_id, holder_name)"
            " VALUES (%s,%s,%s,%s,%s)",
            (gid, category, value, uid, name),
        )


async def _seed_artifacts(uid: int, count: int) -> None:
    async with with_cursor() as cur:
        for i in range(count):
            await cur.execute(
                "INSERT INTO user_artifacts (user_id, artifact_id, quantity)"
                " VALUES (%s,%s,1)",
                (uid, f"art{i}"),
            )


async def _seed_live_streak(uid: int, streak: int) -> None:
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO command_streak (user_id, last_date, streak_count)"
            " VALUES (%s,'2026-08-25',%s)",
            (uid, streak),
        )


async def _apply(filename: str) -> None:
    """Re-run one migration file's statements against the test DB.

    The `db` fixture already applied every migration to an empty DB, so this
    both exercises the backfill against seeded data AND proves the file is
    safe to run twice.
    """
    sql = (Path(_migrations._MIGRATIONS_DIR) / filename).read_text(encoding="utf-8")
    async with with_cursor() as cur:
        for stmt in _migrations._split_statements(sql):
            await cur.execute(stmt)


ARTIFACT_MIGRATION = "0046_sync_artifact_record_across_guilds.sql"
STREAK_MIGRATION = "0047_sync_command_streak_record_across_guilds.sql"


async def _row(gid: int, category: str) -> dict | None:
    return (await load_records(gid)).get(category)


# ── which categories are global ───────────────────────────────────────────────

async def test_only_global_per_user_stats_are_mirrored():
    """A guard on the classification itself. Gambling wins, the crime score,
    per-guild chess wins and per-guild hangman tallies are events or counts
    scoped to one server; the four in the set are properties of the user."""
    assert GLOBAL_STAT_CATEGORIES == {
        "highest_balance",
        "total_artifacts",
        "command_streak",
        "highest_bot_chess_elo_defeated",
    }
    for per_guild in ("flip", "slots_jackpot", "slots_non_jackpot", "lottery",
                      "blackjack", "hangman_payout", "scratchoff_day", "crime",
                      "chess_pvp_wins", "hangman_wins_42"):
        assert per_guild not in GLOBAL_STAT_CATEGORIES


# ── runtime mirror ────────────────────────────────────────────────────────────

async def test_artifact_record_lands_in_both_of_the_users_servers(db):
    _active_in(700, GUILD_A, GUILD_B)

    assert await try_set_record(GUILD_A, "total_artifacts", 12, 700, "collector")

    for gid in (GUILD_A, GUILD_B):
        rec = await _row(gid, "total_artifacts")
        assert rec == {"value": 12, "holder_id": 700, "holder_name": "collector"}


async def test_mirror_skips_servers_the_user_is_not_active_in(db):
    _active_in(701, GUILD_A, GUILD_B)

    await try_set_record(GUILD_A, "total_artifacts", 12, 701, "collector")

    assert await _row(GUILD_C, "total_artifacts") is None


async def test_mirror_does_not_displace_a_bigger_holder_elsewhere(db):
    """Server B's record belongs to whoever in B has the most — a smaller
    count arriving from server A doesn't get to take it."""
    _active_in(702, GUILD_A, GUILD_B)
    await _seed_record(GUILD_B, "total_artifacts", 40, 999, "whale")

    assert await try_set_record(GUILD_A, "total_artifacts", 12, 702, "collector")

    assert (await _row(GUILD_A, "total_artifacts"))["holder_id"] == 702
    assert (await _row(GUILD_B, "total_artifacts"))["holder_id"] == 999


async def test_mirror_still_runs_when_the_calling_guild_did_not_change(db):
    """The return value reports only the calling guild — so the caller
    announces just there — but a server that's still behind gets caught up."""
    _active_in(703, GUILD_A, GUILD_B)
    await _seed_record(GUILD_A, "total_artifacts", 40, 999, "whale")
    await _seed_record(GUILD_B, "total_artifacts", 5, 703, "collector")

    took = await try_set_record(GUILD_A, "total_artifacts", 12, 703, "collector")

    assert took is False  # no announcement in A — the whale still holds it
    assert (await _row(GUILD_B, "total_artifacts"))["value"] == 12


async def test_command_streak_and_balance_and_chess_elo_all_mirror(db):
    _active_in(704, GUILD_A, GUILD_B)

    await try_set_record(GUILD_A, "command_streak", 30, 704, "streaker")
    await try_set_record(GUILD_A, "highest_balance", 999_999, 704, "streaker")
    await try_set_record(GUILD_A, "highest_bot_chess_elo_defeated", 2400, 704, "streaker")

    b = await load_records(GUILD_B)
    assert b["command_streak"]["value"] == 30
    assert b["highest_balance"]["value"] == 999_999
    assert b["highest_bot_chess_elo_defeated"]["value"] == 2400


async def test_gambling_and_crime_records_stay_in_the_server_they_happened_in(db):
    _active_in(705, GUILD_A, GUILD_B)

    await try_set_record(GUILD_A, "slots_jackpot", 50_000, 705, "lucky")
    await try_set_record(GUILD_A, "crime", 80_000, 705, "lucky", crime_type="steal")
    await try_set_record(GUILD_A, "blackjack", 20_000, 705, "lucky")

    assert await load_records(GUILD_B) == {}


async def test_mirrored_meta_rides_along(db):
    """extra_json fields travel with the mirrored row, so the other server
    renders the same detail line rather than a bare number."""
    _active_in(706, GUILD_A, GUILD_B)

    await try_set_record(
        GUILD_A, "highest_bot_chess_elo_defeated", 2400, 706, "gm", opening="Sicilian",
    )

    assert (await _row(GUILD_B, "highest_bot_chess_elo_defeated"))["opening"] == "Sicilian"


async def test_mirror_leaves_other_categories_in_the_target_guild_untouched(db):
    """Writing one row must not clobber the mirrored guild's other records."""
    _active_in(707, GUILD_A, GUILD_B)
    await _seed_record(GUILD_B, "slots_jackpot", 77_000, 888, "someone")

    await try_set_record(GUILD_A, "total_artifacts", 12, 707, "collector")

    b = await load_records(GUILD_B)
    assert b["slots_jackpot"] == {"value": 77_000, "holder_id": 888, "holder_name": "someone"}
    assert b["total_artifacts"]["value"] == 12


async def test_streak_tiebreak_applies_to_mirrored_writes_too(db):
    """command_streak breaks ties on the lower user id; the mirror uses the
    same comparison, so a tie displaces a higher-uid holder in every server."""
    _active_in(100, GUILD_A, GUILD_B)
    await _seed_record(GUILD_B, "command_streak", 30, 900, "higher-uid")

    await try_set_record(GUILD_A, "command_streak", 30, 100, "lower-uid")

    assert (await _row(GUILD_B, "command_streak"))["holder_id"] == 100


async def test_a_purchase_updates_the_record_in_both_servers(db, monkeypatch):
    """End-to-end through !shop artifact rather than the record layer alone."""
    from src.cogs.shop_cog import ShopCog
    import src.economy as _economy
    from src.artifacts import ARTIFACTS
    from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild

    buyer = FakeMember(uid=708, display_name="collector")
    _active_in(buyer.id, GUILD_A, GUILD_B)
    # ARTIFACTS[0] unlocks at display level 5; internal N → display N+1.
    _state.leveling[str(GUILD_A)][str(buyer.id)]["level"] = ARTIFACTS[0]["level"] - 1
    await _economy.add_balance(buyer.id, 10_000_000)

    cog = ShopCog(bot=None)
    ctx = FakeCtx(author=buyer, guild=FakeGuild(gid=GUILD_A))
    await cog.shop_artifacts.callback(cog, ctx, "buy", "1")

    for gid in (GUILD_A, GUILD_B):
        rec = await _row(gid, "total_artifacts")
        assert rec is not None, f"guild {gid} never got the artifact record"
        assert rec["value"] == 1
        assert rec["holder_id"] == buyer.id


# ── 0046: artifact backfill ───────────────────────────────────────────────────

async def test_artifact_backfill_raises_a_stale_row_the_user_already_holds(db):
    """The exact reported case: bought in server A, server B stuck low."""
    await _seed_artifacts(800, 12)
    await _db_active_in(800, GUILD_A, GUILD_B)
    await _seed_record(GUILD_A, "total_artifacts", 12, 800, "collector")
    await _seed_record(GUILD_B, "total_artifacts", 3, 800, "collector")

    await _apply(ARTIFACT_MIGRATION)

    assert (await _row(GUILD_B, "total_artifacts"))["value"] == 12


async def test_artifact_backfill_takes_a_record_the_user_did_not_hold(db):
    """Server B's row belonged to someone smaller — the true count wins it."""
    await _seed_artifacts(801, 12)
    await _db_active_in(801, GUILD_A, GUILD_B)
    await _seed_record(GUILD_A, "total_artifacts", 12, 801, "collector")
    await _seed_record(GUILD_B, "total_artifacts", 8, 999, "rival")

    await _apply(ARTIFACT_MIGRATION)

    rec = await _row(GUILD_B, "total_artifacts")
    assert rec["value"] == 12
    assert rec["holder_id"] == 801
    assert rec["holder_name"] == "collector"


async def test_artifact_backfill_leaves_a_bigger_incumbent_alone(db):
    await _seed_artifacts(802, 12)
    await _db_active_in(802, GUILD_A, GUILD_B)
    await _seed_record(GUILD_A, "total_artifacts", 12, 802, "collector")
    await _seed_record(GUILD_B, "total_artifacts", 40, 999, "whale")

    await _apply(ARTIFACT_MIGRATION)

    rec = await _row(GUILD_B, "total_artifacts")
    assert rec["value"] == 40
    assert rec["holder_id"] == 999


async def test_artifact_backfill_does_not_reach_servers_the_user_never_used(db):
    await _seed_artifacts(803, 12)
    await _db_active_in(803, GUILD_A, GUILD_B)
    await _seed_record(GUILD_A, "total_artifacts", 12, 803, "collector")

    await _apply(ARTIFACT_MIGRATION)

    assert await _row(GUILD_C, "total_artifacts") is None


async def test_artifact_backfill_picks_the_biggest_of_two_competing_users(db):
    """Two users active in the same server both beat its row; the upsert has
    to land on the larger one regardless of which row SQL visits first."""
    await _seed_artifacts(804, 12)
    await _seed_artifacts(805, 30)
    await _db_active_in(804, GUILD_A, GUILD_B)
    await _db_active_in(805, GUILD_A, GUILD_B)
    await _seed_record(GUILD_A, "total_artifacts", 30, 805, "bigger")
    await _seed_record(GUILD_B, "total_artifacts", 1, 999, "tiny")

    await _apply(ARTIFACT_MIGRATION)

    rec = await _row(GUILD_B, "total_artifacts")
    assert rec["value"] == 30
    assert rec["holder_id"] == 805


async def test_artifact_backfill_skips_users_with_no_name_on_record(db):
    """Nothing anywhere knows this user's display name, so there's no row to
    write; the runtime mirror picks them up on their next purchase."""
    await _seed_artifacts(806, 12)
    await _db_active_in(806, GUILD_A)

    await _apply(ARTIFACT_MIGRATION)

    assert await _row(GUILD_A, "total_artifacts") is None


async def test_artifact_backfill_is_idempotent(db):
    await _seed_artifacts(807, 12)
    await _db_active_in(807, GUILD_A, GUILD_B)
    await _seed_record(GUILD_A, "total_artifacts", 12, 807, "collector")
    await _seed_record(GUILD_B, "total_artifacts", 3, 807, "collector")

    await _apply(ARTIFACT_MIGRATION)
    first = await load_records(GUILD_B)
    await _apply(ARTIFACT_MIGRATION)

    assert await load_records(GUILD_B) == first


async def test_artifact_backfill_never_touches_other_categories(db):
    await _seed_artifacts(808, 12)
    await _db_active_in(808, GUILD_A, GUILD_B)
    await _seed_record(GUILD_A, "total_artifacts", 12, 808, "collector")
    await _seed_record(GUILD_B, "slots_jackpot", 50_000, 999, "lucky")
    await _seed_record(GUILD_B, "crime", 9_000, 999, "lucky")

    await _apply(ARTIFACT_MIGRATION)

    b = await load_records(GUILD_B)
    assert b["slots_jackpot"] == {"value": 50_000, "holder_id": 999, "holder_name": "lucky"}
    assert b["crime"] == {"value": 9_000, "holder_id": 999, "holder_name": "lucky"}


# ── 0047: command-streak backfill ─────────────────────────────────────────────

async def test_streak_backfill_propagates_the_high_water_mark(db):
    await _db_active_in(810, GUILD_A, GUILD_B)
    await _seed_record(GUILD_A, "command_streak", 30, 810, "streaker")
    await _seed_record(GUILD_B, "command_streak", 4, 810, "streaker")
    await _seed_live_streak(810, 30)

    await _apply(STREAK_MIGRATION)

    assert (await _row(GUILD_B, "command_streak"))["value"] == 30


async def test_streak_backfill_does_not_lower_a_record_to_a_reset_streak(db):
    """Missing a day resets command_streak.streak_count to 1. The record is a
    high-water mark, so it has to survive that."""
    await _db_active_in(811, GUILD_A, GUILD_B)
    await _seed_record(GUILD_A, "command_streak", 30, 811, "streaker")
    await _seed_record(GUILD_B, "command_streak", 30, 811, "streaker")
    await _seed_live_streak(811, 1)

    await _apply(STREAK_MIGRATION)

    assert (await _row(GUILD_A, "command_streak"))["value"] == 30
    assert (await _row(GUILD_B, "command_streak"))["value"] == 30


async def test_streak_backfill_uses_a_live_streak_that_beats_every_record(db):
    """A streak bumped where no record could be written (a DM) still counts."""
    await _db_active_in(812, GUILD_A, GUILD_B)
    await _seed_record(GUILD_A, "command_streak", 10, 812, "streaker")
    await _seed_live_streak(812, 40)

    await _apply(STREAK_MIGRATION)

    assert (await _row(GUILD_A, "command_streak"))["value"] == 40
    assert (await _row(GUILD_B, "command_streak"))["value"] == 40


async def test_streak_backfill_breaks_ties_on_the_lower_user_id(db):
    """Matches _beats: command_streak is in UID_TIEBREAK_CATEGORIES, so an
    equal value held by a higher uid is displaced."""
    await _db_active_in(100, GUILD_A, GUILD_B)
    await _seed_record(GUILD_A, "command_streak", 30, 100, "lower-uid")
    await _seed_record(GUILD_B, "command_streak", 30, 900, "higher-uid")

    await _apply(STREAK_MIGRATION)

    assert (await _row(GUILD_B, "command_streak"))["holder_id"] == 100


async def test_streak_backfill_leaves_a_bigger_incumbent_alone(db):
    await _db_active_in(813, GUILD_A, GUILD_B)
    await _seed_record(GUILD_A, "command_streak", 30, 813, "streaker")
    await _seed_record(GUILD_B, "command_streak", 99, 999, "marathoner")

    await _apply(STREAK_MIGRATION)

    rec = await _row(GUILD_B, "command_streak")
    assert rec["value"] == 99
    assert rec["holder_id"] == 999


async def test_streak_backfill_is_idempotent(db):
    await _db_active_in(814, GUILD_A, GUILD_B)
    await _seed_record(GUILD_A, "command_streak", 30, 814, "streaker")
    await _seed_record(GUILD_B, "command_streak", 4, 814, "streaker")
    await _seed_live_streak(814, 30)

    await _apply(STREAK_MIGRATION)
    first = await load_records(GUILD_B)
    await _apply(STREAK_MIGRATION)

    assert await load_records(GUILD_B) == first


async def test_backfills_are_no_ops_on_a_database_with_no_records(db):
    await _apply(ARTIFACT_MIGRATION)
    await _apply(STREAK_MIGRATION)

    async with with_cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM records")
        assert (await cur.fetchone())[0] == 0
