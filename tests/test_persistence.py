"""Persistence round-trip tests: write state -> save -> clear -> load -> assert.

These tests use the opt-in `db` fixture (see tests/conftest.py), which swaps
in an in-memory SQLite for `src.db.get_pool` and restores the real save_*/
load_* functions. They exercise the actual SQL strings in src/persistence.py;
a typo in a column name will fail a test here.
"""

import pytest

import src.state as _state
import src.persistence as _persistence


pytestmark = pytest.mark.asyncio


# ── economy ───────────────────────────────────────────────────────────────────

async def test_economy_users_roundtrip(db):
    _state.economy["users"]["1001"] = {
        "balance": 5000,
        "last_daily": 1234.5,
        "daily_date": "2026-05-01",
        "scratch_used": 2,
        "scratch_date": "2026-05-02",
        "jailbreak_used": True,
        "jail_until": 0.0,
        "savings": [{"amount": 100, "deposited_at": 999.0}],
        "jail_reason": "Tried to steal from Alice",
    }
    _state.economy["users"]["1002"] = {
        "balance": 0,
        "last_daily": 0.0,
        "daily_date": None,
        "scratch_used": 0,
        "scratch_date": None,
        "jailbreak_used": False,
        "jail_until": 0.0,
        "savings": [],
        "jail_reason": None,
    }
    _state.economy["last_daily_reset"] = "2026-05-02"

    await _persistence.save_economy()

    # Wipe in-memory state and reload from DB
    _state.economy["users"].clear()
    _state.economy["last_daily_reset"] = None
    await _persistence.init_db_state()

    assert "1001" in _state.economy["users"]
    u = _state.economy["users"]["1001"]
    assert u["balance"] == 5000
    assert u["last_daily"] == 1234.5
    assert u["daily_date"] == "2026-05-01"
    assert u["scratch_used"] == 2
    assert u["scratch_date"] == "2026-05-02"
    assert u["jailbreak_used"] is True
    assert u["savings"] == [{"amount": 100, "deposited_at": 999.0}]
    assert u["jail_reason"] == "Tried to steal from Alice"
    assert _state.economy["users"]["1002"]["balance"] == 0
    assert _state.economy["users"]["1002"]["jail_reason"] is None
    assert _state.economy["last_daily_reset"] == "2026-05-02"


async def test_economy_targeted_save_writes_only_one_user(db):
    _state.economy["users"]["1"] = {"balance": 100, "last_daily": 0.0}
    _state.economy["users"]["2"] = {"balance": 200, "last_daily": 0.0}
    await _persistence.save_economy()  # bulk: both rows in DB

    # Mutate in memory but only save uid=1
    _state.economy["users"]["1"]["balance"] = 999
    _state.economy["users"]["2"]["balance"] = 999
    await _persistence.save_economy(uid=1)

    # Reload — uid 1 should reflect 999, uid 2 should still be 200
    _state.economy["users"].clear()
    await _persistence.init_db_state()
    assert _state.economy["users"]["1"]["balance"] == 999
    assert _state.economy["users"]["2"]["balance"] == 200


async def test_guild_house_roundtrip(db):
    from src.economy import add_guild_house
    await add_guild_house(42, 1500)
    await add_guild_house(42, 250)
    await add_guild_house(99, 7777)

    _state.economy["guild_house"].clear()
    await _persistence.init_db_state()
    assert _state.economy["guild_house"]["42"] == 1750
    assert _state.economy["guild_house"]["99"] == 7777


# ── insurance ─────────────────────────────────────────────────────────────────

async def test_insurance_delete_and_replace(db):
    import time
    future = time.time() + 3600

    _state.insurance["1"] = {
        "expires_at": future,
        "protected_from": ["nickname", "curse"],
    }
    _state.insurance["2"] = {
        "expires_at": future,
        "protected_from": ["tax"],
    }
    await _persistence.save_insurance()

    # Drop user 2, save again — DB should reflect the deletion (save_insurance
    # does DELETE FROM shop_insurance + re-insert).
    del _state.insurance["2"]
    await _persistence.save_insurance()

    _state.insurance.clear()
    await _persistence.init_db_state()
    assert "1" in _state.insurance
    assert "2" not in _state.insurance
    assert _state.insurance["1"]["protected_from"] == ["nickname", "curse"]


# ── lottery ───────────────────────────────────────────────────────────────────

async def test_lottery_roundtrip(db):
    payload = {
        "prize_pool": 5000,
        "last_posted_week": 18,
        "last_drawn_week": 17,
        "players": {"1001": 5, "1002": 3},
    }
    await _persistence.save_lottery(42, payload)

    loaded = await _persistence.load_lottery(42)
    assert loaded["prize_pool"] == 5000
    assert loaded["last_posted_week"] == 18
    assert loaded["last_drawn_week"] == 17
    assert loaded["players"] == {"1001": 5, "1002": 3}


