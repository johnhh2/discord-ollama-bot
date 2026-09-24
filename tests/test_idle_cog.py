"""!idle (src/cogs/idle_cog.py): joining, the tick, the one-hour login grace,
feed threads, and the admin commands."""
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import src.state as _state
import src.persistence as _persistence
import src.cogs.idle_cog as _idle_cog
from src import idlerpg as rpg
from src.cogs.idle_cog import IdleCog
from src.cogs.settings_cog import SettingsCog
from src.guild_config import get_guild_cfg
from src.idle_hub import IdleHub, items_for
from src.idle_map import MAP_FILENAME
from src.panel import _ItemButton, _ItemSelect, _PageSelect, open_panel
from src.settings_views import FormModal
from src.permissions import get_command_perm

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember, FakeMessage, FakeTextChannel, FakeThread

pytestmark = pytest.mark.asyncio

GID, IDLE_CH = 1, 500
# Inside Velvragh's market ring (60 squares) but outside its centre (15), so
# `!idle shop` is open and nothing trades on its own.
MARKET = {"x": 365, "y": 270}
# Open country, clear of every market ring once the wrap is counted.
WILDS = (5, 255)
ALICE, BOB, ADMIN = 11, 12, 13


class _Rng:
    """Nothing random ever happens: no events, no battles below level 25, the lowest rolls."""
    def random(self):
        return 0.999

    def randint(self, low, high):
        return low

    def randrange(self, n):
        return n - 1

    def choice(self, seq):
        return seq[0]

    def sample(self, seq, k):
        return list(seq)[:k]


class _WalkRng(_Rng):
    """Every wander step is +1 on both axes."""
    def randint(self, low, high):
        return high


class _StillRng(_Rng):
    """Nobody wanders: a character placed in a town stays there through a tick."""
    def randint(self, low, high):
        return 0 if (low, high) == (-1, 1) else low


def _sent_text(dest) -> str:
    return "\n".join(call.args[0] for call in dest.send.call_args_list if call.args)


def _world(*, presences: bool = False, channel: bool = True):
    guild = FakeGuild(gid=GID)
    guild.members = [FakeMember(ALICE, "alice"), FakeMember(BOB, "bob"), FakeMember(ADMIN, "boss", administrator=True)]
    for member in guild.members:
        member.status = discord.Status.online
    guild.fetch_channel = AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone"))
    idle = FakeTextChannel(ch_id=IDLE_CH, name="idle")
    made = []

    async def _create_thread(name, **kwargs):
        thread = FakeThread(thread_id=900 + len(made), name=name, parent_id=IDLE_CH)
        thread.guild = guild
        made.append(thread)
        guild.threads.append(thread)
        return thread
    idle.create_thread = AsyncMock(side_effect=_create_thread)
    guild.channels.append(idle)
    if channel:
        get_guild_cfg(GID)["idle_channel"] = IDLE_CH
    bot = SimpleNamespace(intents=SimpleNamespace(presences=presences), get_guild=lambda gid: guild if gid == GID else None)
    cog = IdleCog(bot)
    cog.rng = _Rng()
    # The standings board would otherwise post on the first tick of every
    # test; the board's own tests clear this to let it through.
    cog._board_at[GID] = time.monotonic()
    return cog, guild, idle


def _ctx(guild, uid: int = ALICE, channel=None) -> FakeCtx:
    return FakeCtx(author=guild.get_member(uid), guild=guild, channel=channel, command_name="idle")


def _spawn(uid: int = ALICE, *, level: int = 0, left: int = 1000, **over) -> dict:
    now = int(time.time())
    char = rpg.new_character("Bard", now - 100_000)
    char.update({"level": level, "next_level_at": now + left, "last_seen": now, **over})
    _state.idle_characters.setdefault(GID, {})[uid] = char
    return char


def _sent(dest) -> str:
    return _sent_text(dest)


def _message(guild, uid, content, channel):
    msg = FakeMessage(content=content, author=guild.get_member(uid), channel=channel)
    msg.guild = guild
    return msg


# ── !idle join ───────────────────────────────────────────────────────────────

async def test_join_without_a_class_asks_for_one():
    cog, guild, idle = _world()
    ctx = _ctx(guild)
    await cog.cmd_join.callback(cog, ctx)
    assert ctx.sent_embeds[-1].title == "❌ Pick a Class"
    assert "!idle join <class>" in ctx.sent_embeds[-1].description
    assert _state.idle_characters.get(GID, {}) == {}
    idle.create_thread.assert_not_called()


@pytest.mark.parametrize("bad", ["x" * 31, "**bold**", "@everyone", "<@123>"])
async def test_join_rejects_an_unprintable_class(bad):
    cog, guild, _idle = _world()
    ctx = _ctx(guild)
    await cog.cmd_join.callback(cog, ctx, class_name=bad)
    assert ctx.sent_embeds[-1].title == "❌ Pick a Class"
    assert _state.idle_characters.get(GID, {}) == {}


async def test_join_is_refused_while_no_channel_is_set():
    cog, guild, _idle = _world(channel=False)
    ctx = _ctx(guild)
    await cog.cmd_join.callback(cog, ctx, class_name="Bard")
    assert "Is Off" in ctx.sent_embeds[-1].title
    assert _state.idle_characters.get(GID, {}) == {}


async def test_join_makes_a_character_a_thread_and_an_announcement():
    cog, guild, idle = _world()
    ctx = _ctx(guild)
    await cog.cmd_join.callback(cog, ctx, class_name="  Drunken   Bard ")

    char = _state.idle_characters[GID][ALICE]
    assert char["class"] == "Drunken Bard" and char["level"] == 0 and not rpg.is_paused(char)
    thread = guild.threads[0]
    assert char["thread_id"] == thread.id and "alice" in thread.name
    thread.add_user.assert_awaited_once()
    assert thread.mention in ctx.sent_embeds[-1].description
    assert "joined the realm" in _sent(idle)

    again = _ctx(guild)
    await cog.cmd_join.callback(cog, again, class_name="Wizard")
    assert again.sent_embeds[-1].title == "❌ Already Adventuring"
    assert char["class"] == "Drunken Bard"


async def test_concurrent_joins_make_one_character(monkeypatch):
    cog, guild, idle = _world()

    async def _yielding_save(*args):
        await asyncio.sleep(0)
    monkeypatch.setattr(_persistence, "save_idle_character", _yielding_save)

    await asyncio.gather(
        cog.cmd_join.callback(cog, _ctx(guild), class_name="Bard"),
        cog.cmd_join.callback(cog, _ctx(guild), class_name="Wizard"),
    )
    assert _state.idle_characters[GID][ALICE]["class"] == "Bard"
    assert idle.create_thread.await_count == 1


async def test_join_survives_a_channel_that_cannot_host_threads():
    cog, guild, idle = _world()
    idle.create_thread = AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="no"), "no"))
    ctx = _ctx(guild)
    await cog.cmd_join.callback(cog, ctx, class_name="Bard")
    assert _state.idle_characters[GID][ALICE]["thread_id"] is None
    assert "Create Public Threads" in ctx.sent_embeds[-1].description


# ── the tick ─────────────────────────────────────────────────────────────────

async def test_tick_levels_up_finds_an_item_and_posts_once_per_destination(monkeypatch):
    cog, guild, idle = _world()
    char = _spawn(left=-10)
    now = int(time.time())
    saved = []
    monkeypatch.setattr(_persistence, "save_idle_character", AsyncMock(side_effect=lambda gid, uid: saved.append((gid, uid))))

    await cog.tick(now)

    assert char["level"] == 1 and char["next_level_at"] > now
    assert len(char["items"]) == 1
    idle.send.assert_not_called()                 # an ordinary level is the player's own news
    thread = guild.threads[0]                     # made lazily: the character had none
    story = [call.args[0] for call in thread.send.call_args_list]
    assert len(story) == 2                        # the opening line, then one batched post
    assert "reached **level 1**" in story[1] and "their first ring" in story[1]
    assert "their first ring" not in _sent(idle)  # item finds stay in the feed
    assert (GID, ALICE) in saved


async def test_every_feed_post_is_silent_and_pings_nobody():
    cog, guild, idle = _world()
    _spawn(ALICE, left=-10), _spawn(BOB, left=5000)
    await cog.cmd_duel.callback(cog, _ctx(guild), member=guild.get_member(BOB))
    await cog.tick()
    calls = idle.send.call_args_list + [c for t in guild.threads for c in t.send.call_args_list]
    assert len(calls) >= 4
    for call in calls:
        assert call.kwargs["silent"] is True
        assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}   # only a quest start may mention


async def test_quest_start_mentions_the_questers_silently_and_only_in_the_channel():
    cog, guild, idle = _world()
    _spawn(ALICE, level=45, left=50_000), _spawn(BOB, level=45, left=50_000)
    _spawn(ADMIN, level=3, left=50_000)

    await cog.tick()

    assert _state.idle_quests[GID]["members"] == [ALICE, BOB]
    call = idle.send.call_args
    assert f"<@{ALICE}>" in call.args[0] and f"<@{BOB}>" in call.args[0]
    assert call.kwargs["silent"] is True
    assert sorted(call.kwargs["allowed_mentions"].to_dict()["users"]) == [ALICE, BOB]
    for thread in guild.threads:
        for post in thread.send.call_args_list:
            assert post.kwargs["allowed_mentions"].to_dict() == {"parse": []}


async def test_tick_catches_up_several_levels_in_one_post():
    cog, guild, idle = _world()
    char = _spawn(level=8, left=-(rpg.ttl(9) + rpg.ttl(10) + 5))
    await cog.tick()
    assert char["level"] == 11
    assert idle.send.await_count == 1
    assert "level 10" in _sent(idle) and "level 9" not in _sent(idle) and "level 11" not in _sent(idle)
    assert "The next one takes" not in _sent(idle)      # the room doesn't need a countdown
    feed = _sent(guild.threads[0])
    assert all(f"level {n}" in feed for n in (9, 10, 11))
    assert "The next one takes" in feed                 # the player's own thread does


async def test_tick_does_nothing_with_the_game_off_but_freeze_clocks():
    cog, guild, idle = _world(channel=False)
    char = _spawn(left=-10)
    now = int(time.time())
    await cog.tick(now)
    assert char["level"] == 0 and rpg.is_paused(char) and char["remaining"] == 0
    idle.send.assert_not_called()

    get_guild_cfg(GID)["idle_channel"] = IDLE_CH
    await cog.tick(now + 600)
    assert not rpg.is_paused(char) and char["level"] == 1   # the clock woke with 0s left


async def test_thread_title_follows_the_level_but_not_faster_than_the_rename_budget():
    cog, guild, _idle = _world()
    char = _spawn(left=-10)
    now = int(time.time())
    await cog.tick(now)                      # creates the thread, already titled Lv 1
    thread = guild.threads[0]
    thread.edit.assert_not_called()

    char["next_level_at"] = now              # level 2 on the next tick
    await cog.tick(now + 60)
    thread.edit.assert_awaited_once()
    assert "Lv 2" in thread.edit.call_args.kwargs["name"]

    thread.name = thread.edit.call_args.kwargs["name"]
    char["next_level_at"] = now
    await cog.tick(now + 120)                # level 3, but the last rename was seconds ago
    assert thread.edit.await_count == 1
    assert (GID, ALICE) in cog._rename_due


async def test_deleted_thread_is_remade_on_the_next_post():
    cog, guild, idle = _world()
    char = _spawn(left=-10, thread_id=4242)  # not in the cache, and fetch_channel says NotFound
    await cog.tick()
    assert char["thread_id"] == guild.threads[0].id != 4242


async def test_a_transient_fetch_error_skips_the_post_without_a_second_thread():
    cog, guild, idle = _world()
    guild.fetch_channel = AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x"))
    char = _spawn(left=-10, thread_id=4242)
    await cog.tick()
    assert char["thread_id"] == 4242
    idle.create_thread.assert_not_called()


# ── logged in: the one-hour grace ────────────────────────────────────────────

async def test_offline_past_the_hour_pauses_retroactively():
    cog, guild, _idle = _world(presences=True)
    guild.get_member(ALICE).status = discord.Status.offline
    now = int(time.time())
    # Last seen two hours ago, so the grace ran out an hour ago; the level-up
    # came due half an hour after that.
    char = _spawn(left=-1800, last_seen=now - 7200)

    await cog.tick(now)

    assert char["level"] == 0 and rpg.is_paused(char)
    assert char["remaining"] == 1800         # frozen at last_seen + 1h, not at the tick


async def test_levels_earned_inside_the_grace_hour_still_count():
    cog, guild, _idle = _world(presences=True)
    guild.get_member(ALICE).status = discord.Status.offline
    now = int(time.time())
    char = _spawn(level=20, left=-6600, last_seen=now - 7200)   # due 10 minutes after they left
    await cog.tick(now)
    assert char["level"] == 21 and rpg.is_paused(char)
    assert char["remaining"] == rpg.ttl(21) - 3000   # 50 of the grace hour's minutes went into level 22


async def test_a_short_offline_blip_changes_nothing():
    cog, guild, _idle = _world(presences=True)
    guild.get_member(ALICE).status = discord.Status.offline
    now = int(time.time())
    char = _spawn(left=5000, last_seen=now - 1800)
    await cog.tick(now)
    assert not rpg.is_paused(char) and char["next_level_at"] == now + 5000


async def test_coming_online_resumes_at_once_and_going_offline_starts_the_hour():
    cog, guild, _idle = _world(presences=True)
    now = int(time.time())
    char = _spawn(last_seen=now - 9000)
    rpg.pause(char, now - 5400)
    left = char["remaining"]
    member = guild.get_member(ALICE)
    member.guild = guild
    offline = SimpleNamespace(status=discord.Status.offline)

    await cog.on_presence_update(offline, member)

    assert not rpg.is_paused(char)
    assert abs(char["next_level_at"] - (int(time.time()) + left)) <= 2
    assert "is back" in cog._pending[GID][0].text

    char["last_seen"] = 0
    gone = SimpleNamespace(status=discord.Status.offline, guild=guild, id=ALICE)
    await cog.on_presence_update(member, gone)
    assert abs(char["last_seen"] - time.time()) <= 2 and not rpg.is_paused(char)


