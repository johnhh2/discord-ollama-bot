"""Coverage for the notable_events table and its !recap integration.

notable_events fills a gap the all-time `records` table can't: it logs
*when* a record was broken or a lottery was won, keyed on the 5am-CT day
string, so !recap can mention "set today" rather than just listing
standings.

Three things under test:
  1. The persistence round-trip (log_notable_event → load_notable_events_today),
     plus the day-scoping and prune behavior — exercised against the real
     SQLite-backed `db` fixture.
  2. announce_record() also writing a kind='record' row — it's the single
     hook point every record category funnels through.
  3. !recap's _build_recap_events_block folding notable_events + the day's
     top gambling/crime hauls into the AI prompt.
"""

import pytest

from src.cogs.ai_cog import AICog
from src.economy import _ct_today, _ct_now
from src.persistence.notable_events import (
    log_notable_event, load_notable_events_today, prune_notable_events,
)

from tests.fakes.discord import FakeMember, FakeGuild


pytestmark = pytest.mark.asyncio


# ── persistence round-trip ────────────────────────────────────────────────────

async def test_log_and_load_notable_events_round_trip(db):
    day = _ct_today()
    await log_notable_event(42, day, "lottery_win", None, "Nick", 50_000)
    await log_notable_event(42, day, "record", "slots_jackpot", "Joseph", 15_000)

    rows = await load_notable_events_today(42, day)

    assert len(rows) == 2
    # Oldest-first ordering (by insert id).
    assert rows[0] == {"kind": "lottery_win", "category": None, "holder_name": "Nick", "value": 50_000}
    assert rows[1] == {"kind": "record", "category": "slots_jackpot", "holder_name": "Joseph", "value": 15_000}


async def test_load_notable_events_is_scoped_by_guild_and_day(db):
    day = _ct_today()
    await log_notable_event(42, day, "lottery_win", None, "Nick", 50_000)
    await log_notable_event(99, day, "lottery_win", None, "OtherGuildUser", 1_000)
    await log_notable_event(42, "2000-01-01", "lottery_win", None, "Ancient", 7)

    rows = await load_notable_events_today(42, day)

    holders = {r["holder_name"] for r in rows}
    assert holders == {"Nick"}  # other guild + other day both excluded


async def test_log_notable_event_with_none_guild_is_noop(db):
    # DM context — guild_id is None. Must not raise, must not insert.
    await log_notable_event(None, _ct_today(), "record", "flip", "Nobody", 1)
    rows = await load_notable_events_today(42, _ct_today())
    assert rows == []


async def test_prune_notable_events_drops_old_rows_only(db):
    day = _ct_today()
    await log_notable_event(42, day, "lottery_win", None, "Recent", 100)
    await log_notable_event(42, "2000-01-01", "lottery_win", None, "Old", 1)

    await prune_notable_events(before_date="2020-01-01")

    rows = await load_notable_events_today(42, day)
    assert [r["holder_name"] for r in rows] == ["Recent"]
    # The old row is gone; loading its day returns nothing.
    assert await load_notable_events_today(42, "2000-01-01") == []


# ── announce_record hook ──────────────────────────────────────────────────────

async def test_announce_record_logs_a_notable_event(db, monkeypatch):
    """announce_record is the single hook point for every record category —
    after sending the embed it must also write a kind='record' row."""
    from src.helpers import announce_record

    # Fake channel with a .guild and an awaitable .send.
    class _Chan:
        def __init__(self, guild):
            self.guild = guild
            self.sent = []
        async def send(self, *a, **kw):
            self.sent.append((a, kw))

    guild = FakeGuild(gid=42)
    chan = _Chan(guild)

    await announce_record(chan, "slots_jackpot", "Joseph", 15_000)

    # The announcement embed still went out.
    assert len(chan.sent) == 1
    # And a notable_events row was logged for it.
    rows = await load_notable_events_today(42, _ct_today())
    assert rows == [{
        "kind": "record", "category": "slots_jackpot",
        "holder_name": "Joseph", "value": 15_000,
    }]


async def test_announce_record_with_no_guild_channel_does_not_log(db):
    """A channel without a .guild (shouldn't happen, but be defensive) must
    not raise and must not log — the announcement path still runs."""
    from src.helpers import announce_record

    class _Chan:
        guild = None
        def __init__(self):
            self.sent = []
        async def send(self, *a, **kw):
            self.sent.append(1)

    chan = _Chan()
    await announce_record(chan, "flip", "Joseph", 999)

    assert chan.sent == [1]  # embed still sent
    assert await load_notable_events_today(42, _ct_today()) == []


# ── !recap events block ───────────────────────────────────────────────────────

