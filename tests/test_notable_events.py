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

    # _recap_display_name resolves ids via bot.get_guild(...).get_member(...).
    winner = FakeMember(uid=1001, display_name="BigWinner")
    thief = FakeMember(uid=1003, display_name="SneakyThief")
    guild = FakeGuild(gid=42)
    guild.members = [winner, thief]

    class _Bot:
        def get_guild(self, gid):
            return guild if gid == 42 else None

    cog = AICog(bot=_Bot())
    block = await cog._build_recap_events_block(42, _ct_today())

    assert "BigWinner won 8,000 coins gambling" in block
    assert "SneakyThief pulled off 4,200 coins in crime" in block
    # The net loser is not mentioned.
    assert "1002" not in block


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