async def test_idle_and_dnd_count_as_online():
    cog, guild, _idle = _world(presences=True)
    now = int(time.time())
    for status in (discord.Status.idle, discord.Status.dnd):
        guild.get_member(ALICE).status = status
        char = _spawn(left=5000, last_seen=now - 7200)
        await cog.tick(now)
        assert not rpg.is_paused(char) and char["last_seen"] == now


async def test_without_the_presence_intent_everyone_counts_as_online():
    cog, guild, _idle = _world(presences=False)
    guild.get_member(ALICE).status = discord.Status.offline
    now = int(time.time())
    char = _spawn(left=5000, last_seen=now - 7200)
    await cog.tick(now)
    assert not rpg.is_paused(char)


async def test_a_message_anywhere_keeps_an_invisible_player_logged_in():
    cog, guild, _idle = _world(presences=True)
    guild.get_member(ALICE).status = discord.Status.offline
    char = _spawn(last_seen=0)
    rpg.pause(char, int(time.time()))
    elsewhere = FakeTextChannel(ch_id=77)

    await cog.on_message(_message(guild, ALICE, "hello", elsewhere))

    assert not rpg.is_paused(char) and char["penalty_total"] == 0
    assert abs(char["last_seen"] - time.time()) <= 2


# ── talking is free ──────────────────────────────────────────────────────────

async def test_talking_in_the_idle_channel_or_a_feed_thread_costs_nothing():
    cog, guild, idle = _world()
    char = _spawn(level=10, left=5000)
    due = char["next_level_at"]
    await cog.on_message(_message(guild, ALICE, "x" * 400, idle))
    await cog.on_message(_message(guild, ALICE, "hello", FakeThread(thread_id=950, parent_id=IDLE_CH)))
    await cog.tick()
    assert char["next_level_at"] == due and char["penalty_total"] == 0
    idle.send.assert_not_called()


async def test_a_talking_quester_leaves_the_quest_running():
    cog, guild, idle = _world()
    _spawn(ALICE, level=45, left=50_000), _spawn(BOB, level=45, left=50_000)
    now = int(time.time())
    _state.idle_quests[GID] = {**rpg.new_quest(), "members": [ALICE, BOB], "description": "wait", "kind": "vigil", "ends_at": now + 9000}
    await cog.on_message(_message(guild, ALICE, "still here", idle))
    await cog.tick(now)
    assert _state.idle_quests[GID]["ends_at"] == now + 9000


async def test_silenced_users_and_bots_are_not_stamped_as_seen():
    cog, guild, idle = _world()
    char = _spawn(last_seen=5)
    guild.get_member(ALICE).bot = True
    await cog.on_message(_message(guild, ALICE, "beep", idle))
    guild.get_member(ALICE).bot = False
    _state.blocklist[(GID, ALICE)] = {"reason": None, "banned_by": 1, "banned_at": None}
    await cog.on_message(_message(guild, ALICE, "banned", idle))
    assert char["last_seen"] == 5


async def test_leaving_your_own_thread_is_a_penalty_but_leaving_the_server_is_not_charged_twice():
    cog, guild, _idle = _world()
    char = _spawn(left=5000, thread_id=900)
    thread = FakeThread(thread_id=900, parent_id=IDLE_CH)
    thread.guild = guild

    await cog.on_thread_member_remove(SimpleNamespace(id=ALICE, thread=thread))
    assert char["penalty_total"] == rpg.PEN_PART

    member = guild.get_member(ALICE)
    member.guild = guild
    guild.members.remove(member)
    await cog.on_thread_member_remove(SimpleNamespace(id=ALICE, thread=thread))
    assert char["penalty_total"] == rpg.PEN_PART
    await cog.on_member_remove(member)
    assert char["penalty_total"] == rpg.PEN_PART + rpg.PEN_QUIT


# ── commands ─────────────────────────────────────────────────────────────────

async def test_bare_idle_opens_the_panel_pitching_to_strangers_and_showing_players_their_sheet(monkeypatch):
    cog, guild, _idle = _world()
    ctx = _ctx(guild)
    opened = []

    async def _open(ctx, panel, **kwargs):
        opened.append(panel)
    monkeypatch.setattr(_idle_cog, "open_panel", _open)
    await cog.cmd_idle.callback(cog, ctx)
    assert isinstance(opened[-1], IdleHub) and "**Join** below" in opened[-1].embed().description

    char = _spawn(level=7, last_seen=0)
    await cog.cmd_idle.callback(cog, ctx)
    assert "Lv 7 Bard" in opened[-1].embed().title
    assert char["last_seen"] > 0                     # opening the panel counts as being seen

    # Where no idle channel is set the game is off: no panel, just that.
    off_cog, off_guild, _ = _world(channel=False)
    get_guild_cfg(GID).pop("idle_channel", None)  # the first _world() above set it
    off_ctx = _ctx(off_guild)
    await off_cog.cmd_idle.callback(off_cog, off_ctx)
    assert off_ctx.sent_embeds[-1].title == "💤 Idle RPG Is Off"


async def test_rules_stay_short_and_the_detail_lives_in_topics():
    cog, guild, _idle = _world()
    ctx = _ctx(guild)
    await cog.cmd_rules.callback(cog, ctx)
    card = ctx.sent_embeds[-1].description
    assert len(card) < 700 and card.count("\n") <= 12
    assert "**Start:** `!idle join <class>`" in card                 # a stranger is told how to begin

    _spawn(ALICE, level=3, claimed=False, **{"class": rpg.UNCLAIMED_CLASS})
    await cog.cmd_rules.callback(cog, ctx)
    assert "level 3 Adventurer is already adventuring in your name" in ctx.sent_embeds[-1].description
    _state.idle_characters[GID][ALICE]["claimed"] = True
    await cog.cmd_rules.callback(cog, ctx)
    # A player sees it too — they are the one who passes this card on.
    assert "**New here?** `!idle join <class>`" in ctx.sent_embeds[-1].description
    assert "!idle rules <levels|battles|monsters|map|gold|luck|world|hunts|titles|alignment|quests|prestige>" in card
    assert "talk" not in card.lower()

    await cog.cmd_rules.callback(cog, ctx, "Quests")
    assert ctx.sent_embeds[-1].title == "📖 Idle RPG — Quests"
    await cog.cmd_rules.callback(cog, ctx, "nonsense")
    assert ctx.sent_embeds[-1].description.endswith(card.split("\n\n")[-1])   # the card again, start line aside


async def test_status_of_a_paused_character_says_why():
    cog, guild, _idle = _world()
    char = _spawn(BOB)
    rpg.pause(char, int(time.time()))
    ctx = _ctx(guild)
    await cog.cmd_status.callback(cog, ctx, member=guild.get_member(BOB))
    assert "paused" in ctx.sent_embeds[-1].description and "last seen" in ctx.sent_embeds[-1].description
    assert rpg.is_paused(char)               # looking at someone doesn't wake them


async def test_top_ranks_prestige_then_level():
    cog, guild, _idle = _world()
    _spawn(ALICE, level=30)
    _spawn(BOB, level=2, prestige=1)
    ctx = _ctx(guild)
    await cog.cmd_top.callback(cog, ctx)
    body = ctx.sent_embeds[-1].description
    assert body.index("bob") < body.index("alice")


async def test_align_typed_sets_it_once_a_day():
    cog, guild, _idle = _world()
    char = _spawn()
    ctx = _ctx(guild)
    await cog.cmd_align.callback(cog, ctx, alignment="Chaotic Good")
    assert (char["law"], char["moral"]) == ("chaotic", "good")

    await cog.cmd_align.callback(cog, ctx, alignment="evil")
    assert char["moral"] == "good" and ctx.sent_embeds[-1].title == "❌ Alignment"


async def test_align_bare_opens_the_dropdown(monkeypatch):
    cog, guild, _idle = _world()
    char = _spawn()
    seen = {}

    async def _pick(ctx, *, options, **kwargs):
        seen["options"] = options
        return ["lawful evil"]
    monkeypatch.setattr(_idle_cog, "pick_from_list", _pick)

    await cog.cmd_align.callback(cog, _ctx(guild))
    assert len(seen["options"]) == 9 and ("True Neutral", "neutral neutral") in seen["options"]
    assert (char["law"], char["moral"]) == ("lawful", "evil")


async def test_duel_is_once_a_day_and_not_against_the_sleeping():
    cog, guild, idle = _world()
    alice = _spawn(ALICE, left=10_000)
    bob = _spawn(BOB, left=10_000)
    ctx = _ctx(guild)

    await cog.cmd_duel.callback(cog, ctx, member=guild.get_member(BOB))
    assert ctx.sent_embeds[-1].title == "🤺 Duel"
    assert alice["next_level_at"] != bob["next_level_at"]
    idle.send.assert_not_called()                            # the room isn't told

    await cog.cmd_duel.callback(cog, ctx, member=guild.get_member(BOB))
    assert "today's duel" in ctx.sent_embeds[-1].description

    alice["duel_day"] = None
    rpg.pause(bob, int(time.time()))
    await cog.cmd_duel.callback(cog, ctx, member=guild.get_member(BOB))
    assert "offline" in ctx.sent_embeds[-1].description and alice["duel_day"] is None


async def test_a_duel_plays_out_round_by_round_in_both_feeds_and_not_in_the_room(monkeypatch):
    cog, guild, idle = _world()
    _spawn(ALICE, left=50_000, items={"ring": {"level": 40, "name": None}})
    _spawn(BOB, left=50_000, items={"ring": {"level": 30, "name": None}})
    beats = []
    real_sleep = _idle_cog.asyncio.sleep

    async def _counted(secs):
        beats.append(secs)
        await real_sleep(0)
    monkeypatch.setattr(_idle_cog.asyncio, "sleep", _counted)

    ctx = _ctx(guild)
    await cog.cmd_duel.callback(cog, ctx, member=guild.get_member(BOB))

    feeds = {t.name.split(" ")[0]: _sent(t) for t in guild.threads}
    assert set(feeds) == {"alice", "bob"}                    # both duellists watch it
    for who, feed in feeds.items():
        assert "squares up to" in feed, who
        assert "**Round 1**" in feed and "hits for" in feed, who
        assert "wins." in feed, who                          # and the result lands after it
    assert beats and len(beats) == feeds["alice"].count("**Round ")   # a beat before each round

    # Not the result either: a duel is the two players' business, up to
    # twice a day each, and it says little to anyone who didn't watch it.
    idle.send.assert_not_called()
    assert ctx.sent_embeds[-1].title == "🤺 Duel"            # the challenger's reply still carries it


async def test_duel_run_in_the_idle_channel_is_not_posted_twice():
    cog, guild, idle = _world()
    _spawn(ALICE), _spawn(BOB)
    ctx = _ctx(guild, channel=idle)
    await cog.cmd_duel.callback(cog, ctx, member=guild.get_member(BOB))
    idle.send.assert_not_called()


async def test_prestige_needs_level_sixty_and_starts_over():
    cog, guild, _idle = _world()
    char = _spawn(level=59, items={"ring": {"level": 80, "name": None}})
    ctx = _ctx(guild)
    await cog.cmd_prestige.callback(cog, ctx)
    assert char["level"] == 59 and ctx.sent_embeds[-1].title == "❌ Prestige"

    char["level"] = 60
    await cog.cmd_prestige.callback(cog, ctx)
    assert (char["level"], char["prestige"], char["items"]) == (0, 1, {})


async def test_leave_deletes_the_character_and_archives_the_thread():
    cog, guild, _idle = _world()
    thread = FakeThread(thread_id=900, parent_id=IDLE_CH)
    guild.threads.append(thread)
    _spawn(thread_id=900)
    await cog.cmd_leave.callback(cog, _ctx(guild))
    assert ALICE not in _state.idle_characters[GID]
    thread.edit.assert_awaited_once_with(archived=True)


async def test_leave_declined_keeps_the_character(monkeypatch):
    cog, guild, _idle = _world()
    _spawn()

    async def _no(*args, **kwargs):
        return False
    monkeypatch.setattr(_idle_cog, "confirm_prompt", _no)
    await cog.cmd_leave.callback(cog, _ctx(guild))
    assert ALICE in _state.idle_characters[GID]


# ── the map ──────────────────────────────────────────────────────────────────

async def test_the_tick_walks_everyone_online_a_minute_and_leaves_the_paused_where_they_stand():
    cog, guild, _idle = _world()
    cog.rng = _WalkRng()
    walker = _spawn(ALICE, left=50_000, x=100, y=100)
    sleeper = _spawn(BOB, left=50_000, x=300, y=300)
    rpg.pause(sleeper, int(time.time()))
    guild.get_member(BOB).status = discord.Status.offline
    cog.bot.intents.presences = True
    sleeper["last_seen"] = 0

    await cog.tick()

    assert (walker["x"], walker["y"]) == (160, 160)      # +1 a second, sixty seconds
    assert (sleeper["x"], sleeper["y"]) == (300, 300)


async def test_the_map_draws_only_the_viewer_s_own_route(monkeypatch):
    cog, guild, _idle = _world()
    drawn = []

    def _render(players, **kwargs):
        drawn.append(kwargs["routes"])
        return b"PNG"

    monkeypatch.setattr(_idle_cog, "render_map", _render)
    alice = _spawn(ALICE, x=10, y=20, travel_to="Velvragh")
    _spawn(BOB, x=100, y=100, travel_to="Denmark")

    await cog.cmd_map.callback(cog, _ctx(guild))
    assert drawn[-1] == [(10, 20, *rpg.LANDMARKS["Velvragh"])]
    # Bob's sheet: neither his route (his to know) nor Alice's (not her picture).
    await cog.cmd_status.callback(cog, _ctx(guild), member=guild.get_member(BOB))
    assert drawn[-1] == []

    # A hunt still walking to its country is a route too…
    alice["travel_to"], alice["hunt_mob"], alice["hunt_count"] = None, "Crab", 3
    alice["hunt_x"], alice["hunt_y"] = 0, 60
    await cog.cmd_status.callback(cog, _ctx(guild))
    assert drawn[-1] == [(10, 20, 0, 60)]
    # …and a journey quest's current waypoint outranks it, as it does in move_players.
    _state.idle_quests[GID] = {**rpg.new_quest(), "members": [ALICE], "description": "walk", "kind": "journey",
                               "stage": 1, "p1": [35, 40], "p2": [410, 80]}
    await cog.cmd_quest.callback(cog, _ctx(guild))
    assert drawn[-1] == [(10, 20, 35, 40)]
    await cog.cmd_quest.callback(cog, _ctx(guild, BOB))
    assert drawn[-1] == []                    # Bob is highlighted with the party, but it isn't his route