async def test_lottery_no_players_round_trip_and_redraw_guard(db):
    """Edge case: a week ends with prize pool but zero ticket-buyers.

    Two things must hold for the scheduler in src/cogs/lottery_cog.py to behave:
    1. An empty `players` dict must round-trip cleanly (not become None or
       crash load_lottery — the scheduler keys off `if players and pool > 0`).
    2. After a no-player week, saving lottery with the new `last_drawn_week`
       must persist; otherwise the scheduler would re-trigger the draw branch
       every minute for the rest of the day.
    """
    # Week 17 ended with 5000 in the pool and no buyers.
    abandoned = {
        "prize_pool": 5000,
        "players": {},
        "last_posted_week": 16,
        "last_drawn_week": 16,  # NOT yet drawn for week 17
    }
    await _persistence.save_lottery(7, abandoned)

    loaded = await _persistence.load_lottery(7)
    assert loaded["players"] == {}
    assert loaded["prize_pool"] == 5000
    # Scheduler's guard: skips payout when this is False.
    assert not (loaded["players"] and loaded["prize_pool"] > 0)

    # Scheduler now resets the week. The seed (2000) replaces the pool because
    # no one won, last_drawn_week advances to 17, players cleared.
    reset = {"prize_pool": 2000, "players": {}, "last_drawn_week": 17, "last_posted_week": 0}
    await _persistence.save_lottery(7, reset)

    reloaded = await _persistence.load_lottery(7)
    assert reloaded["players"] == {}
    assert reloaded["prize_pool"] == 2000
    assert reloaded["last_drawn_week"] == 17  # redraw guard is now armed

    # Sanity: hand the loaded lottery back to drain_bot_balance_into_lottery
    # the way the scheduler does — empty house, pool unchanged.
    from src.economy import drain_bot_balance_into_lottery
    transferred = await drain_bot_balance_into_lottery(reloaded, 7)
    assert transferred == 0
    assert reloaded["prize_pool"] == 2000


async def test_lottery_save_replaces_players(db):
    await _persistence.save_lottery(1, {
        "prize_pool": 100, "last_posted_week": 0, "last_drawn_week": 0,
        "players": {"100": 1, "200": 2, "300": 3},
    })
    # Now save with only one player — old players for this guild should be gone
    await _persistence.save_lottery(1, {
        "prize_pool": 100, "last_posted_week": 0, "last_drawn_week": 0,
        "players": {"100": 5},
    })
    loaded = await _persistence.load_lottery(1)
    assert loaded["players"] == {"100": 5}


# ── records ───────────────────────────────────────────────────────────────────

async def test_records_roundtrip_and_extra_meta(db):
    records = {
        "highest_balance": {"value": 9999, "holder_id": 1, "holder_name": "alice"},
        "best_streak": {
            "value": 7, "holder_id": 2, "holder_name": "bob",
            "set_at": "2026-04-01",
        },
    }
    await _persistence.save_records(99, records)
    loaded = await _persistence.load_records(99)
    assert loaded["highest_balance"]["value"] == 9999
    assert loaded["highest_balance"]["holder_name"] == "alice"
    assert loaded["best_streak"]["set_at"] == "2026-04-01"


async def test_load_global_records_picks_top_across_guilds(db):
    await _persistence.save_records(1, {
        "highest_balance": {"value": 500, "holder_id": 1, "holder_name": "alice"},
        "lottery": {"value": 9000, "holder_id": 1, "holder_name": "alice"},
    })
    await _persistence.save_records(2, {
        "highest_balance": {"value": 8000, "holder_id": 2, "holder_name": "bob"},
    })
    await _persistence.save_records(3, {
        "highest_balance": {"value": 3000, "holder_id": 3, "holder_name": "carol"},
    })
    g = await _persistence.load_global_records()
    # highest_balance: guild 2's bob wins
    assert g["highest_balance"]["value"] == 8000
    assert g["highest_balance"]["holder_name"] == "bob"
    # lottery: only guild 1 has it
    assert g["lottery"]["value"] == 9000
    assert g["lottery"]["holder_name"] == "alice"


async def test_try_set_record_only_updates_when_higher(db):
    ok1 = await _persistence.try_set_record(7, "score", 100, 1, "alice")
    assert ok1 is True

    # Lower value: should NOT update
    ok2 = await _persistence.try_set_record(7, "score", 50, 2, "bob")
    assert ok2 is False
    loaded = await _persistence.load_records(7)
    assert loaded["score"]["value"] == 100
    assert loaded["score"]["holder_name"] == "alice"

    # Higher value: should update
    ok3 = await _persistence.try_set_record(7, "score", 200, 3, "carol")
    assert ok3 is True
    loaded = await _persistence.load_records(7)
    assert loaded["score"]["value"] == 200
    assert loaded["score"]["holder_name"] == "carol"


