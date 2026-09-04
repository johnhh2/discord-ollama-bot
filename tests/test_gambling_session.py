"""!session gambling threads — src/gambling/session.py.

Covers: opening a thread (registry + DB row + the opening command list),
refusals (inside a thread, a second thread per owner, no create_thread),
the in-thread command allowlist gate (for anyone, not just the owner),
thread inheritance of the parent channel's game-channel / command-whitelist
status, the result hook that names the thread after its biggest winner or
loser (and coalesces renames to Discord's budget), `!stop` closing the
table (standing other players' live hands, keeping it open during a race,
non-owner refused, admin allowed, final name on the archive edit), the
archive/delete listeners, and the boot-time load.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import src.state as _state
import src.economy as _economy
import src.persistence as _persistence
import src.permissions as _permissions
import src.gambling.session as _session
from src.gambling.session import (
    GamblingSessionCog, GamblingThreadOnly, GAMBLING_THREAD_COMMAND_LINES,
    NEW_THREAD_NAME, RENAME_MIN_INTERVAL, leader_title,
)
from src.cogs.ai_cog import AICog
from src.events import EventsCog

from tests.fakes.discord import (
    FakeMember, FakeGuild, FakeChannel, FakeTextChannel, FakeThread, FakeCtx,
)


pytestmark = pytest.mark.asyncio

GUILD_ID = 42
PARENT_ID = 100
THREAD_ID = 777


class _FakeResp:
    def __init__(self, status: int):
        self.status = status
        self.reason = "test"


class _StubBot:
    """Just the two lookups the rename hook uses."""
    def __init__(self, guild, thread):
        self._guild, self._thread = guild, thread

    def get_channel(self, cid):
        return self._thread if cid == self._thread.id else None

    def get_guild(self, gid):
        return self._guild if gid == self._guild.id else None


@pytest.fixture(autouse=True)
def _record_wrong_channel(monkeypatch):
    """_wrong_channel_reply schedules 10 s delete tasks; record calls instead.
    Patched on both modules that bound it at import."""
    calls: list[tuple[str, str]] = []

    async def _stub(ctx_or_msg, text, *, title="❌ Wrong Channel"):
        calls.append((title, text))
    monkeypatch.setattr(_session, "_wrong_channel_reply", _stub)
    monkeypatch.setattr(_permissions, "_wrong_channel_reply", _stub)
    return calls


@pytest.fixture(autouse=True)
def _cancel_rename_tasks():
    """A queued (delayed) rename would otherwise outlive its test."""
    yield
    for row in _state.gambling_threads.values():
        task = row.get("_rename_task")
        if task is not None and not task.done():
            task.cancel()


def _row(owner_id: int) -> dict:
    return {"owner_id": owner_id, "guild_id": GUILD_ID, "parent_id": PARENT_ID, "created_at": 1, "tally": {}}


def _channel_ctx(author: FakeMember, guild: FakeGuild | None = None) -> FakeCtx:
    """A ctx in a guild text channel whose create_thread hands back a FakeThread
    (registered in guild.threads, as Discord's cache would)."""
    guild = guild or FakeGuild(gid=GUILD_ID)
    guild.members.append(author)
    channel = FakeTextChannel(ch_id=PARENT_ID)
    channel.guild = guild

    async def _create(name: str, **kwargs):
        thread = FakeThread(thread_id=THREAD_ID, name=name, parent_id=PARENT_ID)
        guild.threads.append(thread)
        return thread
    channel.create_thread = AsyncMock(side_effect=_create)
    return FakeCtx(author=author, guild=guild, channel=channel, command_name="session")


def _thread_ctx(author: FakeMember, thread_id: int = THREAD_ID, parent_id: int = PARENT_ID,
                guild: FakeGuild | None = None, command_name: str = "stop",
                thread: FakeThread | None = None) -> FakeCtx:
    guild = guild or FakeGuild(gid=GUILD_ID)
    thread = thread or FakeThread(thread_id=thread_id, parent_id=parent_id)
    return FakeCtx(author=author, guild=guild, channel=thread, command_name=command_name)


def _table(owner_id: int = 1001):
    """An open gambling thread with Alice (1001) and Bob (1002) at it, and a
    cog whose bot can find the thread — the rename hook is live."""
    guild = FakeGuild(gid=GUILD_ID)
    guild.members = [FakeMember(uid=1001, display_name="Alice"), FakeMember(uid=1002, display_name="Bob")]
    thread = FakeThread(thread_id=THREAD_ID, name=NEW_THREAD_NAME, parent_id=PARENT_ID)
    thread.guild = guild
    guild.threads.append(thread)
    _state.gambling_threads[THREAD_ID] = _row(owner_id)
    cog = GamblingSessionCog(bot=_StubBot(guild, thread))
    return cog, thread, guild


async def _result(uid: int, net: int, channel_id: int = THREAD_ID) -> None:
    """A gambling outcome as the games report it."""
    if net >= 0:
        await _economy.record_gambling_event(GUILD_ID, uid, gained=net, channel_id=channel_id)
    else:
        await _economy.record_gambling_event(GUILD_ID, uid, lost=-net, channel_id=channel_id)
    await asyncio.sleep(0)  # let an immediate rename task run


async def _db_rows() -> dict[int, str]:
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT thread_id, tally_json FROM gambling_threads")
            return {int(r[0]): r[1] for r in await cur.fetchall()}


# ── !session ──────────────────────────────────────────────────────────────────

async def test_session_opens_thread_lists_commands_and_persists(db):
    cog = GamblingSessionCog(bot=None)
    owner = FakeMember(uid=1001, display_name="Alice")
    ctx = _channel_ctx(owner)

    await cog.cmd_session.callback(cog, ctx)

    row = _state.gambling_threads[THREAD_ID]
    assert (row["owner_id"], row["guild_id"], row["parent_id"], row["tally"]) == (1001, GUILD_ID, PARENT_ID, {})
    assert set(await _db_rows()) == {THREAD_ID}

    ctx.channel.create_thread.assert_awaited_once()
    kwargs = ctx.channel.create_thread.call_args.kwargs
    assert kwargs["type"] == discord.ChannelType.public_thread
    assert kwargs["name"] == NEW_THREAD_NAME

    # The thread's first message lists every allowed command, in order.
    thread = ctx.guild.threads[0]
    thread.send.assert_awaited_once()
    opening = thread.send.call_args.kwargs["embed"].description
    positions = [opening.index(line) for line in GAMBLING_THREAD_COMMAND_LINES]
    assert positions == sorted(positions)
    assert "Anyone can play" in opening
    assert "<@1001>" in opening
    # No standalone "opened a thread" message in the channel — Discord's own
    # system line already points at the thread.
    assert not ctx.sent_embeds and not ctx.sent_messages


async def test_session_refused_inside_a_thread():
    cog = GamblingSessionCog(bot=None)
    ctx = _thread_ctx(FakeMember(uid=1002), thread_id=555, command_name="session")

    await cog.cmd_session.callback(cog, ctx)

    assert not _state.gambling_threads
    assert "nest" in ctx.sent_embeds[-1].description


async def test_session_one_open_thread_per_owner_per_guild():
    cog = GamblingSessionCog(bot=None)
    owner = FakeMember(uid=1003)
    ctx = _channel_ctx(owner)
    ctx.guild.threads.append(FakeThread(thread_id=900, parent_id=PARENT_ID))
    _state.gambling_threads[900] = _row(1003)

    await cog.cmd_session.callback(cog, ctx)

    ctx.channel.create_thread.assert_not_awaited()
    assert "<#900>" in ctx.sent_embeds[-1].description
    assert set(_state.gambling_threads) == {900}


async def test_session_drops_stale_row_and_opens_fresh(db):
    """A row whose thread is gone from the guild cache (deleted, or archived
    while the bot was down) must not block the owner forever."""
    cog = GamblingSessionCog(bot=None)
    _state.gambling_threads[900] = _row(1004)
    await _persistence.save_gambling_thread(900)
    ctx = _channel_ctx(FakeMember(uid=1004))  # guild.threads is empty → 900 is gone

    await cog.cmd_session.callback(cog, ctx)

    assert set(_state.gambling_threads) == {THREAD_ID}
    assert set(await _db_rows()) == {THREAD_ID}


async def test_session_reports_when_thread_cannot_open():
    cog = GamblingSessionCog(bot=None)
    ctx = _channel_ctx(FakeMember(uid=1005))
    ctx.channel.create_thread = AsyncMock(side_effect=discord.Forbidden(_FakeResp(403), "no"))

    await cog.cmd_session.callback(cog, ctx)

    assert not _state.gambling_threads
    assert "Create Public Threads" in ctx.sent_embeds[-1].description
    assert _state.audit_log[-1]["error"].startswith("Bot Permission Error")


async def test_session_needs_a_channel_that_hosts_threads():
    """FakeChannel has no create_thread (a DM / non-text channel)."""
    cog = GamblingSessionCog(bot=None)
    guild = FakeGuild(gid=GUILD_ID)
    ctx = FakeCtx(author=FakeMember(uid=1006), guild=guild, channel=FakeChannel(ch_id=PARENT_ID, guild=guild))

    await cog.cmd_session.callback(cog, ctx)

    assert not _state.gambling_threads
    assert "Couldn't Open" in ctx.sent_embeds[-1].title


# ── the in-thread command gate ────────────────────────────────────────────────

async def test_gate_lets_anyone_gamble_in_the_thread_and_nothing_else(_record_wrong_channel):
    """The gate is about the command, never the user: uid 1 isn't the owner."""
    cog = GamblingSessionCog(bot=None)
    _state.gambling_threads[THREAD_ID] = _row(1001)
    stranger = FakeMember(uid=1)

    for cmd in ("slots", "flip", "scratchoff", "scratches", "blackjack", "race", "stop"):
        assert await cog.bot_check(_thread_ctx(stranger, command_name=cmd)) is True
    assert not _record_wrong_channel

    for cmd in ("balance", "daily property", "rig slots", "session", "chess", "ask"):
        with pytest.raises(GamblingThreadOnly):
            await cog.bot_check(_thread_ctx(stranger, command_name=cmd))
    title, text = _record_wrong_channel[-1]
    assert title == "🎰 Gambling Thread"
    for line in GAMBLING_THREAD_COMMAND_LINES:
        assert line in text


async def test_gate_is_inert_outside_gambling_threads():
    cog = GamblingSessionCog(bot=None)
    _state.gambling_threads[THREAD_ID] = _row(1001)
    author = FakeMember(uid=1)

    # An ordinary thread and a text channel: nothing is filtered.
    assert await cog.bot_check(_thread_ctx(author, thread_id=778, command_name="balance")) is True
    plain = FakeCtx(author=author, guild=FakeGuild(gid=GUILD_ID), channel=FakeChannel(ch_id=5), command_name="balance")
    assert await cog.bot_check(plain) is True


# ── threads inherit the parent channel's gate status ─────────────────────────

async def test_game_channel_gate_accepts_thread_under_a_game_channel(_record_wrong_channel):
    _state.guild_settings[str(GUILD_ID)] = {"game_channels": [PARENT_ID]}
    author = FakeMember(uid=1)

    inside = _thread_ctx(author, parent_id=PARENT_ID, command_name="slots")
    assert await _permissions.check_game_channel(inside, "Gambling") is False
    assert not _record_wrong_channel

    elsewhere = _thread_ctx(author, thread_id=778, parent_id=555, command_name="slots")
    assert await _permissions.check_game_channel(elsewhere, "Gambling") is True
    assert f"<#{PARENT_ID}>" in _record_wrong_channel[-1][1]


async def test_chess_channel_gate_accepts_thread_under_a_chess_channel():
    _state.guild_settings[str(GUILD_ID)] = {"chess_channels": [PARENT_ID]}
    author = FakeMember(uid=1)
    assert await _permissions.check_chess_channel(_thread_ctx(author, parent_id=PARENT_ID)) is False
    assert await _permissions.check_chess_channel(_thread_ctx(author, parent_id=555)) is True


async def test_command_whitelist_and_blacklist_cover_threads_under_a_listed_channel():
    cog = EventsCog(bot=None)
    author = FakeMember(uid=1)
    inside = _thread_ctx(author, parent_id=PARENT_ID, command_name="slots")
    outside = _thread_ctx(author, thread_id=778, parent_id=555, command_name="slots")

    _state.guild_settings[str(GUILD_ID)] = {"command_whitelist": [PARENT_ID]}
    assert await cog.bot_check(inside) is True
    assert await cog.bot_check(outside) is False

    _state.guild_settings[str(GUILD_ID)] = {"command_blacklist": [PARENT_ID]}
    assert await cog.bot_check(inside) is False
    assert await cog.bot_check(outside) is True


async def test_gate_channel_ids_shapes():
    assert _permissions.gate_channel_ids(FakeChannel(ch_id=5)) == (5,)
    assert _permissions.gate_channel_ids(FakeThread(thread_id=7, parent_id=5)) == (7, 5)
    # A thread the fake didn't give a parent to still gates on its own id.
    assert _permissions.gate_channel_ids(FakeThread(thread_id=7)) == (7,)


# ── the thread name follows the table's leader ───────────────────────────────

async def test_leader_title_rules():
    assert leader_title({"tally": {}}) is None
    assert leader_title({"tally": {1: {"net": 0, "name": "A"}}}) is None  # pushes only
    assert leader_title({"tally": {1: {"net": -100, "name": "A"}, 2: {"net": 250, "name": "B"}}}) == "B gained 250 coins"
    assert leader_title({"tally": {1: {"net": -100, "name": "A"}, 2: {"net": -300, "name": "B"}}}) == "B lost 300 coins"
    assert leader_title({"tally": {1: {"net": 1500, "name": None}}}) == "1 gained 1,500 coins"
    # Ties go to whoever got there first.
    assert leader_title({"tally": {1: {"net": 5, "name": "A"}, 2: {"net": 5, "name": "B"}}}) == "A gained 5 coins"
    long = leader_title({"tally": {1: {"net": 5, "name": "x" * 200}}})
    assert len(long) == 100 and long.endswith(" gained 5 coins")


async def test_result_hook_tallies_and_renames_after_top_gainer():
    cog, thread, _ = _table()

    await _result(1001, 500)

    assert _state.gambling_threads[THREAD_ID]["tally"] == {1001: {"net": 500, "name": "Alice"}}
    thread.edit.assert_awaited_once_with(name="Alice gained 500 coins")


async def test_result_hook_falls_back_to_biggest_loser_when_nobody_is_up():
    cog, thread, _ = _table()

    await _result(1001, -200)
    thread.edit.assert_awaited_once_with(name="Alice lost 200 coins")


async def test_hook_survives_a_reboot_with_the_persisted_tally(db):
    cog, thread, _ = _table()
    await _result(1001, 300)
    await _result(1002, -50)
    rows = await _db_rows()
    assert '"1001"' in rows[THREAD_ID]

    _state.gambling_threads.clear()
    await _persistence.init_db_state()

    assert _state.gambling_threads[THREAD_ID]["tally"] == {
        1001: {"net": 300, "name": "Alice"}, 1002: {"net": -50, "name": "Bob"},
    }


async def test_results_elsewhere_and_zero_nets_leave_the_thread_alone():
    cog, thread, _ = _table()

    await _result(1001, 500, channel_id=555)          # another channel
    await _economy.record_gambling_event(GUILD_ID, 1001, gained=0, channel_id=THREAD_ID)
    await asyncio.sleep(0)

    assert _state.gambling_threads[THREAD_ID]["tally"] == {}
    thread.edit.assert_not_awaited()


async def test_renames_are_coalesced_to_discords_name_budget():
    cog, thread, _ = _table()
    row = _state.gambling_threads[THREAD_ID]

    await _result(1001, 500)
    assert thread.edit.await_count == 1
    thread.name = "Alice gained 500 coins"

    # Bob overtakes inside the budget window: no second edit yet, one
    # delayed rename is queued, and further results don't queue more.
    await _result(1002, 900)
    assert thread.edit.await_count == 1
    task = row["_rename_task"]
    assert not task.done()
    await _result(1001, 1000)
    assert row["_rename_task"] is task

    # When it fires it applies the tally as it stands then, not the one
    # that queued it.
    task.cancel()
    await cog._rename_after(THREAD_ID, 0.0)
    thread.edit.assert_awaited_with(name="Alice gained 1,500 coins")
    assert thread.edit.await_count == 2


async def test_rename_is_skipped_when_the_name_already_matches():
    cog, thread, _ = _table()
    thread.name = "Alice gained 500 coins"
    await _result(1001, 500)
    thread.edit.assert_not_awaited()


async def test_rename_failure_is_logged_not_raised():
    cog, thread, _ = _table()
    thread.edit = AsyncMock(side_effect=discord.HTTPException(_FakeResp(429), "slow down"))
    await _result(1001, 500)  # the hook runs inside record_gambling_event
    assert _state.gambling_threads[THREAD_ID]["tally"][1001]["net"] == 500


async def test_a_raising_hook_never_breaks_the_result_path():
    async def _boom(*args):
        raise RuntimeError("hook bug")
    _economy.GAMBLING_RESULT_HOOKS.append(_boom)

    await _economy.record_gambling_event(GUILD_ID, 1001, gained=5, channel_id=THREAD_ID)

    assert _state.gambling_today_by_user[(GUILD_ID, "1001")]["gained"] == 5


# ── !stop closes the table ────────────────────────────────────────────────────

def _hand(amount: int, channel_id: int, player: list[str], dealer: list[str]) -> dict:
    card = lambda r: {"rank": r, "suit": "♠"}  # noqa: E731
    return {
        "amount": amount, "channel_id": channel_id,
        "player_hand": [card(r) for r in player],
        "dealer_hand": [card(r) for r in dealer],
        "deck": [card("2")] * 10,
    }


async def test_stop_by_owner_closes_table_and_stands_live_hands(db):
    ai = AICog(bot=None)
    owner = FakeMember(uid=1001, display_name="Alice")
    bob = FakeMember(uid=1002, display_name="Bob")
    guild = FakeGuild(gid=GUILD_ID)
    guild.members = [owner, bob]
    ctx = _thread_ctx(owner, guild=guild)
    _state.gambling_threads[THREAD_ID] = _row(owner.id)
    await _persistence.save_gambling_thread(THREAD_ID)

    # Bob's dealt hand at this table: 19 vs a dealer 17 (stands) — standing
    # wins him 2× the stake. A hand at another table is left alone.
    _state.active_blackjack_games[bob.id] = _hand(50, THREAD_ID, ["10", "9"], ["10", "7"])
    _state.active_blackjack_games[1003] = _hand(50, 999, ["10", "9"], ["10", "7"])

    await ai.cmd_stop.callback(ai, ctx)

    assert THREAD_ID not in _state.gambling_threads
    assert await _db_rows() == {}
    assert bob.id not in _state.active_blackjack_games
    assert 1003 in _state.active_blackjack_games
    assert await _economy.get_balance(bob.id) == 100
    # Bob's result posted into the thread before it was archived.
    assert any("Bob" in c.kwargs["embed"].description for c in ctx.channel.send.call_args_list)
    ctx.channel.edit.assert_awaited_once()
    assert ctx.channel.edit.call_args.kwargs["archived"] is True
    summary = ctx.sent_embeds[-1].description
    assert "Gambling thread (closed)" in summary
    assert "Bob" in summary


async def test_stop_by_owner_stands_their_own_hand_instead_of_forfeiting(db):
    ai = AICog(bot=None)
    owner = FakeMember(uid=1001, display_name="Alice")
    guild = FakeGuild(gid=GUILD_ID)
    guild.members = [owner]
    ctx = _thread_ctx(owner, guild=guild)
    _state.gambling_threads[THREAD_ID] = _row(owner.id)
    _state.active_blackjack_games[owner.id] = _hand(50, THREAD_ID, ["10", "9"], ["10", "7"])

    await ai.cmd_stop.callback(ai, ctx)

    assert owner.id not in _state.active_blackjack_games
    assert await _economy.get_balance(owner.id) == 100
    assert "forfeited" not in ctx.sent_embeds[-1].description


async def test_stop_rides_the_final_name_on_the_archive_when_budget_allows(db):
    cog, thread, guild = _table()
    owner = guild.get_member(1001)
    ctx = _thread_ctx(owner, guild=guild, thread=thread)
    row = _state.gambling_threads[THREAD_ID]

    await _result(1002, 400)                   # immediate rename → "Bob gained 400 coins"
    thread.name = "Bob gained 400 coins"
    thread.edit.reset_mock()
    await _result(1001, 900)                   # inside the window: queued
    pending = row["_rename_task"]
    assert not pending.done()

    # Budget spent: the archive goes out without a name; the queue is dropped.
    await AICog(bot=None).cmd_stop.callback(AICog(bot=None), ctx)
    await asyncio.sleep(0)
    assert pending.cancelled()
    assert ctx.channel.edit.call_args.kwargs == {"archived": True, "locked": True}
    assert THREAD_ID not in _state.gambling_threads

    # Same close with the budget free: rename and archive in one edit.
    _state.gambling_threads[THREAD_ID] = {**_row(1001), "tally": {1001: {"net": 900, "name": "Alice"}}}
    _state.gambling_threads[THREAD_ID]["_renamed_at"] = time.monotonic() - RENAME_MIN_INTERVAL - 1
    thread.edit.reset_mock()
    await AICog(bot=None).cmd_stop.callback(AICog(bot=None), ctx)
    assert ctx.channel.edit.call_args.kwargs == {
        "archived": True, "locked": True, "name": "Alice gained 900 coins",
    }


async def test_stop_by_non_owner_leaves_table_open():
    ai = AICog(bot=None)
    ctx = _thread_ctx(FakeMember(uid=1002))
    _state.gambling_threads[THREAD_ID] = _row(1001)

    await ai.cmd_stop.callback(ai, ctx)

    assert THREAD_ID in _state.gambling_threads
    ctx.channel.edit.assert_not_awaited()
    assert "<@1001>" in ctx.sent_embeds[-1].description


async def test_stop_by_admin_closes_someone_elses_table(monkeypatch):
    ai = AICog(bot=None)
    monkeypatch.setattr(_state, "bot_admins", {1002})
    ctx = _thread_ctx(FakeMember(uid=1002))
    _state.gambling_threads[THREAD_ID] = _row(1001)

    await ai.cmd_stop.callback(ai, ctx)

    assert THREAD_ID not in _state.gambling_threads
    assert ctx.channel.edit.call_args.kwargs["archived"] is True


async def test_stop_keeps_table_open_while_a_race_runs():
    ai = AICog(bot=None)
    ctx = _thread_ctx(FakeMember(uid=1001))
    _state.gambling_threads[THREAD_ID] = _row(1001)
    _state.active_race_games[THREAD_ID] = {
        "players": [3, 4], "names": {}, "positions": {3: 0, 4: 0}, "amount": 10,
    }

    await ai.cmd_stop.callback(ai, ctx)

    assert THREAD_ID in _state.gambling_threads
    assert THREAD_ID in _state.active_race_games
    ctx.channel.edit.assert_not_awaited()
    assert "race is still running" in ctx.sent_embeds[-1].description


# ── thread listeners ──────────────────────────────────────────────────────────

async def test_thread_delete_forgets_and_refunds_live_hands():
    cog = GamblingSessionCog(bot=None)
    _state.gambling_threads[THREAD_ID] = _row(1001)
    _state.active_blackjack_games[1002] = _hand(50, THREAD_ID, ["10", "9"], ["10", "7"])
    _state.active_blackjack_games[1003] = _hand(50, 999, ["10", "9"], ["10", "7"])

    await cog.on_thread_delete(FakeThread(thread_id=THREAD_ID))

    assert THREAD_ID not in _state.gambling_threads
    assert 1002 not in _state.active_blackjack_games
    assert await _economy.get_balance(1002) == 50
    assert 1003 in _state.active_blackjack_games


async def test_thread_delete_of_an_unrelated_thread_is_a_noop():
    cog = GamblingSessionCog(bot=None)
    _state.gambling_threads[THREAD_ID] = _row(1001)
    _state.active_blackjack_games[1002] = _hand(50, 555, ["10", "9"], ["10", "7"])

    await cog.on_thread_delete(FakeThread(thread_id=555))

    assert THREAD_ID in _state.gambling_threads
    assert 1002 in _state.active_blackjack_games


async def test_archiving_the_thread_forgets_it(db):
    cog = GamblingSessionCog(bot=None)
    _state.gambling_threads[THREAD_ID] = _row(1001)
    await _persistence.save_gambling_thread(THREAD_ID)

    # A rename keeps it; the archive transition drops it.
    await cog.on_thread_update(FakeThread(thread_id=THREAD_ID), FakeThread(thread_id=THREAD_ID, name="renamed"))
    assert THREAD_ID in _state.gambling_threads

    await cog.on_thread_update(FakeThread(thread_id=THREAD_ID), FakeThread(thread_id=THREAD_ID, archived=True))
    assert THREAD_ID not in _state.gambling_threads
    assert await _db_rows() == {}


# ── boot-time load ────────────────────────────────────────────────────────────

async def test_init_db_state_loads_gambling_threads(db):
    _state.gambling_threads[THREAD_ID] = _row(1001)
    await _persistence.save_gambling_thread(THREAD_ID)
    _state.gambling_threads.clear()

    await _persistence.init_db_state()

    assert _state.gambling_threads == {THREAD_ID: _row(1001)}


async def test_save_gambling_thread_with_no_row_deletes(db):
    _state.gambling_threads[THREAD_ID] = _row(1001)
    await _persistence.save_gambling_thread(THREAD_ID)
    _state.gambling_threads.clear()
    await _persistence.save_gambling_thread(THREAD_ID)
    assert await _db_rows() == {}


async def test_opening_embed_names_owner():
    owner = SimpleNamespace(mention="<@9>")
    e = _session.opening_embed(owner)
    assert "<@9>" in e.description
    assert e.description.startswith("Only these commands work in here:")