async def test_a_character_from_before_the_map_is_placed_on_the_first_tick():
    cog, guild, _idle = _world()
    char = _spawn(left=50_000)
    assert char["x"] is None
    await cog.tick()
    assert 0 <= char["x"] <= rpg.MAP_SIZE and 0 <= char["y"] <= rpg.MAP_SIZE


async def test_status_map_and_quest_carry_the_map_image():
    cog, guild, _idle = _world()
    _spawn(ALICE, x=10, y=20), _spawn(BOB, x=35, y=40)
    _state.idle_quests[GID] = {**rpg.new_quest(), "members": [BOB], "description": "walk", "kind": "journey", "p1": [35, 40], "p2": [410, 80]}

    # (The bare `!idle` panel carries it too — see the panel tests below.)
    for command in (cog.cmd_status, cog.cmd_quest):
        ctx = _ctx(guild)
        await command.callback(cog, ctx)
        sent = ctx.send_mock.call_args.kwargs
        assert sent["file"].filename == _idle_cog.MAP_FILENAME
        assert ctx.sent_embeds[-1].image.url == f"attachment://{_idle_cog.MAP_FILENAME}"
    # `!idle map` is the picture alone — an embed would shrink it.
    ctx = _ctx(guild)
    await cog.cmd_map.callback(cog, ctx)
    sent = ctx.send_mock.call_args.kwargs
    assert sent["file"].filename == _idle_cog.MAP_FILENAME and sent.get("embed") is None and not ctx.sent_embeds

    ctx = _ctx(guild)
    await cog.cmd_status.callback(cog, ctx, member=guild.get_member(BOB))
    assert "**Position:** [35, 40] — at Denmark" in ctx.sent_embeds[-1].description
    await cog.cmd_quest.callback(cog, ctx)
    assert "Waypoint 1 of 2: Denmark [35, 40]" in ctx.sent_embeds[-1].description


async def test_a_journey_is_announced_with_the_map_and_a_vigil_is_not():
    for first, expect_map in ((rpg._JOURNEYS[0], True), (rpg._VIGILS[0], False)):
        _state.idle_characters.clear()
        _state.idle_quests.clear()
        cog, guild, idle = _world()

        class _Pick(_Rng):
            def choice(self, seq):
                return first if first in seq else seq[0]
        cog.rng = _Pick()
        _spawn(ALICE, level=45, left=50_000), _spawn(BOB, level=45, left=50_000)

        await cog.tick()

        files = [c.kwargs["file"] for c in idle.send.call_args_list if c.kwargs.get("file")]
        assert bool(files) is expect_map
        assert all(c.kwargs["silent"] is True for c in idle.send.call_args_list)
        if expect_map:
            assert _state.idle_quests[GID]["kind"] == "journey"
            assert "must first reach Denmark [35, 40]" in _sent_text(idle)


async def test_profile_mentions_the_idle_character(monkeypatch):
    import src.cogs.profile_cog as _profile_cog
    cog, guild, _idle = _world()
    _spawn(ALICE, level=9, x=5, y=6)
    _state.leveling[str(GID)] = {str(ALICE): {"level": 4, "xp": 1234}}
    monkeypatch.setattr(_profile_cog, "load_lottery", AsyncMock(return_value={}))
    monkeypatch.setattr(_profile_cog, "load_records", AsyncMock(return_value={}))
    member = guild.get_member(ALICE)
    member.display_avatar = SimpleNamespace(url="https://example.invalid/a.png")
    profile = _profile_cog.ProfileCog(None)
    ctx = _ctx(guild)
    await profile.cmd_profile.callback(profile, ctx)
    shown = ctx.sent_embeds[-1].description.split("\n")
    order = [next(i for i, line in enumerate(shown) if line.startswith(mark)) for mark in ("🏺 Artifacts", "📊 Level", "⚔️ Idle RPG")]
    assert order == sorted(order) and order[2] == order[1] + 1 == order[0] + 2
    assert "⚔️ Idle RPG: **Lv 9 Bard** · [5, 6]" in shown[order[2]]


# ── voice bonus ──────────────────────────────────────────────────────────────

def _join_voice(guild, uid, channel=None, *, company=(ADMIN,)):
    """Put `uid` in a voice channel alongside `company` (other members' ids)."""
    channel = channel or SimpleNamespace(id=321)
    channel.members = [guild.get_member(u) for u in (uid, *company)]
    guild.get_member(uid).voice = SimpleNamespace(channel=channel)


async def test_a_minute_in_voice_takes_six_seconds_off_the_clock_and_only_for_those_in_it():
    cog, guild, _idle = _world()
    cog.rng = _StillRng()
    talker, lurker = _spawn(ALICE, left=50_000, **MARKET), _spawn(BOB, left=50_000, **MARKET)
    _join_voice(guild, ALICE)
    due_talker, due_lurker = talker["next_level_at"], lurker["next_level_at"]

    await cog.tick()

    assert talker["next_level_at"] == due_talker - 6 and lurker["next_level_at"] == due_lurker


async def test_gold_earned_by_the_tick_is_a_tenth_larger_in_voice_and_the_feed_says_so():
    cog, guild, _idle = _world()
    cog.rng = _StillRng()
    talker, lurker = _spawn(ALICE, level=6, left=-10, **MARKET), _spawn(BOB, level=6, left=-10, **MARKET)
    _join_voice(guild, ALICE)

    await cog.tick()

    assert (talker["gold"], lurker["gold"]) == (77, 70)          # level 7 pays 70
    feeds = {t.name.split(" ")[0]: _sent(t) for t in guild.threads}
    assert "+10% this minute: +7 gold" in feeds["alice"] and "this minute" not in feeds["bob"]


async def test_the_afk_channel_and_a_paused_clock_earn_nothing_and_voice_counts_as_being_seen():
    cog, guild, _idle = _world(presences=True)
    cog.rng = _StillRng()
    afk = SimpleNamespace(id=999)
    guild.afk_channel = afk
    parked = _spawn(ALICE, left=50_000, **MARKET)
    _join_voice(guild, ALICE, afk)
    due = parked["next_level_at"]
    await cog.tick()
    assert parked["next_level_at"] == due

    # On a phone in a call: presence says offline, the voice channel says otherwise.
    now = int(time.time())
    caller = _spawn(BOB, left=50_000, last_seen=now - 7200, **MARKET)
    guild.get_member(BOB).status = discord.Status.offline
    _join_voice(guild, BOB)
    await cog.tick(now)
    assert not rpg.is_paused(caller) and caller["last_seen"] == now


async def test_alone_in_voice_or_with_only_a_bot_for_company_earns_nothing():
    cog, guild, _idle = _world()
    cog.rng = _StillRng()
    loner = _spawn(ALICE, left=50_000, **MARKET)
    due = loner["next_level_at"]

    _join_voice(guild, ALICE, company=())
    await cog.tick()
    assert loner["next_level_at"] == due

    guild.get_member(BOB).bot = True                       # a music bot is not company
    _join_voice(guild, ALICE, company=(BOB,))
    await cog.tick()
    assert loner["next_level_at"] == due

    _join_voice(guild, ALICE, company=(BOB, ADMIN))       # …but one real person is
    await cog.tick()
    assert loner["next_level_at"] == due - 6


async def test_status_shows_the_voice_bonus_while_it_applies():
    cog, guild, _idle = _world()
    _spawn(x=1, y=1)
    ctx = _ctx(guild)
    await cog.cmd_status.callback(cog, ctx)
    assert "In voice" not in ctx.sent_embeds[-1].description
    _join_voice(guild, ALICE)
    await cog.cmd_status.callback(cog, ctx)
    assert "🎙️ **In voice:** levelling and gold 10% faster" in ctx.sent_embeds[-1].description


# ── monsters ─────────────────────────────────────────────────────────────────

async def test_the_tick_sends_wild_characters_into_fights_and_keeps_them_in_the_feed(monkeypatch):
    cog, guild, idle = _world()
    wild = _spawn(ALICE, level=10, left=50_000, x=WILDS[0], y=WILDS[1])
    townie = _spawn(BOB, level=10, left=50_000, **MARKET)
    met = []

    def _encounter(uid, char, rng, name, now, effect=None):
        met.append(uid)
        return [rpg.Note((uid,), f"🗡️ {name(uid)} killed a Normal Rat.")]
    monkeypatch.setattr(rpg, "mob_encounter", _encounter)

    class _Eager(_StillRng):
        def random(self):
            return 0.0
    cog.rng = _Eager()
    monkeypatch.setattr(rpg, "random_events", lambda *args: [])
    monkeypatch.setattr(rpg, "team_battle", lambda *args: [])
    monkeypatch.setattr(rpg, "tick_world", lambda *args: [])

    await cog.tick()

    assert met == [ALICE, BOB]                 # the cog asks for everyone; the rules keep towns safe
    assert "killed a Normal Rat" in _sent(guild.threads[0])
    idle.send.assert_not_called()
    assert wild["x"] == WILDS[0] and townie["x"] == MARKET["x"]


async def test_a_real_monster_fight_reaches_the_feed_through_a_tick():
    """No stub for mob_encounter: the whole path, cog to rules to thread."""
    cog, guild, idle = _world()

    class _Hunting(_StillRng):
        def random(self):
            return 0.0          # every per-tick chance hits, including the monster roll
    cog.rng = _Hunting()
    char = _spawn(level=10, left=50_000, gold=500, x=WILDS[0], y=WILDS[1],   # out in the wilds
                  items={"ring": {"level": 20, "name": None}})

    await cog.tick()

    feed = _sent(guild.threads[0])
    assert "🗡️" in feed or "☠️" in feed, feed
    assert char["mob_kills"] or char["mob_deaths"] or "fled" in feed


async def test_the_tick_mends_a_wounded_character_and_status_shows_the_bar():
    cog, guild, _idle = _world()
    cog.rng = _StillRng()
    char = _spawn(level=10, left=50_000, x=WILDS[0], y=WILDS[1], items={"ring": {"level": 25, "name": None}})
    full = rpg.max_hp(char)
    char["hp"] = full // 2

    await cog.tick()
    assert rpg.hp_of(char) == full // 2 + max(1, full // rpg.HP_REGEN_DIVISOR)

    ctx = _ctx(guild)
    await cog.cmd_status.callback(cog, ctx)
    sheet = ctx.sent_embeds[-1].description
    assert "**Health:**" in sheet and f"/{full:,}" in sheet and "█" in sheet
    assert "too hurt to fight" not in sheet

    char["hp"] = full // 10                               # under the camp line
    await cog.cmd_status.callback(cog, ctx)
    assert "too hurt to fight" in ctx.sent_embeds[-1].description


async def test_status_shows_the_biome_and_the_monster_tally():
    cog, guild, _idle = _world()
    _spawn(x=300, y=100, mob_kills=12, mob_deaths=3)
    ctx = _ctx(guild)
    await cog.cmd_status.callback(cog, ctx)
    sheet = ctx.sent_embeds[-1].description
    assert "[300, 100] — in Mountains country" in sheet and "**Monsters slain:** 12 · **Struck down:** 3" in sheet


# ── auto-enrollment ──────────────────────────────────────────────────────────

BOT_ROLE = 7001


def _enrolling_world(**kwargs):
    from tests.fakes.discord import FakeRole
    cog, guild, idle = _world(**kwargs)
    cog.rng = _StillRng()
    _state.bot_roles.add(BOT_ROLE)
    get_guild_cfg(GID)["idle_enroll"] = True
    for uid in (ALICE, BOB):
        guild.get_member(uid).roles = [FakeRole(BOT_ROLE)]
    return cog, guild, idle


async def test_members_with_a_bot_role_are_enrolled_silently_and_nobody_else_is():
    cog, guild, idle = _enrolling_world()
    guild.get_member(BOB).bot = True                              # a bot holding the role is not a player

    await cog.tick()

    chars = _state.idle_characters[GID]
    assert set(chars) == {ALICE}                                  # ADMIN has no bot role
    assert chars[ALICE]["class"] == "Adventurer" and chars[ALICE]["claimed"] is False
    assert chars[ALICE]["x"] is not None and not rpg.is_paused(chars[ALICE])
    idle.send.assert_not_called()
    idle.create_thread.assert_not_called()


async def test_enrollment_needs_the_setting_and_a_channel_and_is_paced():
    cog, guild, _idle = _enrolling_world()
    get_guild_cfg(GID)["idle_enroll"] = False
    await cog.tick()
    assert _state.idle_characters.get(GID, {}) == {}

    from tests.fakes.discord import FakeRole
    get_guild_cfg(GID)["idle_enroll"] = True
    for uid in range(100, 110):
        extra = FakeMember(uid, f"m{uid}")
        extra.roles, extra.status = [FakeRole(BOT_ROLE)], discord.Status.online
        guild.members.append(extra)
    await cog.tick()
    assert len(_state.idle_characters[GID]) == _idle_cog.ENROLL_PER_TICK
    await cog.tick()
    assert len(_state.idle_characters[GID]) == 2 * _idle_cog.ENROLL_PER_TICK


async def test_an_unclaimed_character_levels_without_a_thread_a_member_add_or_a_mention():
    cog, guild, idle = _enrolling_world()
    _spawn(ALICE, level=9, left=-10, claimed=False, **MARKET)     # level 10 is channel news

    await cog.tick()

    assert _state.idle_characters[GID][ALICE]["level"] == 10
    idle.create_thread.assert_not_called()                         # no thread, so nobody is added to one
    assert "reached **level 10**" in _sent(idle)
    for call in idle.send.call_args_list:
        assert call.kwargs["silent"] is True and call.kwargs["allowed_mentions"].to_dict() == {"parse": []}