# ── balance history ───────────────────────────────────────────────────────────

async def test_balance_history_roundtrip(db):
    """History is keyed {date: {bucket: {uid: payload}}} — bucket 0..3
    splits each calendar day into 6h CT windows."""
    history = {
        "2026-04-30": {0: {"1": {"wallet": 100, "savings": 50}}},
        "2026-05-01": {1: {"1": {"wallet": 110, "savings": 55}, "2": {"wallet": 0, "savings": 0}}},
    }
    await _persistence.save_balance_history(history)
    loaded = await _persistence.load_balance_history()
    assert loaded == history


# ── command perms ─────────────────────────────────────────────────────────────

async def test_command_perms_roundtrip(db):
    _state.command_perms.clear()
    _state.command_perms["godmode"] = {"tier": "bot_admin", "hidden": True}
    _state.command_perms["balance"] = {"tier": "everyone", "hidden": False}
    await _persistence.save_command_perms()

    # Read back via SQL directly — init_db_state's command_perms branch also
    # tries to load JSON file from disk, which would muddy the assertion.
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT command_name, tier, hidden FROM command_perms")
            rows = await cur.fetchall()
    by_name = {r[0]: (r[1], bool(r[2])) for r in rows}
    assert by_name["godmode"] == ("bot_admin", True)
    assert by_name["balance"] == ("everyone", False)


# ── gambler streak ────────────────────────────────────────────────────────────

async def test_gambler_streak_roundtrip(db):
    _state.gambler_streak.clear()
    _state.gambler_streak["1"] = {"date": "2026-05-01", "count": 3}
    _state.gambler_streak["2"] = {"date": "2026-04-29", "count": 1}
    await _persistence.save_gambler_streak()

    _state.gambler_streak.clear()
    await _persistence.init_db_state()
    assert _state.gambler_streak["1"] == {"date": "2026-05-01", "count": 3}
    assert _state.gambler_streak["2"] == {"date": "2026-04-29", "count": 1}


# ── jackpot ───────────────────────────────────────────────────────────────────

async def test_jackpot_roundtrip(db):
    await _persistence.save_jackpot(42424)
    # Wipe in-memory then reload
    _state.slot_jackpot = 0
    await _persistence.init_db_state()
    assert _state.slot_jackpot == 42424


# ── chess_games ───────────────────────────────────────────────────────────────

async def test_chess_games_roundtrip(db):
    """save_chess_game writes one row via per-row upsert; verify FEN/PGN
    and player ids round-trip through the column-per-field schema."""
    _state.active_chess_games.clear()
    _state.active_chess_games[12345] = {
        "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        "pgn": "1. e4",
        "white_id": 100,
        "black_id": 200,
        "current_id": 200,
        "amount": 0,
        "last_move": "Alice played e4",
        "board_msg_id": 555,
    }
    _state.active_chess_games[67890] = {
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "pgn": "",
        "white_id": 300,
        "black_id": 400,
        "current_id": 300,
        "amount": 500,
        "last_move": "",
        "board_msg_id": None,
    }
    await _persistence.save_chess_game(12345)
    await _persistence.save_chess_game(67890)

    _state.active_chess_games.clear()
    await _persistence.init_db_state()

    assert 12345 in _state.active_chess_games
    g = _state.active_chess_games[12345]
    assert g["white_id"] == 100 and g["black_id"] == 200
    assert g["current_id"] == 200
    assert g["pgn"] == "1. e4"
    assert "e4" in g["fen"] or "4P3" in g["fen"]
    assert _state.active_chess_games[67890]["amount"] == 500


async def test_chess_game_elo_roundtrip_for_bot_game(db):
    """Bot-vs-human games carry an elo field; persistence preserves it so
    a bot restart resumes at the right strength."""
    _state.active_chess_games.clear()
    _state.active_chess_games[44444] = {
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "pgn": "", "white_id": 11, "black_id": 22, "current_id": 11,
        "amount": 0, "last_move": "", "board_msg_id": None,
        "elo": 1750,
    }
    await _persistence.save_chess_game(44444)

    _state.active_chess_games.clear()
    await _persistence.init_db_state()

    g = _state.active_chess_games[44444]
    assert g.get("elo") == 1750


async def test_chess_game_no_elo_for_pvp(db):
    """PvP games omit the elo field; persistence + reload must NOT inject
    a spurious elo key on PvP rows (the bot-mention branch keys off its
    presence to know whether to spawn Stockfish)."""
    _state.active_chess_games.clear()
    _state.active_chess_games[55555] = {
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "pgn": "", "white_id": 33, "black_id": 44, "current_id": 33,
        "amount": 0, "last_move": "", "board_msg_id": None,
    }
    await _persistence.save_chess_game(55555)

    _state.active_chess_games.clear()
    await _persistence.init_db_state()

    assert "elo" not in _state.active_chess_games[55555]