async def test_recap_events_block_includes_records_and_lottery(db):
    day = _ct_today()
    await log_notable_event(42, day, "lottery_win", None, "Nick", 50_000)
    await log_notable_event(42, day, "record", "slots_jackpot", "Joseph", 15_000)
    await log_notable_event(42, day, "record", "hangman_wins_easy", "Sara", 12)

    cog = AICog(bot=None)
    block = await cog._build_recap_events_block(42, day)

    assert "<notable_events>" in block
    assert "Nick won the lottery (50,000 coins)" in block
    assert "Joseph set a new server record" in block and "15,000 coins" in block
    # hangman_wins_* records read as "wins", not "coins".
    assert "Sara set a new server record" in block and "12 wins" in block


async def test_recap_events_block_includes_top_gambling_and_crime(db, monkeypatch):
    """The block should surface the day's biggest gambling wins and
    successful crimes, pulled from *_history (not scraped from embeds)."""
    from src.persistence.history import upsert_gambling_delta, upsert_crime_delta
    from src.economy import _current_bucket_ct

    cal_today = _ct_now().date().isoformat()
    bucket = _current_bucket_ct()

    # Two gamblers (one big winner, one net loser → excluded) and one thief.
    await upsert_gambling_delta(cal_today, bucket, 1001, gained=8_000, lost=0)
    await upsert_gambling_delta(cal_today, bucket, 1002, gained=0, lost=3_000)
    await upsert_crime_delta(cal_today, bucket, 1003, gained=4_200, lost=0)

    # _recap_resolve_name resolves ids via fetch_member() — get_member()
    # finds these because they're in guild.members.
    winner = FakeMember(uid=1001, display_name="BigWinner")
    thief = FakeMember(uid=1003, display_name="SneakyThief")
    guild = FakeGuild(gid=42)
    guild.members = [winner, thief]

    class _Bot:
        def get_guild(self, gid):
            return guild if gid == 42 else None

    cog = AICog(bot=_Bot())
    block = await cog._build_recap_events_block(42, _ct_today())

    # Each user is the sole entry in its category, so the top-1 rule shows
    # both even though neither clears the 10k floor.
    assert "BigWinner won 8,000 coins gambling" in block
    assert "SneakyThief pulled off 4,200 coins in crime" in block
    # The net loser is not mentioned.
    assert "1002" not in block


async def test_recap_resolve_name_falls_back_to_api_fetch(db):
    """The bot runs without the members intent, so guild.get_member() misses
    for most users. _recap_resolve_name must fall through to an API fetch
    (guild.fetch_member, then bot.fetch_user) instead of printing raw ids —
    this is the bug behind 'user 1489430987489149110' in real recaps."""
    from src.persistence.history import upsert_gambling_delta
    from src.economy import _current_bucket_ct

    cal_today = _ct_now().date().isoformat()
    bucket = _current_bucket_ct()
    await upsert_gambling_delta(cal_today, bucket, 555, gained=20_000, lost=0)

    # Guild with an EMPTY member cache — get_member(555) returns None.
    guild = FakeGuild(gid=42)
    guild.members = []

    async def _fetch_member(uid):
        # Simulates the API fetch fetch_member() falls through to.
        if uid == 555:
            return FakeMember(uid=555, display_name="ApiResolved")
        raise Exception("not found")
    guild.fetch_member = _fetch_member

    class _Bot:
        def get_guild(self, gid):
            return guild if gid == 42 else None
        async def fetch_user(self, uid):
            raise AssertionError("should have resolved via fetch_member first")

    cog = AICog(bot=_Bot())
    block = await cog._build_recap_events_block(42, _ct_today())

    assert "ApiResolved won 20,000 coins gambling" in block
    assert "555" not in block


async def test_recap_resolve_name_resolves_a_bot_via_fetch_user(db):
    """A bot account that isn't a guild member: guild.fetch_member 404s,
    but bot.fetch_user still resolves it. This is the 'other bot' case —
    it should show the bot's name, not a raw id."""
    from src.persistence.history import upsert_gambling_delta
    from src.economy import _current_bucket_ct

    cal_today = _ct_now().date().isoformat()
    bucket = _current_bucket_ct()
    await upsert_gambling_delta(cal_today, bucket, 888, gained=30_000, lost=0)

    guild = FakeGuild(gid=42)
    guild.members = []

    async def _fetch_member(uid):
        raise Exception("404 Not Found (the bot isn't a member)")
    guild.fetch_member = _fetch_member

    class _Bot:
        def get_guild(self, gid):
            return guild
        async def fetch_user(self, uid):
            # fetch_user works for bot accounts and non-members alike.
            m = FakeMember(uid=uid, display_name="OtherBot")
            m.bot = True
            return m

    cog = AICog(bot=_Bot())
    block = await cog._build_recap_events_block(42, _ct_today())

    assert "OtherBot won 30,000 coins gambling" in block
    assert "888" not in block