async def test_a_quest_mentions_only_the_questers_who_claimed_their_character():
    cog, guild, idle = _world()
    _spawn(ALICE, level=45, left=50_000), _spawn(BOB, level=45, left=50_000, claimed=False)

    await cog.tick()

    call = idle.send.call_args
    assert f"<@{ALICE}>" in call.args[0] and f"<@{BOB}>" not in call.args[0] and "**bob**" in call.args[0]
    assert call.kwargs["allowed_mentions"].to_dict()["users"] == [ALICE]
    assert [t.name for t in guild.threads] == [guild.threads[0].name] and "alice" in guild.threads[0].name


async def test_idle_join_claims_the_waiting_character_with_a_free_class_and_a_thread():
    cog, guild, _idle = _enrolling_world()
    waiting = _spawn(ALICE, level=14, gold=900, claimed=False, items={"ring": {"level": 9, "name": None}},
                     **{"class": rpg.UNCLAIMED_CLASS})
    ctx = _ctx(guild)

    await cog.cmd_join.callback(cog, ctx)                          # no class: told what is waiting
    assert "level 14 Adventurer has been playing in your name" in ctx.sent_embeds[-1].description
    assert waiting["claimed"] is False

    await cog.cmd_join.callback(cog, ctx, class_name="Tax Wizard")
    assert _state.idle_characters[GID][ALICE] is waiting           # the same character, not a new one
    assert (waiting["claimed"], waiting["class"], waiting["level"], waiting["gold"]) == (True, "Tax Wizard", 14, 900)
    thread = guild.threads[0]
    assert waiting["thread_id"] == thread.id and "Tax Wizard" in thread.name
    thread.add_user.assert_awaited_once()
    assert ctx.sent_embeds[-1].title == "⚔️ Character Claimed"

    await cog.cmd_join.callback(cog, ctx, class_name="Bard")       # and only once
    assert ctx.sent_embeds[-1].title == "❌ Already Adventuring" and waiting["class"] == "Tax Wizard"


async def test_leaving_is_remembered_until_the_player_joins_again():
    cog, guild, _idle = _enrolling_world()
    guild.get_member(BOB).roles = []
    _spawn(ALICE, claimed=False)

    await cog.cmd_leave.callback(cog, _ctx(guild))
    assert ALICE in _state.idle_optouts[GID]
    await cog.tick()
    assert ALICE not in _state.idle_characters[GID]                # not handed another

    await cog.cmd_join.callback(cog, _ctx(guild), class_name="Bard")
    assert _state.idle_characters[GID][ALICE]["claimed"] is True and ALICE not in _state.idle_optouts[GID]


async def test_the_sheet_and_the_ladder_mark_an_unclaimed_character():
    cog, guild, _idle = _world()
    _spawn(ALICE, level=30), _spawn(BOB, level=12, claimed=False, x=1, y=1)
    ctx = _ctx(guild)
    await cog.cmd_status.callback(cog, ctx, member=guild.get_member(BOB))
    assert "Unclaimed" in ctx.sent_embeds[-1].description
    await cog.cmd_top.callback(cog, ctx)
    lines = ctx.sent_embeds[-1].description.split("\n")
    assert "unclaimed" not in lines[0] and lines[1].endswith("*unclaimed*")


async def test_settings_idle_enroll_toggles(monkeypatch):
    import src.cogs.settings_cog as _settings_cog
    monkeypatch.setattr(_settings_cog, "save_guild_settings", AsyncMock())
    guild = FakeGuild(gid=GID)
    guild.members = [FakeMember(ADMIN, "boss", administrator=True)]
    cog = SettingsCog(None)
    ctx = FakeCtx(author=guild.get_member(ADMIN), guild=guild, command_name="settings idle-enroll")
    await cog.settings_idle_enroll.callback(cog, ctx, "on")
    assert get_guild_cfg(GID)["idle_enroll"] is True and "idle channel is set" in ctx.sent_embeds[-1].description
    await cog.settings_idle_enroll.callback(cog, ctx, "maybe")
    assert get_guild_cfg(GID)["idle_enroll"] is True
    await cog.settings_idle_enroll.callback(cog, ctx, "OFF")
    assert get_guild_cfg(GID)["idle_enroll"] is False


async def test_optouts_survive_a_reload(db):
    _state.idle_optouts.setdefault(GID, set()).add(ALICE)
    await _persistence.save_idle_optout(GID, ALICE)
    await _persistence.save_idle_optout(GID, ALICE)                # twice is fine
    _state.idle_optouts.clear()
    await _persistence.init_db_state()
    assert _state.idle_optouts == {GID: {ALICE}}
    await _persistence.delete_idle_optout(GID, ALICE)
    _state.idle_optouts.clear()
    await _persistence.init_db_state()
    assert _state.idle_optouts == {}


# ── names, not mentions ──────────────────────────────────────────────────────

async def test_a_typed_at_name_part_of_a_name_or_an_id_finds_the_player():
    cog, guild, _idle = _world()
    char = _spawn(BOB, left=10_000)
    ctx = _ctx(guild, ADMIN)
    for who in ("@bob", "ob", "  @BOB ", str(BOB), f"<@{BOB}>"):
        due = char["next_level_at"]
        await cog.cmd_admin_push.callback(cog, ctx, who, "-1m")
        assert char["next_level_at"] == due - 60, who


async def test_an_ambiguous_name_prefers_whoever_is_playing_and_otherwise_lists_the_candidates():
    cog, guild, _idle = _world()
    bob = _spawn(BOB, left=10_000, x=1, y=1)
    due = bob["next_level_at"]
    ctx = _ctx(guild)

    await cog.cmd_status.callback(cog, ctx, member="bo")             # bob plays, boss doesn't: no contest
    assert "bob" in ctx.sent_embeds[-1].title

    _spawn(ADMIN, left=10_000, x=2, y=2)                              # now both do
    await cog.cmd_status.callback(cog, ctx, member="bo")
    said = ctx.sent_embeds[-1]
    assert said.title == "❌ Who?" and "**bob**" in said.description and "**boss**" in said.description

    _spawn(ALICE, left=10_000)
    await cog.cmd_duel.callback(cog, ctx, member="bo")               # a duel with "one of them" is no duel
    assert ctx.sent_embeds[-1].title == "❌ Who?"
    assert bob["next_level_at"] == due and _state.idle_characters[GID][ALICE]["duel_day"] is None

    await cog.cmd_status.callback(cog, ctx, member="bob")             # an exact name beats a partial one
    assert "bob" in ctx.sent_embeds[-1].title
    await cog.cmd_items.callback(cog, ctx, member="zelda")
    assert "Nobody here is called `zelda`" in ctx.sent_embeds[-1].description

    admin = _ctx(guild, ADMIN)
    await cog.cmd_admin_push.callback(cog, admin, "bo", "-1m")
    assert "could be" in admin.sent_embeds[-1].description and bob["next_level_at"] == due


async def test_names_are_read_live_and_a_rename_retitles_the_feed_thread():
    cog, guild, _idle = _world()
    char = _spawn(ALICE, level=4, thread_id=900)
    thread = FakeThread(thread_id=900, name="alice — Lv 4 Bard", parent_id=IDLE_CH)
    guild.threads.append(thread)
    assert "name" not in char and "display_name" not in char          # nothing to go stale

    member = guild.get_member(ALICE)
    member.guild = guild
    before = SimpleNamespace(display_name="alice")
    member.display_name = "Alice the Bold"
    await cog.on_member_update(before, member)
    await cog.tick()

    assert thread.edit.call_args.kwargs["name"] == "Alice the Bold — Lv 4 Bard"
    ctx = _ctx(guild)
    await cog.cmd_top.callback(cog, ctx)
    assert "Alice the Bold" in ctx.sent_embeds[-1].description


# ── !lb idle ─────────────────────────────────────────────────────────────────

async def test_lb_idle_ranks_this_servers_characters_by_prestige_then_level(monkeypatch):
    import src.cogs.economy_cog as _economy_cog
    cog, guild, _idle = _world()
    _spawn(ALICE, level=30)
    _spawn(BOB, level=2, prestige=1)
    paused = _spawn(ADMIN, level=12)
    rpg.pause(paused, int(time.time()))
    _state.idle_characters[2] = {999: rpg.new_character("Elsewhere", 0)}     # another server's: not listed

    async def _fetch(g, uid):
        return g.get_member(uid)
    monkeypatch.setattr(_economy_cog, "fetch_member", _fetch)
    economy = _economy_cog.EconomyCog.__new__(_economy_cog.EconomyCog)
    ctx = _ctx(guild)

    await economy.cmd_leaderboard.callback(economy, ctx, "idle")

    board = ctx.sent_embeds[-1]
    lines = board.description.split("\n")
    assert board.title == "⚔️ Idle RPG Leaderboard"
    assert lines[0].startswith("🥇 **bob** — ★ Lv 2 Bard") and lines[1].startswith("🥈 **alice** — Lv 30 Bard")
    assert lines[2].startswith("🥉 **boss** — Lv 12 Bard · paused") and "Elsewhere" not in board.description

    _state.idle_characters.pop(GID)
    await economy.cmd_leaderboard.callback(economy, ctx, "IDLE")
    assert "Nobody is adventuring here yet" in ctx.sent_embeds[-1].description


# ── the tables ───────────────────────────────────────────────────────────────

async def test_gamble_command_bets_in_town_and_the_sheet_keeps_score():
    cog, guild, _idle = _world()

    class _Lucky(_Rng):
        def randrange(self, n):
            return n - 1                                  # 99: a win
    cog.rng = _Lucky()
    char = _spawn(gold=1000, **MARKET)
    ctx = _ctx(guild)

    # Bare: the terms with stake buttons; the pick bets like the typed word.
    prompts = []

    async def _half(ctx_, **kwargs):
        prompts.append(kwargs)
        return "half"
    _idle_cog.confirm_choice = _half
    await cog.cmd_gamble.callback(cog, ctx)
    assert "even money" in prompts[0]["description"] and [c["value"] for c in prompts[0]["choices"]] == ["250", "half", "all", "other"]
    assert char["gold"] == 1500 and "won 500 gold" in ctx.sent_embeds[-1].description
    char["gold"] = 1000

    async def _other(ctx_, **kwargs):
        return "other"

    async def _form(ctx_, **kwargs):
        return {"amount": "100"}
    _idle_cog.confirm_choice = _other
    _idle_cog.open_form = _form
    await cog.cmd_gamble.callback(cog, ctx)
    assert char["gold"] == 1100
    char["gold"] = 1000

    await cog.cmd_gamble.callback(cog, ctx, "200")
    assert char["gold"] == 1200 and "won 200 gold" in ctx.sent_embeds[-1].description
    await cog.cmd_gamble.callback(cog, ctx, "half")
    assert char["gold"] == 1800
    await cog.cmd_gamble.callback(cog, ctx, "5k")
    assert char["gold"] == 1800 and ctx.sent_embeds[-1].title == "❌ The Tables"
    await cog.cmd_gamble.callback(cog, ctx, "lots")
    assert char["gold"] == 1800

    await cog.cmd_status.callback(cog, ctx)
    assert "**At the tables:** 4 bets · won 1,400 · lost 0" in ctx.sent_embeds[-1].description

    char["x"], char["y"] = WILDS                            # out in the wilds
    await cog.cmd_gamble.callback(cog, ctx, "all")
    assert char["gold"] == 1800 and "towns" in ctx.sent_embeds[-1].description


async def test_the_tick_gambles_for_a_character_in_town_in_the_feed_and_without_a_voice_top_up():
    cog, guild, idle = _world()

    class _Gambler(_StillRng):
        def random(self):
            return 0.0                                    # every chance hits
        def randrange(self, n):
            return n - 1                                  # and the bet wins
    cog.rng = _Gambler()
    import src.idlerpg as _rpg
    char = _spawn(level=10, left=50_000, gold=1000, auto_trade=False, x=MARKET["x"], y=MARKET["y"])
    _join_voice(guild, ALICE)
    real_events, real_team, real_world = _rpg.random_events, _rpg.team_battle, _rpg.tick_world
    _rpg.random_events = lambda *args: []
    _rpg.team_battle = lambda *args: []
    _rpg.tick_world = lambda *args: []
    try:
        await cog.tick()
    finally:
        _rpg.random_events, _rpg.team_battle, _rpg.tick_world = real_events, real_team, real_world

    assert char["gold"] == 1138                           # +138, and not a coin more for being in voice
    assert "sat down at the tables with 138 gold and doubled it" in _sent(guild.threads[0])
    idle.send.assert_not_called()


# ── travel ───────────────────────────────────────────────────────────────────

async def test_travel_sets_a_destination_shows_the_route_and_stop_clears_it():
    cog, guild, _idle = _world()
    char = _spawn(x=300, y=200)
    ctx = _ctx(guild)
    await cog.cmd_travel.callback(cog, ctx, where="velvragh")
    assert char["travel_to"] == "Velvragh"
    assert "70 squares" in ctx.sent_embeds[-1].description and "1h 56m" in ctx.sent_embeds[-1].description
    assert ctx.send_mock.call_args.kwargs["file"].filename == _idle_cog.MAP_FILENAME

    await cog.cmd_status.callback(cog, ctx)
    assert "**Travelling to:** Velvragh" in ctx.sent_embeds[-1].description

    await cog.cmd_travel.callback(cog, ctx, where="stop")
    assert char["travel_to"] is None
    await cog.cmd_travel.callback(cog, ctx, where="atlantis")
    assert ctx.sent_embeds[-1].title == "❌ Travel" and char["travel_to"] is None
    assert "**Wilds:**" in ctx.sent_embeds[-1].description

    await cog.cmd_travel.callback(cog, ctx, where="trnalvph")
    assert char["travel_to"] == "T'rnalvph" and "Darklands country" in ctx.sent_embeds[-1].description


