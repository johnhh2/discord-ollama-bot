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
from src.permissions import get_command_perm

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember, FakeMessage, FakeTextChannel, FakeThread

pytestmark = pytest.mark.asyncio

GID, IDLE_CH = 1, 500
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
    return "\n".join(call.args[0] for call in dest.send.call_args_list)


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
    assert "reached **level 1**" in _sent(idle)
    thread = guild.threads[0]                     # made lazily: the character had none
    story = [call.args[0] for call in thread.send.call_args_list]
    assert len(story) == 2                        # the opening line, then one batched post
    assert "reached **level 1**" in story[1] and "found a level" in story[1]
    assert "found a level" not in _sent(idle)     # item finds stay in the feed
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
    char = _spawn(left=-(rpg.ttl(1) + rpg.ttl(2) + 5))
    await cog.tick()
    assert char["level"] == 3
    assert idle.send.await_count == 1


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
    _state.idle_quests[GID] = {"members": [ALICE, BOB], "description": "wait", "ends_at": now + 9000, "not_before": 0}
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

async def test_bare_idle_pitches_to_strangers_and_shows_the_sheet_to_players():
    cog, guild, _idle = _world()
    ctx = _ctx(guild)
    await cog.cmd_idle.callback(cog, ctx)
    assert "!idle join <class>" in ctx.sent_embeds[-1].description

    _spawn(level=7)
    await cog.cmd_idle.callback(cog, ctx)
    assert "Lv 7 Bard" in ctx.sent_embeds[-1].title


async def test_rules_stay_short_and_the_detail_lives_in_topics():
    cog, guild, _idle = _world()
    ctx = _ctx(guild)
    await cog.cmd_rules.callback(cog, ctx)
    card = ctx.sent_embeds[-1].description
    assert len(card) < 600 and card.count("\n") <= 10
    assert "!idle rules <levels|battles|alignment|quests|prestige>" in card
    assert "talk" not in card.lower()

    await cog.cmd_rules.callback(cog, ctx, "Quests")
    assert ctx.sent_embeds[-1].title == "📖 Idle RPG — Quests"
    await cog.cmd_rules.callback(cog, ctx, "nonsense")
    assert ctx.sent_embeds[-1].description == card


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
    assert "duelled" in _sent(idle)

    await cog.cmd_duel.callback(cog, ctx, member=guild.get_member(BOB))
    assert "today's duel" in ctx.sent_embeds[-1].description

    alice["duel_day"] = None
    rpg.pause(bob, int(time.time()))
    await cog.cmd_duel.callback(cog, ctx, member=guild.get_member(BOB))
    assert "offline" in ctx.sent_embeds[-1].description and alice["duel_day"] is None


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


# ── admin ────────────────────────────────────────────────────────────────────

@pytest.fixture
def _member_lookup(monkeypatch):
    async def _convert(self, ctx, argument):
        digits = argument.strip("<@!>")
        member = ctx.guild.get_member(int(digits)) if digits.isdigit() else None
        if member is None:
            raise discord.ext.commands.BadArgument("not found")
        return member
    monkeypatch.setattr(_idle_cog.MemberConverter, "convert", _convert)


async def test_admin_push_moves_a_clock_both_ways(_member_lookup):
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


async def test_admin_remove_reaches_someone_who_left_the_server(_member_lookup):
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


# ── persistence ──────────────────────────────────────────────────────────────

async def test_characters_and_quests_round_trip_through_the_db(db):
    char = _spawn(level=12, items={"ring": {"level": 9, "name": None}}, thread_id=900, law="chaotic")
    paused = _spawn(BOB)
    rpg.pause(paused, int(time.time()))
    _state.idle_quests[GID] = {"members": [ALICE, BOB], "description": "wait", "ends_at": 123, "not_before": 45}
    await _persistence.save_idle_character(GID, ALICE)
    await _persistence.save_idle_character(GID, BOB)
    await _persistence.save_idle_quest(GID)
    expected, expected_paused = dict(char), dict(paused)

    _state.idle_characters.clear()
    _state.idle_quests.clear()
    await _persistence.init_db_state()

    assert _state.idle_characters[GID][ALICE] == expected
    assert _state.idle_characters[GID][BOB] == expected_paused
    assert _state.idle_quests[GID]["members"] == [ALICE, BOB]

    await _persistence.delete_idle_guild(GID)
    _state.idle_characters.clear()
    await _persistence.init_db_state()
    assert GID not in _state.idle_characters