async def test_chess_game_delete_removes_row(db):
    """delete_chess_game removes a single row; other games unaffected."""
    _state.active_chess_games.clear()
    base = {
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "pgn": "",
        "white_id": 1, "black_id": 2, "current_id": 1,
        "amount": 0, "last_move": "", "board_msg_id": None,
    }
    _state.active_chess_games[1] = dict(base, white_id=1)
    _state.active_chess_games[2] = dict(base, white_id=3)
    await _persistence.save_chess_game(1)
    await _persistence.save_chess_game(2)

    await _persistence.delete_chess_game(2)

    _state.active_chess_games.clear()
    await _persistence.init_db_state()
    assert 1 in _state.active_chess_games
    assert 2 not in _state.active_chess_games


async def test_chess_report_save_and_load(db):
    """save_chess_report returns the new auto-increment id; load_chess_report
    returns the row as a dict."""
    rid = await _persistence.save_chess_report(
        guild_id=42, channel_id=999, white_id=10, black_id=20,
        winner_id=10, result="1-0", pgn="1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#",
        final_fen="r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4",
    )
    assert rid is not None and rid > 0

    report = await _persistence.load_chess_report(rid)
    assert report is not None
    assert report["white_id"] == 10
    assert report["black_id"] == 20
    assert report["winner_id"] == 10
    assert report["result"] == "1-0"
    assert "Qxf7#" in report["pgn"]


async def test_chess_report_load_missing_returns_none(db):
    assert await _persistence.load_chess_report(999999) is None


async def test_chess_report_draw_has_null_winner(db):
    rid = await _persistence.save_chess_report(
        guild_id=42, channel_id=999, white_id=10, black_id=20,
        winner_id=None, result="1/2-1/2", pgn="1. e4 e5 (stalemate)",
        final_fen="8/8/8/8/8/3k4/3p4/3K4 w - - 0 1",
    )
    report = await _persistence.load_chess_report(rid)
    assert report["winner_id"] is None
    assert report["result"] == "1/2-1/2"


async def test_head_to_head_counts_both_orders_and_draws(db):
    """load_head_to_head treats white-vs-black and black-vs-white as one
    matchup, and counts NULL winner_id as a draw. Cross-guild totals."""
    base = dict(channel_id=1, pgn="", final_fen="-")
    # A=10 vs B=20: A wins twice, B wins once, one draw
    await _persistence.save_chess_report(guild_id=1, white_id=10, black_id=20,
                                          winner_id=10, result="1-0", **base)
    await _persistence.save_chess_report(guild_id=2, white_id=20, black_id=10,
                                          winner_id=10, result="0-1", **base)
    await _persistence.save_chess_report(guild_id=1, white_id=20, black_id=10,
                                          winner_id=20, result="1-0", **base)
    await _persistence.save_chess_report(guild_id=1, white_id=10, black_id=20,
                                          winner_id=None, result="1/2-1/2", **base)
    # Noise: a different pair shouldn't count.
    await _persistence.save_chess_report(guild_id=1, white_id=10, black_id=99,
                                          winner_id=10, result="1-0", **base)

    h2h = await _persistence.load_head_to_head(10, 20)
    assert h2h == {"wins_a": 2, "wins_b": 1, "draws": 1}

    # Swapped order swaps a/b counts.
    h2h_rev = await _persistence.load_head_to_head(20, 10)
    assert h2h_rev == {"wins_a": 1, "wins_b": 2, "draws": 1}


async def test_head_to_head_returns_zeros_when_no_games(db):
    h2h = await _persistence.load_head_to_head(777, 888)
    assert h2h == {"wins_a": 0, "wins_b": 0, "draws": 0}


async def test_count_pvp_wins_excludes_bot_games(db):
    """count_pvp_wins_in_guild filters out reports where either side is the
    bot user. Bot games shouldn't pad the PvP wins record."""
    BOT_UID = 999_000
    base = dict(channel_id=1, pgn="", final_fen="-")
    # Two PvP wins in guild 42
    await _persistence.save_chess_report(guild_id=42, white_id=10, black_id=20,
                                          winner_id=10, result="1-0", **base)
    await _persistence.save_chess_report(guild_id=42, white_id=10, black_id=30,
                                          winner_id=10, result="1-0", **base)
    # One bot win in guild 42 — must not be counted.
    await _persistence.save_chess_report(guild_id=42, white_id=10, black_id=BOT_UID,
                                          winner_id=10, result="1-0", **base)
    # A win in a different guild — also not counted.
    await _persistence.save_chess_report(guild_id=43, white_id=10, black_id=20,
                                          winner_id=10, result="1-0", **base)
    # A loss — not counted.
    await _persistence.save_chess_report(guild_id=42, white_id=10, black_id=20,
                                          winner_id=20, result="0-1", **base)

    wins = await _persistence.count_pvp_wins_in_guild(10, 42, BOT_UID)
    assert wins == 2