async def test_travel_bare_lists_towns_nearest_first(monkeypatch):
    cog, guild, _idle = _world()
    char = _spawn(x=300, y=200)
    seen = {}

    async def _pick(ctx, *, options, **kwargs):
        seen["options"] = options
        return [options[0][1]]
    monkeypatch.setattr(_idle_cog, "pick_from_list", _pick)
    await cog.cmd_travel.callback(cog, _ctx(guild))
    assert [value for _label, value in seen["options"]][0] == "Velvragh" and len(seen["options"]) == len(rpg.LANDMARKS)
    labels = dict((value, label) for label, value in seen["options"])
    assert "(market)" in labels["Velvragh"] and "(Darklands)" in labels["T'rnalvph"]
    assert char["travel_to"] == "Velvragh"


async def test_no_travel_on_a_journey_quest_or_to_where_you_stand():
    cog, guild, _idle = _world()
    gx, gy = rpg.LANDMARKS["Denmark"]
    char = _spawn(x=gx, y=gy)
    ctx = _ctx(guild)
    await cog.cmd_travel.callback(cog, ctx, where="denmark")
    assert "already in Denmark" in ctx.sent_embeds[-1].description and char["travel_to"] is None

    _state.idle_quests[GID] = {**rpg.new_quest(), "members": [ALICE], "description": "walk", "kind": "journey", "p1": [1, 1], "p2": [2, 2]}
    await cog.cmd_travel.callback(cog, ctx, where="velvragh")
    assert "journey quest" in ctx.sent_embeds[-1].description and char["travel_to"] is None


async def test_arriving_in_town_is_announced_in_the_feed_and_the_errand_follows():
    cog, guild, idle = _world()

    class _Stride(_StillRng):
        def random(self):
            return 0.0                                       # every step chance hits (so does every stroke of luck)
    cog.rng = _Stride()
    gx, gy = rpg.LANDMARKS["Velvragh"]
    char = _spawn(level=10, left=50_000, gold=1000, x=gx - 20, y=gy, travel_to="Velvragh", items={"ring": {"level": 5, "name": None}})

    await cog.tick()

    assert (char["x"], char["y"]) == (gx, gy) and char["travel_to"] is None
    feed = _sent(guild.threads[0])
    assert "arrived in Velvragh" in feed and "did some trading" in feed


async def test_the_board_takes_a_hunt_in_town_and_says_why_not_elsewhere():
    cog, guild, _idle = _world()
    ctx = _ctx(guild)
    char = _spawn(ALICE, x=WILDS[0], y=WILDS[1])
    await cog.cmd_hunt.callback(cog, ctx)
    assert ctx.sent_embeds[-1].title == "❌ Hunt" and not rpg.hunting(char)

    char.update(MARKET, stay_left=300)
    await cog.cmd_hunt.callback(cog, ctx)
    text = ctx.sent_embeds[-1].description
    assert rpg.hunting(char) and "took a hunt off the board" in text and "keep to Velvragh for another 5m" in text
    await cog.cmd_hunt.callback(cog, ctx)
    assert "already on one" in ctx.sent_embeds[-1].description

    rpg.finish_hunt(ALICE, char, str, int(time.time()))
    await cog.cmd_hunt.callback(cog, ctx)
    assert "nothing for you for another" in ctx.sent_embeds[-1].description and not rpg.hunting(char)


# ── gold ─────────────────────────────────────────────────────────────────────

async def test_a_level_up_pays_gold_and_says_so_in_the_feed():
    cog, guild, _idle = _world()
    char = _spawn(level=6, left=-10)
    await cog.tick()
    assert char["level"] == 7 and char["gold"] == 70
    assert "+70 gold" in _sent(guild.threads[0])


async def test_shop_typed_purchases_charge_and_apply():
    cog, guild, _idle = _world()
    char = _spawn(level=10, left=10_000, gold=5000, items={"helm": {"level": 20, "name": None}}, **MARKET)
    ctx = _ctx(guild)

    await cog.cmd_shop.callback(cog, ctx, "sharpen", arg="Helm")
    assert char["items"]["helm"]["level"] == 22 and char["gold"] == 5000 - 400

    before = char["next_level_at"]
    await cog.cmd_shop.callback(cog, ctx, "rush")
    assert char["next_level_at"] < before and char["gold"] == 4600 - 150
    await cog.cmd_shop.callback(cog, ctx, "rush")
    assert "already rushed" in ctx.sent_embeds[-1].description and char["gold"] == 4450

    await cog.cmd_shop.callback(cog, ctx, "find")
    assert char["gold"] == 4450 - 250 and len(char["items"]) >= 1

    await cog.cmd_shop.callback(cog, ctx, "class", arg="Tax  Wizard")
    assert char["class"] == "Tax Wizard" and char["gold"] == 4200 - 100
    await cog.cmd_shop.callback(cog, ctx, "class", arg="@everyone")
    assert char["class"] == "Tax Wizard" and char["gold"] == 4100
    assert "4,100" not in ctx.sent_embeds[-1].description     # a refusal, not a receipt


async def test_shop_refuses_what_you_cannot_afford_or_do_not_own():
    cog, guild, _idle = _world()
    char = _spawn(level=10, gold=10, **MARKET)
    ctx = _ctx(guild)
    for item, arg in (("find", None), ("rush", None), ("sharpen", "ring"), ("duel", None), ("potion", "1"), ("amulet", None)):
        await cog.cmd_shop.callback(cog, ctx, item, arg=arg)
        assert ctx.sent_embeds[-1].title == "❌ Idle Shop"
    assert char["gold"] == 10 and char["items"] == {}


async def test_shop_second_duel_only_after_the_first_and_once_a_day():
    cog, guild, _idle = _world()
    alice, _bob = _spawn(ALICE, level=10, gold=1000, **MARKET), _spawn(BOB)
    ctx = _ctx(guild)
    await cog.cmd_shop.callback(cog, ctx, "duel")
    assert "haven't used today's duel" in ctx.sent_embeds[-1].description and alice["gold"] == 1000

    await cog.cmd_duel.callback(cog, ctx, member=guild.get_member(BOB))
    await cog.cmd_shop.callback(cog, ctx, "duel")
    assert alice["gold"] == 900 and alice["duel_day"] is None
    await cog.cmd_duel.callback(cog, ctx, member=guild.get_member(BOB))
    await cog.cmd_shop.callback(cog, ctx, "duel")
    assert "already bought" in ctx.sent_embeds[-1].description and alice["gold"] == 900


async def test_bare_shop_opens_a_menu_and_buys_the_pick(monkeypatch):
    cog, guild, _idle = _world()
    char = _spawn(level=10, left=10_000, gold=1000, **MARKET)
    menus = []

    async def _pick(ctx, *, title, options, **kwargs):
        menus.append((title, options))
        return ["rush"]
    monkeypatch.setattr(_idle_cog, "pick_from_list", _pick)
    await cog.cmd_shop.callback(cog, _ctx(guild))
    assert [key for _label, key in menus[0][1]] == ["find", "potion", "sharpen", "rush", "duel", "class"]
    assert char["gold"] == 850 and char["rush_day"] is not None


async def test_shop_potions_typed_and_from_the_menu(monkeypatch):
    cog, guild, _idle = _world()
    char = _spawn(level=10, left=10_000, gold=1000, **MARKET)
    ctx = _ctx(guild)

    await cog.cmd_shop.callback(cog, ctx, "potion", arg="2")
    assert char["potions"] == 2 and char["gold"] == 1000 - 2 * 50
    assert "Bought 2 potions. You carry 2/5" in ctx.sent_embeds[-1].description
    await cog.cmd_shop.callback(cog, ctx, "potion", arg="4")
    assert "only carry 3 more" in ctx.sent_embeds[-1].description and char["gold"] == 900
    await cog.cmd_shop.callback(cog, ctx, "potion", arg="lots")
    assert ctx.sent_embeds[-1].title == "❌ Idle Shop" and char["gold"] == 900

    menus = []

    async def _pick(ctx, *, title, options, **kwargs):
        menus.append((title, options))
        return ["potion"] if title == "🛒 Idle Shop" else ["3"]
    monkeypatch.setattr(_idle_cog, "pick_from_list", _pick)
    await cog.cmd_shop.callback(cog, _ctx(guild))
    assert [m[0] for m in menus] == ["🛒 Idle Shop", "🛒 Potions"]
    assert [key for _label, key in menus[1][1]] == ["1", "2", "3"]         # only what the belt has room for
    assert char["potions"] == 5 and char["gold"] == 900 - 150

    ctx = _ctx(guild)
    await cog.cmd_status.callback(cog, ctx)
    assert "🧪 **Potions:** 5/5" in ctx.sent_embeds[-1].description
    ctx = _ctx(guild)
    await cog.cmd_items.callback(cog, ctx)
    assert "🧪 **Potions:** 5/5" in ctx.sent_embeds[-1].description


async def test_concurrent_rushes_charge_once(monkeypatch):
    cog, guild, _idle = _world()
    char = _spawn(level=10, left=10_000, gold=1000, **MARKET)

    async def _yielding_save(*args):
        await asyncio.sleep(0)
    monkeypatch.setattr(_persistence, "save_idle_character", _yielding_save)
    await asyncio.gather(
        cog.cmd_shop.callback(cog, _ctx(guild), "rush"),
        cog.cmd_shop.callback(cog, _ctx(guild), "rush"),
    )
    assert char["gold"] == 850


async def test_the_shop_is_shut_in_the_wilds_and_says_where_the_nearest_market_is():
    cog, guild, _idle = _world()
    char = _spawn(level=10, gold=5000, x=WILDS[0], y=WILDS[1])   # open country, far from the rest
    ctx = _ctx(guild)
    for item in (None, "find", "rush"):
        await cog.cmd_shop.callback(cog, ctx, item)
        assert ctx.sent_embeds[-1].title == "❌ No Market Here"
    assert "squares away" in ctx.sent_embeds[-1].description
    assert char["gold"] == 5000

    await cog.cmd_status.callback(cog, ctx)
    assert "nearest market:" in ctx.sent_embeds[-1].description
    char["x"], char["y"] = 345, 270
    await cog.cmd_status.callback(cog, ctx)
    assert "market open (Velvragh)" in ctx.sent_embeds[-1].description


async def test_a_menu_pick_made_after_wandering_out_of_the_market_buys_nothing(monkeypatch):
    cog, guild, _idle = _world()
    char = _spawn(level=10, gold=1000, **MARKET)

    async def _pick_late(ctx, **kwargs):
        char["x"] = 100                                       # walked off while the dropdown was open
        return ["find"]
    monkeypatch.setattr(_idle_cog, "pick_from_list", _pick_late)
    await cog.cmd_shop.callback(cog, _ctx(guild))
    assert char["gold"] == 1000


async def test_walking_into_a_town_runs_the_errand_once_and_reports_it_in_the_feed():
    cog, guild, idle = _world()
    cog.rng = _StillRng()
    char = _spawn(level=10, left=50_000, gold=1000, x=325, y=270, items={"ring": {"level": 5, "name": None}, "helm": {"level": 40, "name": None}})

    await cog.tick()

    # Half the purse at most: a find (250), then the weakest item sharpened (5 × 20 = 100),
    # then potions with the 150 left — three fit, and the still rng takes the low end, two.
    assert char["gold"] == 550 and char["items"]["ring"]["level"] == 6 and char["items"]["helm"]["level"] == 40
    assert char["potions"] == 2
    feed = _sent(guild.threads[0])
    assert "wandered into Velvragh and did some trading" in feed and "450 gold spent, 550 gold left" in feed
    assert "Bought 2 potions (2/5)" in feed
    idle.send.assert_not_called()                             # the player's own business

    await cog.tick()                                          # still in town: not again for twelve hours
    assert char["gold"] == 550


async def test_auto_trade_can_be_switched_off_from_anywhere():
    cog, guild, _idle = _world()
    cog.rng = _StillRng()
    char = _spawn(level=10, left=50_000, gold=1000, x=WILDS[0], y=WILDS[1])
    ctx = _ctx(guild)
    await cog.cmd_shop.callback(cog, ctx, "auto", arg="off")
    assert char["auto_trade"] is False
    char["x"], char["y"] = 325, 270
    await cog.tick()
    assert char["gold"] == 1000
    await cog.cmd_shop.callback(cog, ctx, "auto")
    assert "Auto-trading is **off**" in ctx.sent_embeds[-1].description


async def test_a_wagered_duel_moves_the_gold_once_the_target_accepts(monkeypatch):
    cog, guild, _idle = _world()
    alice = _spawn(ALICE, left=10_000, gold=500, items={"ring": {"level": 9, "name": None}})
    bob = _spawn(BOB, left=10_000, gold=300)
    asked = {}

    async def _accept(ctx, *, payer, description, **kwargs):
        asked.update(payer=payer.id, description=description)
        return True
    monkeypatch.setattr(_idle_cog, "confirm_prompt", _accept)

    class _HighRoller(_Rng):
        def randint(self, low, high):
            return high
    cog.rng = _HighRoller()                      # alice rolls 9, bob has nothing to roll

    ctx = _ctx(guild)
    await cog.cmd_duel.callback(cog, ctx, member=guild.get_member(BOB), wager="200")
    assert asked["payer"] == BOB and "200 gold" in asked["description"]
    assert (alice["gold"], bob["gold"]) == (700, 100)
    assert "200 gold on the table" in ctx.sent_embeds[-1].description


async def test_a_declined_or_uncoverable_wager_costs_nothing_not_even_the_daily_duel(monkeypatch):
    cog, guild, _idle = _world()
    alice, bob = _spawn(ALICE, gold=500), _spawn(BOB, gold=300)
    ctx = _ctx(guild)

    await cog.cmd_duel.callback(cog, ctx, member=guild.get_member(BOB), wager="400")
    assert "only has 300" in ctx.sent_embeds[-1].description and alice["duel_day"] is None

    async def _decline(*args, **kwargs):
        return False
    monkeypatch.setattr(_idle_cog, "confirm_prompt", _decline)
    await cog.cmd_duel.callback(cog, ctx, member=guild.get_member(BOB), wager="200")
    assert alice["duel_day"] is None and (alice["gold"], bob["gold"]) == (500, 300)

    async def _accept_after_spending(*args, **kwargs):
        bob["gold"] = 50                         # spent it while the prompt was open
        return True
    monkeypatch.setattr(_idle_cog, "confirm_prompt", _accept_after_spending)
    await cog.cmd_duel.callback(cog, ctx, member=guild.get_member(BOB), wager="200")
    assert "fell through" in ctx.sent_embeds[-1].description
    assert alice["duel_day"] is None and (alice["gold"], bob["gold"]) == (500, 50)