async def test_recap_resolve_name_logs_diagnostic_when_all_paths_fail(db, caplog):
    """If every resolution step misses, _recap_resolve_name must (a) fall
    back to a bare id and (b) log recap_name_unresolved with the cause of
    each failed fetch — so a real recap printing 'user <id>' is debuggable
    instead of silent."""
    import logging
    from src.persistence.history import upsert_gambling_delta
    from src.economy import _current_bucket_ct

    cal_today = _ct_now().date().isoformat()
    bucket = _current_bucket_ct()
    await upsert_gambling_delta(cal_today, bucket, 999, gained=5_000, lost=0)

    guild = FakeGuild(gid=42)
    guild.members = []

    async def _fetch_member(uid):
        raise RuntimeError("member fetch boom")
    guild.fetch_member = _fetch_member

    class _Bot:
        def get_guild(self, gid):
            return guild
        async def fetch_user(self, uid):
            raise RuntimeError("user fetch boom")

    cog = AICog(bot=_Bot())
    with caplog.at_level(logging.WARNING):
        block = await cog._build_recap_events_block(42, _ct_today())

    # Bare-id fallback in the output.
    assert "user 999 won 5,000 coins gambling" in block
    # And a diagnostic record naming both failures.
    rec = next(r for r in caplog.records if r.message == "recap_name_unresolved")
    assert rec.user_id == 999
    assert "member fetch boom" in rec.fetch_member_error
    assert "user fetch boom" in rec.fetch_user_error


async def test_recap_events_block_top1_unconditional_then_floor(db):
    """Trim rule: the single biggest gambling win always shows; further
    entries only if they clear RECAP_EVENT_FLOOR, capped at RECAP_EVENT_MAX."""
    from src.persistence.history import upsert_gambling_delta
    from src.economy import _current_bucket_ct

    cal_today = _ct_now().date().isoformat()
    bucket = _current_bucket_ct()
    # Mirrors the user's real output: one huge, two over-floor, two under.
    await upsert_gambling_delta(cal_today, bucket, 1, gained=74_173, lost=0)
    await upsert_gambling_delta(cal_today, bucket, 2, gained=14_073, lost=0)
    await upsert_gambling_delta(cal_today, bucket, 3, gained=11_100, lost=0)
    await upsert_gambling_delta(cal_today, bucket, 4, gained=2_600, lost=0)
    await upsert_gambling_delta(cal_today, bucket, 5, gained=1_100, lost=0)

    guild = FakeGuild(gid=42)
    guild.members = [FakeMember(uid=i, display_name=f"u{i}") for i in range(1, 6)]

    class _Bot:
        def get_guild(self, gid):
            return guild

    cog = AICog(bot=_Bot())
    block = await cog._build_recap_events_block(42, _ct_today())

    # Top-1 (74,173) + two over the 10k floor; the 2,600 and 1,100 drop.
    assert "u1 won 74,173" in block
    assert "u2 won 14,073" in block
    assert "u3 won 11,100" in block
    assert "u4" not in block
    assert "u5" not in block


async def test_recap_events_block_lone_small_win_still_shows(db):
    """Top-1 is unconditional — a quiet day's single sub-floor win still
    gets a line rather than the block being empty."""
    from src.persistence.history import upsert_gambling_delta
    from src.economy import _current_bucket_ct

    cal_today = _ct_now().date().isoformat()
    bucket = _current_bucket_ct()
    await upsert_gambling_delta(cal_today, bucket, 1, gained=300, lost=0)

    guild = FakeGuild(gid=42)
    guild.members = [FakeMember(uid=1, display_name="QuietDayWinner")]

    class _Bot:
        def get_guild(self, gid):
            return guild

    cog = AICog(bot=_Bot())
    block = await cog._build_recap_events_block(42, _ct_today())

    assert "QuietDayWinner won 300 coins gambling" in block


async def test_recap_events_block_empty_when_nothing_notable(db):
    cog = AICog(bot=None)
    block = await cog._build_recap_events_block(42, _ct_today())
    assert block == ""


async def test_recap_events_block_survives_db_error(monkeypatch):
    """A DB hiccup while building the block must degrade to '' (chat-only
    recap), never raise — the recap should still go out."""
    cog = AICog(bot=None)

    async def _boom(*a, **kw):
        raise RuntimeError("db down")
    # Patch every loader the block touches — it imports them function-locally,
    # so patching the source modules is enough.
    import src.persistence as _p
    import src.persistence.history as _h
    monkeypatch.setattr(_p, "load_notable_events_today", _boom)
    monkeypatch.setattr(_h, "load_crime_history", _boom)
    monkeypatch.setattr(_h, "load_gambling_history", _boom)

    block = await cog._build_recap_events_block(42, _ct_today())
    assert block == ""