async def test_count_pvp_wins_zero_when_no_wins(db):
    BOT_UID = 999_000
    wins = await _persistence.count_pvp_wins_in_guild(5555, 42, BOT_UID)
    assert wins == 0


# ── ai_threads (ask, story, roleplay, rpg) ───────────────────────────────────

async def test_ai_threads_roundtrip_preserves_set_invited_ids(db):
    """ai_threads.invited_ids is a set in memory but a JSON list on disk —
    pin the round-trip so the set→list→set conversion stays correct."""
    _state.ai_threads.clear()
    _state.ai_threads[111] = {
        "kind": "ask",
        "owner_id": 7,
        "guild_id": 99,
        "invited_ids": {201, 202, 203},
        "system_prompt": "be brief",
        "character_prompt": None,
        "history": [{"role": "user", "content": "hi"}],
    }
    await _persistence.save_ai_threads()

    _state.ai_threads.clear()
    await _persistence.init_db_state()

    t = _state.ai_threads[111]
    assert t["kind"] == "ask"
    assert t["owner_id"] == 7
    assert t["guild_id"] == 99
    # Set on the way out, set on the way in.
    assert isinstance(t["invited_ids"], set)
    assert t["invited_ids"] == {201, 202, 203}
    assert t["system_prompt"] == "be brief"
    assert t["character_prompt"] is None
    assert t["history"] == [{"role": "user", "content": "hi"}]


async def test_ai_threads_roundtrip_handles_null_guild_id(db):
    """guild_id can be None for DMs — must round-trip through INT NULL column."""
    _state.ai_threads.clear()
    _state.ai_threads[222] = {
        "kind": "roleplay",
        "owner_id": 8,
        "guild_id": None,
        "invited_ids": set(),
        "system_prompt": None,
        "character_prompt": "wizard",
        "history": [],
    }
    await _persistence.save_ai_threads()

    _state.ai_threads.clear()
    await _persistence.init_db_state()

    t = _state.ai_threads[222]
    assert t["guild_id"] is None
    assert t["character_prompt"] == "wizard"
    assert t["invited_ids"] == set()


async def test_ai_threads_save_replaces_full_table(db):
    """save_ai_threads is DELETE-then-INSERT semantics; removed threads vanish."""
    _state.ai_threads.clear()
    _state.ai_threads[1] = {
        "kind": "story", "owner_id": 1, "guild_id": 1,
        "invited_ids": set(), "system_prompt": None,
        "character_prompt": None, "history": [],
    }
    _state.ai_threads[2] = {
        "kind": "story", "owner_id": 2, "guild_id": 1,
        "invited_ids": set(), "system_prompt": None,
        "character_prompt": None, "history": [],
    }
    await _persistence.save_ai_threads()

    del _state.ai_threads[2]
    await _persistence.save_ai_threads()

    _state.ai_threads.clear()
    await _persistence.init_db_state()
    assert 1 in _state.ai_threads
    assert 2 not in _state.ai_threads


# ── channel_prompts ───────────────────────────────────────────────────────────

async def test_channel_prompts_roundtrip(db):
    """save_channel_prompts(prompts: dict) DELETEs then INSERTs.
    Verify the int channel_id key + text round-trip cleanly."""
    _state.channel_prompts.clear()
    prompts = {
        555_001: "Be a pirate.",
        555_002: "Speak only in haiku.",
    }
    await _persistence.save_channel_prompts(prompts)

    _state.channel_prompts.clear()
    await _persistence.init_db_state()

    assert _state.channel_prompts[555_001] == "Be a pirate."
    assert _state.channel_prompts[555_002] == "Speak only in haiku."


async def test_channel_prompts_save_replaces_full_table(db):
    """Pass a dict missing previously-saved channels and verify they're gone."""
    _state.channel_prompts.clear()
    await _persistence.save_channel_prompts({1: "first", 2: "second"})
    await _persistence.save_channel_prompts({1: "first-only"})

    _state.channel_prompts.clear()
    await _persistence.init_db_state()
    assert _state.channel_prompts == {1: "first-only"}


async def test_channel_prompts_empty_dict_clears_table(db):
    """Saving an empty dict should leave the table empty."""
    _state.channel_prompts.clear()
    await _persistence.save_channel_prompts({1: "before"})
    await _persistence.save_channel_prompts({})

    _state.channel_prompts.clear()
    await _persistence.init_db_state()
    assert _state.channel_prompts == {}