async def test_status_shows_gold_and_admin_can_adjust_it():
    cog, guild, _idle = _world()
    char = _spawn(gold=1250, x=1, y=1)
    ctx = _ctx(guild)
    await cog.cmd_status.callback(cog, ctx)
    assert "**Gold:** 1,250" in ctx.sent_embeds[-1].description

    admin = _ctx(guild, ADMIN)
    await cog.cmd_admin_gold.callback(cog, admin, f"<@{ALICE}>", "+2k")
    assert char["gold"] == 3250
    await cog.cmd_admin_gold.callback(cog, admin, f"<@{ALICE}>", "-9999999")
    assert char["gold"] == 0


# ── admin ────────────────────────────────────────────────────────────────────

async def test_admin_move_sets_a_character_down_by_place_or_coordinates():
    cog, guild, _idle = _world()
    char = _spawn(BOB, travel_to="Denmark", x=1, y=1)
    ctx = _ctx(guild, ADMIN)

    await cog.cmd_admin_move.callback(cog, ctx, "bob", where="velvragh")
    assert (char["x"], char["y"]) == rpg.LANDMARKS["Velvragh"] and char["travel_to"] is None
    assert "Velvragh" in ctx.sent_embeds[-1].description and "market is open" in ctx.sent_embeds[-1].description

    await cog.cmd_admin_move.callback(cog, ctx, "bob", where=f"{WILDS[0]} {WILDS[1]}")
    assert (char["x"], char["y"]) == WILDS
    assert "Open country" in ctx.sent_embeds[-1].description and "Plains" in ctx.sent_embeds[-1].description

    for bad in ("atlantis", "900 20", "1 2 3"):
        await cog.cmd_admin_move.callback(cog, ctx, "bob", where=bad)
        assert ctx.sent_embeds[-1].title == "❌ Idle Admin", bad
    assert (char["x"], char["y"]) == WILDS


async def test_admin_push_moves_a_clock_both_ways():
    cog, guild, _idle = _world()
    char = _spawn(left=10_000)
    due = char["next_level_at"]
    ctx = _ctx(guild, ADMIN)
    await cog.cmd_admin_push.callback(cog, ctx, f"<@{ALICE}>", "+1h")
    assert char["next_level_at"] == due + 3600
    await cog.cmd_admin_push.callback(cog, ctx, f"<@{ALICE}>", "-2h")
    assert char["next_level_at"] == due - 3600
    await cog.cmd_admin_push.callback(cog, ctx, f"<@{ALICE}>", "soon")
    assert ctx.sent_embeds[-1].title == "❌ Idle Admin"


async def test_admin_remove_reaches_someone_who_left_the_server():
    cog, guild, _idle = _world()
    _spawn(uid=4040)
    await cog.cmd_admin_remove.callback(cog, _ctx(guild, ADMIN), who="4040")
    assert 4040 not in _state.idle_characters[GID]


async def test_admin_reset_wipes_the_guild_only():
    cog, guild, _idle = _world()
    _spawn(ALICE), _spawn(BOB)
    _state.idle_characters[2] = {ALICE: rpg.new_character("Elsewhere", 0)}
    _state.idle_quests[GID] = rpg.new_quest()
    await cog.cmd_admin_reset.callback(cog, _ctx(guild, ADMIN))
    assert GID not in _state.idle_characters and GID not in _state.idle_quests
    assert ALICE in _state.idle_characters[2]


async def test_permission_tiers():
    _state.command_perms = json.loads(Path("src/command_perms.json").read_text(encoding="utf-8"))
    assert get_command_perm("idle join")["tier"] == "everyone"
    assert get_command_perm("idle admin reset")["tier"] == "server_admin"
    assert get_command_perm("settings-channel idle")["tier"] == "server_admin"


# ── the channel setting ──────────────────────────────────────────────────────

async def test_settings_channel_idle_sets_and_clears(monkeypatch):
    # settings_cog binds the saver at import, out of the conftest stub's reach.
    monkeypatch.setattr("src.cogs.settings_cog.save_guild_settings", AsyncMock())
    guild = FakeGuild(gid=GID)
    guild.members = [FakeMember(ADMIN, "boss", administrator=True)]
    cog = SettingsCog(None)
    ctx = FakeCtx(author=guild.get_member(ADMIN), guild=guild, command_name="settings-channel idle")
    ctx.message.channel_mentions = [FakeTextChannel(ch_id=IDLE_CH)]
    await cog.settings_channel_idle.callback(cog, ctx)
    assert get_guild_cfg(GID)["idle_channel"] == IDLE_CH

    ctx.message.channel_mentions = []
    await cog.settings_channel_idle.callback(cog, ctx, "clear")
    assert get_guild_cfg(GID)["idle_channel"] is None


async def test_the_tick_runs_at_the_guilds_pace(monkeypatch):
    cog, guild, _idle = _world()
    _spawn(left=50_000)
    seen = []
    monkeypatch.setattr(rpg, "random_events", lambda *args: seen.append(args[-1]) or [])

    await cog.tick()
    get_guild_cfg(GID)["idle_pace"] = "classic"
    await cog.tick()
    get_guild_cfg(GID)["idle_pace"] = "nonsense"
    await cog.tick()

    assert seen == [rpg.PACES["lively"], rpg.CLASSIC, rpg.PACES["lively"]]


async def test_settings_idle_pace_sets_validates_and_prompts(monkeypatch):
    import src.cogs.settings_cog as _settings_cog
    monkeypatch.setattr(_settings_cog, "save_guild_settings", AsyncMock())
    guild = FakeGuild(gid=GID)
    guild.members = [FakeMember(ADMIN, "boss", administrator=True)]
    cog = SettingsCog(None)
    ctx = FakeCtx(author=guild.get_member(ADMIN), guild=guild, command_name="settings idle-pace")

    await cog.settings_idle_pace.callback(cog, ctx, "Classic")
    assert get_guild_cfg(GID)["idle_pace"] == "classic"
    await cog.settings_idle_pace.callback(cog, ctx, "turbo")
    assert get_guild_cfg(GID)["idle_pace"] == "classic" and "Usage" in ctx.sent_embeds[-1].description

    async def _pick(ctx, *, choices, **kwargs):
        return "lively"
    monkeypatch.setattr(_settings_cog, "confirm_choice", _pick)
    await cog.settings_idle_pace.callback(cog, ctx)
    assert get_guild_cfg(GID)["idle_pace"] == "lively"


# ── persistence ──────────────────────────────────────────────────────────────

async def test_characters_and_quests_round_trip_through_the_db(db):
    char = _spawn(level=12, items={"ring": {"level": 9, "name": None}}, thread_id=900, law="chaotic", x=17, y=499,
                  gold=4321, rush_day="2026-09-21", extra_duel_day="2026-09-20", auto_trade=False, traded_at=1234, travel_to="Velvragh", mob_kills=12, mob_deaths=3,
                  gamble_town="Denmark", gamble_visit_at=99, gamble_budget=40, gambles=7, gamble_won=300, gamble_lost=450, claimed=False)
    paused = _spawn(BOB)
    rpg.pause(paused, int(time.time()))
    _state.idle_quests[GID] = {
        "members": [ALICE, BOB], "description": "walk", "kind": "journey", "ends_at": None,
        "stage": 2, "p1": [35, 40], "p2": [410, 80], "not_before": 45,
    }
    expected_quest = dict(_state.idle_quests[GID])
    await _persistence.save_idle_character(GID, ALICE)
    await _persistence.save_idle_character(GID, BOB)
    await _persistence.save_idle_quest(GID)
    expected, expected_paused = dict(char), dict(paused)

    _state.idle_characters.clear()
    _state.idle_quests.clear()
    await _persistence.init_db_state()

    assert _state.idle_characters[GID][ALICE] == expected
    assert _state.idle_characters[GID][BOB] == expected_paused
    assert _state.idle_quests[GID] == expected_quest

    await _persistence.delete_idle_guild(GID)
    _state.idle_characters.clear()
    await _persistence.init_db_state()
    assert GID not in _state.idle_characters


async def test_items_sheet_shows_the_purse_too():
    cog, guild, _idle = _world()
    _spawn(gold=1250, items={"ring": {"level": 9, "name": None}})
    ctx = _ctx(guild)
    await cog.cmd_items.callback(cog, ctx)
    assert "**Item power:** 9 · **Gold:** 1,250" in ctx.sent_embeds[-1].description


# ── the world, blessings and the boost ───────────────────────────────────────

class _Eager(_StillRng):
    """Every chance fires — a tick rolls the world event and the town errand
    it would otherwise not see for days — but nobody wanders out of town
    while it happens."""
    def random(self):
        return 0.0


async def test_a_world_event_is_channel_news_and_reaches_no_feed(monkeypatch):
    cog, guild, idle = _world()
    await cog.cmd_join.callback(cog, _ctx(guild), class_name="Bard")
    _state.idle_characters[GID][ALICE].update(level=5, next_level_at=int(time.time()) + 50_000, **MARKET)
    cog.rng = _Eager()
    monkeypatch.setattr(rpg, "random_events", lambda *args: [])
    monkeypatch.setattr(rpg, "mob_encounter", lambda *args, **kw: [])
    idle.send.reset_mock()

    await cog.tick()

    row = _state.idle_guild_events[GID][0]
    assert row["kind"] in rpg.WORLD_EVENTS and row["stage"] == 0
    posted = _sent(idle)
    assert posted and posted == rpg.WORLD_EVENTS[row["kind"]].omen.format(detail=row["detail"])
    assert posted not in _sent(guild.threads[0])      # the whole realm's news, nobody's feed

    # Only once it lands does it bite.
    await cog.tick(now=row["starts_at"])
    assert _state.idle_guild_events[GID][0]["stage"] == 1
    assert rpg.WORLD_EVENTS[row["kind"]].begins.format(detail=row["detail"]) in _sent(idle)
    assert rpg.world_effect(_state.idle_guild_events[GID], row["starts_at"]) is not None


async def test_a_blessing_speeds_up_everybodys_clock_and_shows_in_the_sheet(monkeypatch):
    cog, guild, idle = _world()
    alice = _spawn(ALICE, level=5, left=50_000, **MARKET)
    bob = _spawn(BOB, level=5, left=50_000, **MARKET)
    _state.idle_guild_events[GID] = [{
        "kind": "bless", "detail": "", "cast_by": ALICE, "stage": 1,
        "starts_at": int(time.time()), "ends_at": int(time.time()) + 3600,
    }]
    monkeypatch.setattr(rpg, "random_events", lambda *args: [])
    monkeypatch.setattr(rpg, "mob_encounter", lambda *args, **kw: [])
    monkeypatch.setattr(rpg, "tick_world", lambda *args: [])
    before = (alice["next_level_at"], bob["next_level_at"])

    await cog.tick()

    # A minute at +25% is fifteen seconds off, for everyone here.
    assert before[0] - alice["next_level_at"] == 15
    assert before[1] - bob["next_level_at"] == 15

    ctx = _ctx(guild)
    await cog.cmd_status.callback(cog, ctx)
    assert "✨ **Boosted:** levelling and gold 25% faster" in ctx.sent_embeds[-1].description


async def test_bless_charges_the_caster_and_tells_the_room():
    cog, guild, idle = _world()
    char = _spawn(ALICE, level=30, gold=rpg.BLESS_COST + 7, **MARKET)
    ctx = _ctx(guild)

    await cog.cmd_bless.callback(cog, ctx)

    assert char["gold"] == 7
    assert rpg.bless_count(_state.idle_guild_events[GID], int(time.time())) == 1
    assert ctx.sent_embeds[-1].title == "🕊️ Blessed"
    assert "has blessed the realm" in _sent(idle)


async def test_bless_is_refused_without_the_gold_and_takes_nothing():
    cog, guild, idle = _world()
    char = _spawn(ALICE, level=30, gold=rpg.BLESS_COST - 1, **MARKET)
    ctx = _ctx(guild)

    await cog.cmd_bless.callback(cog, ctx)

    assert char["gold"] == rpg.BLESS_COST - 1 and not _state.idle_guild_events.get(GID)
    assert ctx.sent_embeds[-1].title == "❌ Not Enough Gold"


async def test_world_reports_the_omen_the_blessings_and_your_own_boost():
    cog, guild, _idle = _world()
    _spawn(ALICE, level=5, boost_pct=50, boost_until=int(time.time()) + 600, **MARKET)
    ctx = _ctx(guild)
    await cog.cmd_world.callback(cog, ctx)
    body = ctx.sent_embeds[-1].description
    assert "The realm is quiet" in body and "No blessing is on the realm" in body
    assert "levelling and earning **50% faster**" in body


# ── titles ───────────────────────────────────────────────────────────────────

async def test_the_tick_awards_a_title_publicly_and_it_shows_in_the_thread_name(monkeypatch):
    cog, guild, idle = _world()
    await cog.cmd_join.callback(cog, _ctx(guild), class_name="Bard")
    char = _state.idle_characters[GID][ALICE]
    char.update(level=5, next_level_at=int(time.time()) + 50_000, gold=50_000, **MARKET)
    monkeypatch.setattr(rpg, "random_events", lambda *args: [])
    monkeypatch.setattr(rpg, "mob_encounter", lambda *args, **kw: [])
    monkeypatch.setattr(rpg, "tick_world", lambda *args: [])
    idle.send.reset_mock()

    await cog.tick()

    assert char["titles"] == ["hoarder"] and char["title"] == "hoarder"
    assert "earned the title **Gold Hoarder**" in _sent(idle)
    assert "alice the Gold Hoarder" in cog._title(guild, ALICE, char)