# ── bot_roles / godmode_users ─────────────────────────────────────────────────

async def test_bot_roles_roundtrip(db):
    """Save + reload preserves both the set of role IDs and the per-guild
    rank map. Rank is stored as (guild_id, role_id) → rank_pos."""
    _state.bot_roles.clear()
    _state.bot_role_ranks.clear()
    _state.bot_roles.update({101, 202, 303})
    _state.bot_role_ranks.update({
        (42, 101): 1,
        (42, 202): 2,
        (42, 303): 3,
    })
    await _persistence.save_bot_roles()

    _state.bot_roles.clear()
    _state.bot_role_ranks.clear()
    await _persistence.init_db_state()
    assert _state.bot_roles == {101, 202, 303}
    assert _state.bot_role_ranks == {(42, 101): 1, (42, 202): 2, (42, 303): 3}


async def test_bot_roles_save_replaces_full_table(db):
    """save_bot_roles is DELETE-then-INSERT: removed roles vanish on next save."""
    _state.bot_roles.clear()
    _state.bot_role_ranks.clear()
    _state.bot_roles.update({1, 2, 3})
    _state.bot_role_ranks.update({(42, 1): 1, (42, 2): 2, (42, 3): 3})
    await _persistence.save_bot_roles()

    _state.bot_roles.discard(2)
    _state.bot_role_ranks.pop((42, 2), None)
    await _persistence.save_bot_roles()

    _state.bot_roles.clear()
    _state.bot_role_ranks.clear()
    await _persistence.init_db_state()
    assert _state.bot_roles == {1, 3}
    assert _state.bot_role_ranks == {(42, 1): 1, (42, 3): 3}


async def test_bot_roles_ranks_are_per_guild(db):
    """Two guilds can have independent rank ladders for their own bot
    roles. Role IDs are globally unique, so the same ID never appears in
    both guilds — but each guild's set of bot roles is ranked separately."""
    _state.bot_roles.clear()
    _state.bot_role_ranks.clear()
    _state.bot_roles.update({501, 502, 601, 602})
    _state.bot_role_ranks.update({
        (10, 501): 1,
        (10, 502): 2,
        (20, 601): 1,
        (20, 602): 2,
    })
    await _persistence.save_bot_roles()

    _state.bot_roles.clear()
    _state.bot_role_ranks.clear()
    await _persistence.init_db_state()

    # Each guild's rank=1 is its own role, not a global tie.
    g10_top = [rid for (g, rid), r in _state.bot_role_ranks.items() if g == 10 and r == 1]
    g20_top = [rid for (g, rid), r in _state.bot_role_ranks.items() if g == 20 and r == 1]
    assert g10_top == [501]
    assert g20_top == [601]


async def test_godmode_users_roundtrip(db):
    _state.godmode_users.clear()
    _state.godmode_users.update({4001, 4002})
    await _persistence.save_godmode_users()

    _state.godmode_users.clear()
    await _persistence.init_db_state()
    assert _state.godmode_users == {4001, 4002}


# ── rigged_slots / flips / scratch / steal ────────────────────────────────────

async def test_rigged_slots_roundtrip(db):
    _state.rigged_slots.clear()
    _state.rigged_slots["100"] = "🍒"
    _state.rigged_slots["200"] = "7️⃣"
    await _persistence.save_rigged_slots()

    _state.rigged_slots.clear()
    await _persistence.init_db_state()
    # init_db_state keys rigged_slots by str(uid).
    assert _state.rigged_slots == {"100": "🍒", "200": "7️⃣"}


async def test_rigged_slots_save_replaces_full_table(db):
    _state.rigged_slots.clear()
    _state.rigged_slots["1"] = "🍒"
    _state.rigged_slots["2"] = "🍋"
    await _persistence.save_rigged_slots()

    del _state.rigged_slots["2"]
    await _persistence.save_rigged_slots()

    _state.rigged_slots.clear()
    await _persistence.init_db_state()
    assert "1" in _state.rigged_slots
    assert "2" not in _state.rigged_slots


async def test_rigged_flips_roundtrip(db):
    _state.rigged_flips.clear()
    _state.rigged_flips[5001] = 3   # 3 guaranteed wins remaining
    _state.rigged_flips[5002] = 1
    await _persistence.save_rigged_flips()

    _state.rigged_flips.clear()
    await _persistence.init_db_state()
    # init_db_state keys rigged_flips by int(uid).
    assert _state.rigged_flips == {5001: 3, 5002: 1}


async def test_rigged_scratch_roundtrip(db):
    _state.rigged_scratch.clear()
    _state.rigged_scratch[6001] = 4   # forced 4-symbol match next scratch
    await _persistence.save_rigged_scratch()

    _state.rigged_scratch.clear()
    await _persistence.init_db_state()
    assert _state.rigged_scratch == {6001: 4}


async def test_rigged_steal_roundtrip(db):
    _state.rigged_steal.clear()
    _state.rigged_steal[7001] = 2   # 2 guaranteed steal successes remaining
    await _persistence.save_rigged_steal()

    _state.rigged_steal.clear()
    await _persistence.init_db_state()
    assert _state.rigged_steal == {7001: 2}


# ── leveling ──────────────────────────────────────────────────────────────────

async def test_leveling_targeted_save_writes_one_user(db):
    """save_leveling(guild_id, uid) writes exactly that user's row."""
    _state.leveling.clear()
    _state.leveling["42"] = {
        "100": {"xp": 500, "level": 3, "msg_today": 2},
        "200": {"xp": 1500, "level": 5, "msg_today": 0},
    }
    await _persistence.save_leveling(guild_id=42, uid=100)

    # Read uid=100 back via init_db_state
    _state.leveling.clear()
    await _persistence.init_db_state()
    assert _state.leveling["42"]["100"]["xp"] == 500
    assert _state.leveling["42"]["100"]["level"] == 3
    # uid=200 was NOT saved (targeted write).
    assert "200" not in _state.leveling.get("42", {})


async def test_leveling_bulk_save_writes_all(db):
    """save_leveling() with no args writes every row in state.leveling."""
    _state.leveling.clear()
    _state.leveling["42"] = {
        "100": {"xp": 100, "level": 1},
        "200": {"xp": 200, "level": 2},
    }
    _state.leveling["99"] = {
        "300": {"xp": 999, "level": 9},
    }
    await _persistence.save_leveling()

    _state.leveling.clear()
    await _persistence.init_db_state()
    assert _state.leveling["42"]["100"]["xp"] == 100
    assert _state.leveling["42"]["200"]["xp"] == 200
    assert _state.leveling["99"]["300"]["xp"] == 999


async def test_leveling_targeted_save_overwrites_existing_row(db):
    """ON DUPLICATE KEY UPDATE: re-saving the same (guild, uid) overwrites."""
    _state.leveling.clear()
    _state.leveling["42"] = {"100": {"xp": 100, "level": 1}}
    await _persistence.save_leveling(guild_id=42, uid=100)

    _state.leveling["42"]["100"]["xp"] = 999
    await _persistence.save_leveling(guild_id=42, uid=100)

    _state.leveling.clear()
    await _persistence.init_db_state()
    assert _state.leveling["42"]["100"]["xp"] == 999


# ── init_db_state reconnect guard ─────────────────────────────────────────────

async def test_init_db_state_is_idempotent_across_reconnects(db, monkeypatch):
    """on_ready fires on every gateway reconnect; the second+ call must be
    a no-op so a reconnect can't clobber in-memory mutations made since the
    last save (e.g. an in-progress chess game, an active ragebait).

    The conftest wrapper resets the guard before every call so existing
    tests can re-seed state freely. This test undoes that wrapper to
    exercise the production behavior directly.
    """
    # Reach into conftest's wrapper closure to find the real init_db_state.
    wrapper = _persistence.init_db_state
    real_init = wrapper.__closure__[0].cell_contents

    # First load: populates state from DB and sets the guard.
    _state.economy["users"]["7001"] = {"balance": 100, "last_daily": 0.0}
    await _persistence.save_economy()
    _state.economy["users"].clear()
    _persistence._init_db_state_done = False
    await real_init()
    assert _state.economy["users"]["7001"]["balance"] == 100
    assert _persistence._init_db_state_done is True

    # Mutate in-memory (simulating an unsaved live change).
    _state.economy["users"]["7001"]["balance"] = 999

    # Second call: the guard should make it a no-op so the live mutation survives.
    await real_init()
    assert _state.economy["users"]["7001"]["balance"] == 999


async def test_init_done_event_set_after_init_db_state(db):
    """on_message blocks on init_done so a fast command after restart can't
    hit _ensure_user against an empty state.economy["users"] and overwrite
    the user's real DB row with {balance: 0, daily_date: None}. The event
    must be set by the time init_db_state returns."""
    _persistence.init_done.clear()
    _persistence._init_db_state_done = False
    await _persistence.init_db_state()
    assert _persistence.init_done.is_set()