async def test_title_lists_what_is_earned_and_wears_the_one_asked_for():
    cog, guild, _idle = _world()
    char = _spawn(ALICE, level=5, titles=["hoarder", "hunter"], title="hoarder", **MARKET)

    ctx = _ctx(guild)
    await cog.cmd_title.callback(cog, ctx)
    body = ctx.sent_embeds[-1].description
    assert "🎖️ **Gold Hoarder** ← worn" in body and "🔒 Tracker" in body

    ctx = _ctx(guild)
    await cog.cmd_title.callback(cog, ctx, which="monster hunter")
    assert char["title"] == "hunter" and "Monster Hunter" in ctx.sent_embeds[-1].description

    ctx = _ctx(guild)
    await cog.cmd_title.callback(cog, ctx, which="none")
    assert char["title"] is None


async def test_an_unearned_title_is_refused():
    cog, guild, _idle = _world()
    char = _spawn(ALICE, level=5, titles=["hoarder"], title="hoarder", **MARKET)
    ctx = _ctx(guild)
    await cog.cmd_title.callback(cog, ctx, which="Tracker")
    assert ctx.sent_embeds[-1].title == "❌ No Such Title" and char["title"] == "hoarder"


# ── the bag ──────────────────────────────────────────────────────────────────

async def test_shop_sell_empties_the_bag_and_items_lists_it():
    cog, guild, _idle = _world()
    char = _spawn(ALICE, level=10, gold=0, **MARKET)
    char["loot"] = [{"slot": "ring", "level": 4, "name": "Crude Iron Ring"}]

    ctx = _ctx(guild)
    await cog.cmd_items.callback(cog, ctx)
    assert "Crude Iron Ring (4)" in ctx.sent_embeds[-1].description

    ctx = _ctx(guild)
    await cog.cmd_shop.callback(cog, ctx, item="sell")
    assert char["loot"] == [] and char["gold"] == 4 * rpg.LOOT_GOLD_PER_LEVEL
    assert "Sold 1 piece" in ctx.sent_embeds[-1].description


# ── hunts ────────────────────────────────────────────────────────────────────

async def test_a_tick_in_town_hands_out_a_hunt_and_the_sheet_shows_it(monkeypatch):
    cog, guild, idle = _world()
    await cog.cmd_join.callback(cog, _ctx(guild), class_name="Bard")
    char = _state.idle_characters[GID][ALICE]
    char.update(level=10, next_level_at=int(time.time()) + 50_000, **MARKET)
    cog.rng = _Eager()
    monkeypatch.setattr(rpg, "random_events", lambda *args: [])
    monkeypatch.setattr(rpg, "mob_encounter", lambda *args, **kw: [])
    monkeypatch.setattr(rpg, "tick_world", lambda *args: [])

    await cog.tick()

    assert rpg.hunting(char)
    assert "asked to deal with" in _sent(guild.threads[0])
    assert "asked to deal with" not in _sent(idle)      # an errand is the player's own business

    ctx = _ctx(guild)
    await cog.cmd_status.callback(cog, ctx)
    assert f"0/{char['hunt_count']} {char['hunt_mob']}s" in ctx.sent_embeds[-1].description


async def test_a_hunter_walks_at_its_country_instead_of_wandering():
    cog, guild, _idle = _world()
    char = _spawn(ALICE, level=10, left=50_000, x=WILDS[0], y=WILDS[1], hunt_mob="Crab", hunt_count=3)
    goal = rpg.nearest_biome_point(char, rpg.hunt_biomes(char))   # the Crab is a coast animal
    char["hunt_x"], char["hunt_y"] = goal
    was = abs(char["x"] - goal[0]) + abs(char["y"] - goal[1])

    class _Stride(_Rng):
        def random(self):
            return 0.0            # every step of the walk is taken

    cog.rng = _Stride()
    rpg.move_players(_state.idle_characters[GID], cog._quest(GID), cog.rng, cog._namer(guild), int(time.time()), 5)
    assert abs(char["x"] - goal[0]) + abs(char["y"] - goal[1]) == was - 10   # five steps, both axes
    assert char["hunt_x"] == goal[0]          # still walking: the coast is further than five steps
    ctx = _ctx(guild)
    await cog.cmd_status.callback(cog, ctx)
    steps = rpg.steps_to(char, goal)
    assert f"0/3 Crabs — walking to [{goal[0]}, {goal[1]}], {steps} squares, about" in ctx.sent_embeds[-1].description


# ── the standings board ──────────────────────────────────────────────────────

async def test_the_board_is_posted_once_pinned_and_then_edited_in_place(monkeypatch):
    cog, guild, idle = _world()
    _spawn(ALICE, level=12, gold=900, mob_kills=40,
           items={"ring": {"level": 30, "name": "Sturdy Iron Ring"}}, **MARKET)
    _spawn(BOB, level=7, gold=50, **MARKET)
    monkeypatch.setattr(rpg, "random_events", lambda *args: [])
    monkeypatch.setattr(rpg, "mob_encounter", lambda *args, **kw: [])
    monkeypatch.setattr(rpg, "tick_world", lambda *args: [])
    posted = []

    async def _send(*args, **kwargs):
        message = SimpleNamespace(id=4242, edit=AsyncMock(), pin=AsyncMock())
        posted.append((kwargs.get("embed"), message))
        return message
    idle.send = AsyncMock(side_effect=_send)
    idle.fetch_message = AsyncMock(side_effect=lambda mid: posted[0][1])

    del cog._board_at[GID]                     # let the sweep through
    await cog.tick()

    assert len(posted) == 1
    embed = posted[0][0]
    assert embed.title == "🏔️ The Realm's Standings" and "alice" in embed.description
    assert [f.name for f in embed.fields] == ["Gold", "Monsters slain", "Item power"]
    posted[0][1].pin.assert_awaited_once()
    assert get_guild_cfg(GID)["idle_board_message"] == 4242

    # The next sweep edits that message rather than posting a second one.
    cog._board_at[GID] -= 10_000
    await cog.tick()
    assert len(posted) == 1 and posted[0][1].edit.await_count == 1


async def test_the_board_waits_out_its_interval():
    cog, guild, idle = _world()
    _spawn(ALICE, level=5, **MARKET)
    idle.send.reset_mock()
    await cog._sweep_board(guild)              # inside the interval _world() stamped
    idle.send.assert_not_called()


async def test_a_deleted_board_is_posted_again(monkeypatch):
    cog, guild, idle = _world()
    _spawn(ALICE, level=5, **MARKET)
    get_guild_cfg(GID)["idle_board_message"] = 111
    idle.fetch_message = AsyncMock(side_effect=discord.NotFound(
        SimpleNamespace(status=404, reason="gone"), "gone"))
    made = SimpleNamespace(id=222, edit=AsyncMock(), pin=AsyncMock())
    idle.send = AsyncMock(return_value=made)

    del cog._board_at[GID]
    await cog._sweep_board(guild)

    idle.send.assert_awaited_once()
    assert get_guild_cfg(GID)["idle_board_message"] == 222


async def test_the_board_survives_a_realm_with_nobody_in_it():
    cog, guild, _idle = _world()
    embed = cog._board_embed(guild)
    assert "Nobody is adventuring here yet" in embed.description and not embed.fields


# ── lore ─────────────────────────────────────────────────────────────────────

async def test_lore_reads_a_place_and_lists_them_all_when_asked_vaguely():
    cog, guild, _idle = _world()

    ctx = _ctx(guild)
    await cog.cmd_lore.callback(cog, ctx, where="velvragh")
    assert ctx.sent_embeds[-1].title == "📜 Velvragh"
    assert rpg.LORE["Velvragh"] in ctx.sent_embeds[-1].description
    assert "a market town" in ctx.sent_embeds[-1].description

    ctx = _ctx(guild)
    await cog.cmd_lore.callback(cog, ctx)
    assert all(place in ctx.sent_embeds[-1].description for place in rpg.LANDMARKS)

    ctx = _ctx(guild)
    await cog.cmd_lore.callback(cog, ctx, where="atlantis")
    assert "Nowhere on the map goes by that name" in ctx.sent_embeds[-1].description


async def test_lore_names_the_country_of_a_wild_place():
    cog, guild, _idle = _world()
    ctx = _ctx(guild)
    await cog.cmd_lore.callback(cog, ctx, where="trnalvph")
    assert ctx.sent_embeds[-1].title == "📜 T'rnalvph"
    assert "Darklands country" in ctx.sent_embeds[-1].description


# ── a world event keeps to its hours through the cog ─────────────────────────

async def test_the_tick_offers_only_the_kinds_the_hour_allows(monkeypatch):
    cog, guild, _idle = _world()
    _spawn(ALICE, level=5, left=50_000, **MARKET)
    monkeypatch.setattr(rpg, "random_events", lambda *args: [])
    monkeypatch.setattr(rpg, "mob_encounter", lambda *args, **kw: [])
    seen = {}

    def _spy(rows, rng, now, ticks_per_day, hour=0):
        seen["hour"] = hour
        return []
    monkeypatch.setattr(rpg, "tick_world", _spy)
    monkeypatch.setattr(_idle_cog, "_ct_now", lambda: SimpleNamespace(hour=21))

    await cog.tick()

    assert seen["hour"] == 21                  # the cog hands the rules a CT hour
    assert "blood_moon" in rpg.world_kinds_at(21)


# ── bare commands, feed threads and the cards' menus ─────────────────────────

def _interaction(guild, uid: int, channel=None):
    response = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), send_modal=AsyncMock(),
                               defer=AsyncMock(), is_done=lambda: False)
    return SimpleNamespace(user=guild.get_member(uid), guild=guild, channel=channel or guild.channels[0],
                           response=response, followup=SimpleNamespace(send=AsyncMock()),
                           edit_original_response=AsyncMock())


def _ctx_in(guild, uid: int, channel, command_name: str) -> FakeCtx:
    return FakeCtx(author=guild.get_member(uid), guild=guild, channel=channel, command_name=command_name)


async def test_every_subcommand_but_admin_shop_and_help_is_registered_bare():
    # Registration happens in the constructor and unload undoes it; add_cog
    # would also start the tick loop, which has no gateway to wait on here.
    bot = discord.ext.commands.Bot(command_prefix="!", intents=discord.Intents.none(), help_command=None)
    cog = IdleCog(bot)
    try:
        assert bot.get_command("map").callback is IdleCog.cmd_map.callback
        assert bot.get_command("info").callback is IdleCog.cmd_status.callback      # aliases travel too
        assert bot.get_command("store").callback is IdleCog.cmd_shop.callback       # the coin shop has `!shop`
        assert bot.get_command("map").cog is cog                                     # bound, so `self` isn't the ctx
        for absent in ("admin", "shop", "help"):
            assert bot.get_command(absent) is None
        assert set(_idle_cog.FEED_THREAD_COMMANDS) >= {"idle", "help", "store", "map", "travel", "rules"}
        assert "shop" not in _idle_cog.FEED_THREAD_COMMANDS
        # The bare form runs the same code.
        guild = _world()[1]
        cog.rng = _Rng()
        ctx = _ctx(guild)
        await bot.get_command("top")(ctx)
        assert ctx.sent_embeds[-1].title.endswith("Idle Ladder")
    finally:
        cog.cog_unload()
    assert bot.get_command("map") is None                                            # unload takes them with it


async def test_a_feed_thread_only_runs_the_idle_game():
    cog, guild, idle = _world()
    _spawn(ALICE, thread_id=900)
    feed = FakeThread(thread_id=900, parent_id=IDLE_CH)
    other = FakeThread(thread_id=901, parent_id=IDLE_CH)

    for allowed in ("idle", "idle status", "idle admin hog", "map", "status", "travel", "help", "store"):
        assert await cog.bot_check(_ctx_in(guild, BOB, feed, allowed)) is True
    for refused in ("slots", "shop", "shop insurance", "insurance", "ask"):
        ctx = _ctx_in(guild, BOB, feed, refused)
        with pytest.raises(_idle_cog.IdleThreadOnly):
            await cog.bot_check(ctx)
        assert ctx.sent_embeds[-1].title == "⚔️ Idle Feed"
    # Only a character's feed thread is gated — not another thread, not the channel.
    assert await cog.bot_check(_ctx_in(guild, BOB, other, "slots")) is True
    assert await cog.bot_check(_ctx_in(guild, BOB, idle, "slots")) is True


async def test_help_is_the_games_in_idle_context():
    from src.cogs.utility_cog import UtilityCog
    cog, guild, idle = _world()
    _spawn(ALICE, thread_id=900)
    feed = FakeThread(thread_id=900, parent_id=IDLE_CH)
    elsewhere = FakeTextChannel(ch_id=77, name="general")
    assert cog.in_idle_context(_ctx(guild, channel=idle))
    assert cog.in_idle_context(_ctx(guild, channel=feed))
    assert not cog.in_idle_context(_ctx(guild, channel=elsewhere))
    assert not cog.in_idle_context(FakeCtx(author=guild.get_member(ALICE), guild=None, channel=elsewhere))

    bot = SimpleNamespace(get_cog=lambda name: cog if name == "IdleCog" else None)
    util = UtilityCog(bot)
    ctx = _ctx(guild, channel=feed)
    await util.cmd_help.callback(util, ctx)
    assert ctx.sent_embeds[-1].title == "📖 Idle RPG"
    assert isinstance(ctx.sent_views[-1], _idle_cog._HelpView)


async def test_the_status_card_is_the_sheet_and_the_map_with_no_menu():
    cog, guild, _idle = _world()
    _spawn(BOB, **MARKET, level=4)
    ctx = _ctx(guild)
    await cog.cmd_status.callback(cog, ctx, member=guild.get_member(BOB))
    assert "Lv 4 Bard" in ctx.sent_embeds[-1].title and ctx.sent_views == []
    assert ctx.send_mock.await_args.kwargs["file"].filename == MAP_FILENAME


# ── the panel ────────────────────────────────────────────────────────────────

def _hub(cog, guild, uid: int = ALICE, page: str = "sheet") -> "tuple[IdleHub, FakeCtx]":
    ctx = _ctx(guild, uid)
    return IdleHub(cog, ctx, page), ctx


def _keys(page: str, hub: IdleHub) -> list:
    return [item.key for item in items_for(page, hub)]


def _real_tiers():
    """The admin page follows the shipped `idle admin` tier; conftest empties the table."""
    _state.command_perms.update(json.loads(Path("src/command_perms.json").read_text(encoding="utf-8")))