async def test_init_db_state_failure_leaves_guard_unset(db, monkeypatch):
    """If init_db_state raises (migrations fail, DB unreachable), the guard
    must stay False so a reconnect retries the full load instead of skipping
    it and unblocking on_message against partially-loaded state."""
    wrapper = _persistence.init_db_state
    real_init = wrapper.__closure__[0].cell_contents

    _persistence._init_db_state_done = False
    _persistence.init_done.clear()

    # Make run_migrations raise to simulate a startup-time failure.
    import src.migrations as _migrations

    async def _boom():
        raise RuntimeError("simulated migration failure")
    monkeypatch.setattr(_migrations, "run_migrations", _boom)

    with pytest.raises(RuntimeError, match="simulated migration failure"):
        await real_init()

    # Guard stayed False → next on_ready will retry instead of skipping.
    assert _persistence._init_db_state_done is False
    # init_done stayed unset → economy/leveling guards block instead of
    # writing zero-baselined rows from empty state.
    assert not _persistence.init_done.is_set()


async def test_add_balance_blocks_until_init_done(db):
    """A background task that calls add_balance before init_db_state has loaded
    state would otherwise see an empty state.economy["users"], create a fresh
    {balance: 0} entry, increment it, and UPSERT zero over the user's real row.
    _ensure_user must block on init_done to prevent this."""
    import asyncio
    import src.economy as _economy

    # Seed a real row in the DB and clear in-memory state to simulate a restart
    # where init_db_state hasn't run yet.
    _state.economy["users"]["8001"] = {"balance": 50_000, "last_daily": 0.0}
    await _persistence.save_economy()
    _state.economy["users"].clear()
    _persistence.init_done.clear()

    # add_balance must NOT proceed while init_done is unset.
    add_task = asyncio.create_task(_economy.add_balance(8001, 100))
    await asyncio.sleep(0.05)
    assert not add_task.done(), "add_balance should block on init_done"

    # Simulate init_db_state finishing: load the real row, then set the event.
    _persistence._init_db_state_done = False
    await _persistence.init_db_state()  # also sets init_done

    await add_task
    assert _state.economy["users"]["8001"]["balance"] == 50_100


async def test_grant_xp_blocks_until_init_done(db):
    """A background voice tick that calls grant_xp before init_db_state has
    loaded state.leveling would otherwise materialize a {xp: 0, level: 0} rec
    and UPSERT it over the user's real row. grant_xp must block on init_done."""
    import asyncio
    import src.leveling as _leveling

    # Seed a real leveling row and clear in-memory state.
    _state.leveling["42"] = {"8002": {"xp": 9_999, "level": 5,
                                       "msg_last_hour": 0.0, "msg_today": 0, "msg_day_ts": 0.0,
                                       "cmd_last_hour": 0.0, "cmd_today": 0, "cmd_day_ts": 0.0,
                                       "voice_last_30": 0.0, "voice_today": 0, "voice_day_ts": 0.0,
                                       "stream_last_hour": 0.0, "stream_today": 0, "stream_day_ts": 0.0}}
    _state.leveling.clear()
    _persistence.init_done.clear()

    grant_task = asyncio.create_task(_leveling.grant_xp(8002, "voice", guild_id=42))
    await asyncio.sleep(0.05)
    assert not grant_task.done(), "grant_xp should block on init_done"

    _persistence._init_db_state_done = False
    _state.leveling["42"] = {"8002": {"xp": 9_999, "level": 5,
                                       "msg_last_hour": 0.0, "msg_today": 0, "msg_day_ts": 0.0,
                                       "cmd_last_hour": 0.0, "cmd_today": 0, "cmd_day_ts": 0.0,
                                       "voice_last_30": 0.0, "voice_today": 0, "voice_day_ts": 0.0,
                                       "stream_last_hour": 0.0, "stream_today": 0, "stream_day_ts": 0.0}}
    _persistence.init_done.set()

    await grant_task
    # XP should have been added to the real seeded value, not started from 0.
    # Without the guard, the rec would have been re-created as {xp: 0} and
    # incremented to XP_VOICE (==10), which is the bug we're proving against.
    assert _state.leveling["42"]["8002"]["xp"] > 9_999


# ── restart_msg ───────────────────────────────────────────────────────────────

async def test_restart_msg_save_load_clear_cycle(db):
    """save → load → clear → load returns empty."""
    await _persistence.save_restart_msg(channel_id=8888, message_id=99999)

    loaded = await _persistence.load_restart_msg()
    assert loaded == {"channel_id": 8888, "message_id": 99999}

    await _persistence.clear_restart_msg()
    assert await _persistence.load_restart_msg() == {}


async def test_restart_msg_load_when_empty_returns_empty_dict(db):
    """No saved message → load returns {} (not None)."""
    assert await _persistence.load_restart_msg() == {}


async def test_restart_msg_save_overwrites_existing(db):
    """Only one restart_msg row exists at a time (id=1 PK)."""
    await _persistence.save_restart_msg(channel_id=1, message_id=2)
    await _persistence.save_restart_msg(channel_id=3, message_id=4)

    loaded = await _persistence.load_restart_msg()
    assert loaded == {"channel_id": 3, "message_id": 4}