async def test_the_panel_offers_what_its_invoker_could_do_now():
    _real_tiers()
    cog, guild, _idle = _world()
    # No character: Common pitches and offers Join; no town, road or character page; no admin page for a player.
    hub, _ctx_ = _hub(cog, guild)
    assert [key for key, _label in hub.pages()] == ["common", "realm", "rules"]
    assert "**Join** below" in hub.embed().description
    assert _keys("common", hub) == ["refresh", "join", "quest", "top"]
    assert _keys("realm", hub) == ["world", "quest", "top", "look", "their-items"]

    # Out in the wilds with no gold: no store, no tables; the town page offers the road there.
    char = _spawn(ALICE, x=WILDS[0], y=WILDS[1], gold=0)
    hub, _ctx_ = _hub(cog, guild)
    assert [key for key, _label in hub.pages()] == ["common", "town", "road", "character", "realm", "rules"]
    assert "Lv 0 Bard" in hub.embed().title
    assert _keys("common", hub) == ["refresh", "items", "travel", "quest", "top"]
    assert _keys("town", hub) == ["travel", "auto"]
    assert _keys("road", hub) == ["travel", "lore"]
    assert _keys("character", hub) == ["items", "align", "titles", "duel", "leave"]
    assert "bless" in _keys("realm", hub)

    # In a market ring with gold, at the prestige level, on a journey: the store, the tables and the board; no travel.
    char.update(MARKET, gold=50, level=rpg.PRESTIGE_LEVEL, titles=["hoarder"])
    _state.idle_quests[GID] = {**rpg.new_quest(), "members": [ALICE], "description": "walk", "kind": "journey", "p1": [35, 40], "p2": [410, 80]}
    hub, _ctx_ = _hub(cog, guild)
    assert _keys("common", hub) == ["refresh", "items", "store", "gamble", "hunt", "quest", "top", "prestige"]
    assert _keys("town", hub) == ["shop-find", "shop-potion", "shop-rush", "shop-duel", "shop-class", "gamble", "hunt", "auto"]
    assert _keys("road", hub) == ["lore"]
    assert _keys("character", hub) == ["items", "align", "titles", "wear", "duel", "prestige", "leave"]
    for page, _label in hub.pages():
        for item in items_for(page, hub):
            assert len(item.label) <= 100 and len(item.description) <= 100 and item.short and len(item.short) <= 40
    # Off the journey but travelling: the road offers Stop. A bag sells, an item sharpens, a full belt isn't restocked;
    # broke, the tables close; on a hunt, the board is gone.
    _state.idle_quests[GID]["members"] = [BOB]
    char.update(gold=0, hunt_mob="Rat", hunt_count=3, travel_to="Denmark", potions=rpg.POTION_MAX,
                loot=[{"slot": "helm", "level": 3, "name": None}], items={"amulet": {"level": 2, "name": None}})
    hub, _ctx_ = _hub(cog, guild)
    assert _keys("road", hub) == ["travel", "stop", "lore"]
    assert _keys("town", hub) == ["shop-sell", "shop-find", "shop-sharpen", "shop-rush", "shop-duel", "shop-class", "auto"]

    # The admin page is the `idle admin` tier's.
    hub, _ctx_ = _hub(cog, guild, ADMIN)
    assert [key for key, _label in hub.pages()][-1] == "admin"
    assert _keys("admin", hub) == ["hog", "gold", "push", "move", "remove", "reset"]


async def test_the_actions_are_buttons_under_the_page_dropdown():
    cog, guild, _idle = _world()
    _spawn(ALICE, **MARKET, gold=50)
    hub, ctx = _hub(cog, guild)
    assert isinstance(hub.children[0], _PageSelect) and not any(isinstance(c, _ItemSelect) for c in hub.children)
    buttons = [c for c in hub.children if isinstance(c, _ItemButton)]
    assert [b.item.key for b in buttons] == _keys("common", hub)
    assert (str(buttons[0].emoji), buttons[0].label, buttons[0].row) == ("🔄", "Refresh", 1)
    store = next(b for b in buttons if b.item.key == "store")
    assert store.style is discord.ButtonStyle.primary
    # Store jumps to the town page, whose buttons are the market's.
    press = _interaction(guild, ALICE)
    await store.callback(press)
    assert hub.page == "town"
    keys = [c.item.key for c in hub.children if isinstance(c, _ItemButton)]
    assert keys[:2] == ["shop-find", "shop-potion"] and keys[-1] == "auto"
    # A form's button opens its modal; the rules page fills three rows.
    gamble = next(c for c in hub.children if isinstance(c, _ItemButton) and c.item.key == "gamble")
    press = _interaction(guild, ALICE)
    await gamble.callback(press)
    assert isinstance(press.response.send_modal.await_args.args[0], FormModal)
    hub.page = "rules"
    hub._build()
    rows = [c.row for c in hub.children if isinstance(c, _ItemButton)]
    assert len(rows) == len(_idle_cog._RULES_TOPICS) and set(rows) <= {1, 2, 3} and hub.children[-1].row == 4


async def test_the_sheet_page_carries_the_map_and_can_look_at_a_player():
    cog, guild, _idle = _world()
    _spawn(ALICE, **MARKET)
    _spawn(BOB, x=WILDS[0], y=WILDS[1], level=3)
    hub, ctx = _hub(cog, guild)
    assert [f.filename for f in await hub.attachments()] == [MAP_FILENAME]
    assert hub.embed().image.url == f"attachment://{MAP_FILENAME}"
    hub.page = "realm"
    assert await hub.attachments() == [] and hub.embed().image.url is None

    # Look at Bob: his sheet, back on Common, the map redrawn; My sheet returns to Alice's own.
    look = next(item for item in items_for("realm", hub) if item.key == "look")
    press = _interaction(guild, ALICE)
    await hub.on_pick(press, look, {"user": [BOB]})
    assert hub.page == "common" and hub.target == BOB and "Lv 3 Bard" in hub.embed().title
    kwargs = press.edit_original_response.await_args.kwargs
    assert [f.filename for f in kwargs["attachments"]] == [MAP_FILENAME] and kwargs["view"] is hub
    assert _keys("common", hub)[0] == "own"
    back = next(item for item in items_for("common", hub) if item.key == "own")
    await hub.on_pick(_interaction(guild, ALICE), back, None)
    assert hub.target == ALICE and "own" not in _keys("common", hub)
    # Someone with no character is said so, not drawn.
    await hub.on_pick(_interaction(guild, ALICE), look, {"user": [ADMIN]})
    assert hub.embed().title == "❌ No Character"
    assert ctx.sent_embeds == []                                     # nothing posted for any of it


async def test_a_pick_runs_the_typed_subcommand_and_keeps_the_reply_in_the_panel():
    cog, guild, _idle = _world()
    char = _spawn(ALICE, **MARKET, gold=1000)
    hub, ctx = _hub(cog, guild, page="town")
    items = {item.key: item for item in hub.items()}
    press = _interaction(guild, ALICE)
    await hub.on_pick(press, items["shop-find"], None)
    press.response.defer.assert_awaited_once()
    assert ctx.sent_embeds == []                                     # captured, not posted
    assert hub.last.title == "🛒 Idle Shop" and char["gold"] == 1000 - rpg.shop_prices(char)["find"]
    assert hub.embed().fields[-1].name == "🛒 Idle Shop" and hub.embed().fields[-1].value == hub.last.description
    press.edit_original_response.assert_awaited_once()

    # Plain items carry their arguments; a form's values become the typed ones.
    await hub.on_pick(_interaction(guild, ALICE), items["auto"], None)
    assert char["auto_trade"] is False and "**off**" in hub.last.description
    await hub.on_pick(_interaction(guild, ALICE), items["shop-potion"], {"n": ["2"]})
    assert char["potions"] == 2
    hub.page = "road"
    travel = next(item for item in hub.items() if item.key == "travel")
    await hub.on_pick(_interaction(guild, ALICE), travel, {"place": ["Denmark"]})
    assert char["travel_to"] == "Denmark" and hub.last.title == "🧭 Travel"
    hub.page = "character"
    align = next(item for item in hub.items() if item.key == "align")
    await hub.on_pick(_interaction(guild, ALICE), align, {"alignment": ["chaotic good"]})
    assert (char["law"], char["moral"]) == ("chaotic", "good")
    hub.page = "rules"
    await hub.on_pick(_interaction(guild, ALICE), hub.items()[0], None)
    assert hub.last.title == "📖 Idle RPG — Levels"
    # A refusal from the command lands in the same field.
    hub.page = "town"
    gamble = next(item for item in hub.items() if item.key == "gamble")
    await hub.on_pick(_interaction(guild, ALICE), gamble, {"stake": "lots"})
    assert hub.last.title == "❌ The Tables"
    assert ctx.sent_embeds == []


async def test_confirm_gated_picks_post_in_the_channel():
    cog, guild, _idle = _world()
    char = _spawn(ALICE, **MARKET, level=rpg.PRESTIGE_LEVEL)
    hub, ctx = _hub(cog, guild, page="character")
    prestige = next(item for item in hub.items() if item.key == "prestige")
    await hub.on_pick(_interaction(guild, ALICE), prestige, None)
    assert char["prestige"] == 1 and char["level"] == 0            # conftest auto-confirms
    assert ctx.sent_embeds[-1].title == "🌟 Prestige"                # posted, not captured
    assert hub.last.description == "Posted in the channel."


async def test_the_panel_applies_the_gates_a_direct_call_skips():
    _real_tiers()
    cog, guild, _idle = _world()
    bob = _spawn(BOB)
    before = bob["next_level_at"]
    # Alice is no admin: the page isn't listed, and a pick on its item is refused all the same.
    hub, _ctx_ = _hub(cog, guild)
    hog = next(item for item in items_for("admin", hub) if item.key == "hog")
    press = _interaction(guild, ALICE)
    await hub.on_pick(press, hog, {"user": [BOB]})
    assert press.followup.send.await_args.kwargs["ephemeral"] and "can't use" in press.followup.send.await_args.args[0]
    assert bob["next_level_at"] == before and hub.last is None
    # The admin runs it; a form handed no player is refused before anything runs.
    hub, _ctx_ = _hub(cog, guild, ADMIN, page="admin")
    await hub.on_pick(_interaction(guild, ADMIN), hog, {"user": [BOB]})
    assert bob["next_level_at"] != before and not hub.last.title.startswith("❌")
    press = _interaction(guild, ADMIN)
    await hub.on_pick(press, hog, {"user": []})
    assert press.followup.send.await_args.args[0] == "Pick a player first."


async def test_open_panel_posts_the_sheet_with_the_map_and_deletes_it_on_close():
    cog, guild, _idle = _world()
    _spawn(ALICE, **MARKET)
    hub, ctx = _hub(cog, guild)
    hub.wait = AsyncMock(return_value=False)
    await open_panel(ctx, hub)
    kwargs = ctx.send_mock.await_args.kwargs
    assert [f.filename for f in kwargs["files"]] == [MAP_FILENAME] and kwargs["view"] is hub
    assert "Lv 0 Bard" in kwargs["embed"].title


async def test_the_help_card_reads_topics_and_joins():
    cog, guild, idle = _world()
    ctx = _ctx(guild, channel=idle)
    await cog.cmd_rules.callback(cog, ctx)
    view = ctx.sent_views[-1]
    assert isinstance(view, _idle_cog._HelpView)
    select, join = view.children
    assert [option.value for option in select.options] == list(_idle_cog._RULES_TOPICS)
    reader = _interaction(guild, BOB)
    select._values = ["battles"]
    await select.callback(reader)
    kwargs = reader.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] and kwargs["embed"].title == "📖 Idle RPG — Battles"

    # Anyone may join from the card: a modal asks for the class.
    click = _interaction(guild, BOB, idle)
    await join.callback(click)
    modal = click.response.send_modal.await_args.args[0]
    assert isinstance(modal, _idle_cog._JoinModal)
    modal.class_name._value = "  Tax   Wizard "
    submit = _interaction(guild, BOB, idle)
    await modal.on_submit(submit)
    sent = submit.response.send_message.await_args.kwargs
    assert sent["embed"].title == "⚔️ A New Adventurer" and not sent["ephemeral"]
    assert _state.idle_characters[GID][BOB]["class"] == "Tax Wizard"
    idle.create_thread.assert_awaited_once()
    assert not idle.send.called                              # the card was in the idle channel: no second announcement

    # A refusal is private; a player already in the game gets one without a modal.
    again = _interaction(guild, BOB, idle)
    await join.callback(again)
    again.response.send_modal.assert_not_called()
    assert again.response.send_message.await_args.kwargs["ephemeral"]
    assert again.response.send_message.await_args.kwargs["embed"].title == "❌ Already Adventuring"
    bad = _idle_cog._JoinModal(cog)
    bad.class_name._value = "**bold**"
    refusal = _interaction(guild, ALICE, idle)
    await bad.on_submit(refusal)
    assert refusal.response.send_message.await_args.kwargs["ephemeral"]
    assert ALICE not in _state.idle_characters[GID]


async def test_the_join_button_says_when_the_game_is_off():
    cog, guild, idle = _world(channel=False)
    ctx = _ctx(guild)
    await cog.cmd_rules.callback(cog, ctx)
    join = ctx.sent_views[-1].children[1]
    click = _interaction(guild, BOB)
    await join.callback(click)
    click.response.send_modal.assert_not_called()
    assert click.response.send_message.await_args.kwargs["embed"].title == "💤 Idle RPG Is Off"


async def test_help_lists_the_idle_rpg_only_where_a_channel_is_set(db):
    from src.cogs.utility_cog import UtilityCog
    utility = UtilityCog(bot=None)
    _cog, guild, _idle = _world(channel=False)
    ctx = _ctx(guild)
    await utility.cmd_help.callback(utility, ctx)
    fields = {f.name: f.value for f in ctx.sent_embeds[-1].fields}
    assert "!idle" not in fields["🔧 Utility"]

    _cog, guild, _idle = _world()
    ctx = _ctx(guild)
    await utility.cmd_help.callback(utility, ctx)
    fields = {f.name: f.value for f in ctx.sent_embeds[-1].fields}
    assert "!idle" in fields["🔧 Utility"]
