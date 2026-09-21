"""!idle — a per-guild idle RPG. The rules live in src/idlerpg.py; this cog
is the Discord half: commands, the once-a-minute tick, the listeners that
track who is online, and the feed threads.

The game runs in the channel set with `!settings-channel idle` and is off
while none is set (every clock freezes). Server-wide news is posted in that
channel; each character also gets a public thread under it that carries
everything concerning them.

"Logged in" means seen online within the last hour (`idlerpg.GRACE_SECS`).
`last_seen` is stamped by presence updates, by the tick for anyone showing
non-offline, and by any message the player sends in the guild (which is what
keeps an invisible player in the game). A character not seen for an hour is
paused retroactively at `last_seen + GRACE_SECS`, so neither the tick's timing
nor a reboot hands out free minutes. Without the presence intent every
status reads offline, so the check is skipped and everyone counts as online.

All rule mutations are synchronous; awaits happen only when posting and
saving. Rows changed by the tick or a listener are queued in `_dirty` and
written once per tick rather than per event.
"""
from __future__ import annotations

import asyncio
import io
import logging
import random
import re
import time

import discord
from discord.ext import commands, tasks

import src.persistence as persistence
from src import idlerpg as rpg
from src import state
from src.confirm_view import confirm_prompt
from src.economy import _ct_today, next_daily_reset_ts
from src.guild_config import get_guild_cfg
from src.idle_map import MAP_FILENAME, render_map
from src.helpers import (
    emb, C_BLUE, C_GOLD, C_GREEN, C_GREY, C_RED,
    MemberConverter, format_duration, parse_duration, parse_int_amount,
)
from src.permissions import is_silenced
from src.settings_views import pick_from_list

log = logging.getLogger(__name__)

TICK_SECONDS = 60
TICKS_PER_DAY = 86_400 // TICK_SECONDS
# A late boot may owe a character many levels; past this the rest wait a tick.
MAX_LEVELS_PER_TICK = 25
SEEN_SAVE_SECS = 300            # how stale the persisted last_seen may get
THREAD_AUTO_ARCHIVE_MINUTES = 10080   # Discord's maximum; a send un-archives the thread anyway
THREAD_NAME_MAX = 100
# Discord allows two renames per thread per ten minutes (see CLAUDE.md:
# Gambling threads) — titles are brought up to date by the tick, never inline.
RENAME_INTERVAL = 300.0
MESSAGE_MAX = 1900
LEADERBOARD_SIZE = 10
CLASS_MAX = 30
_CLASS_RE = re.compile(r"[\w][\w '\-]*")
NOT_YOURS = "Not your prompt."
WAGER_ACCEPT_SECS = 120.0
NO_MENTIONS = discord.AllowedMentions.none()

ALIGN_EFFECTS = (
    "**Good** +10% item power in battle, prayers with other good players, rarer critical strikes.\n"
    "**Evil** −10% item power, more critical strikes, a chance to rob the good — or be forsaken.\n"
    "**Lawful** half as many random events, good and bad. **Chaotic** twice as many."
)

# `!idle rules <topic>` — the bare command stays a few lines on purpose.
_RULES_TOPICS = {
    "levels": (
        f"Level 1 takes {format_duration(rpg.ttl(0))}; each level after takes {int((rpg.LEVEL_MULT - 1) * 100)}% longer.\n"
        f"The timer runs while you've been online in the last {format_duration(rpg.GRACE_SECS)} — idle and do-not-disturb count. "
        "After that it pauses and picks up where it stopped. Being away never costs you anything."
    ),
    "battles": (
        f"Each level-up finds an item for one of ten slots and may start a fight (always, from level {rpg.BATTLE_ALWAYS_LEVEL}).\n"
        "Each side rolls a number from 0 up to their item power (`!idle items`), shown as \"rolled 12 of 40\"; the higher roll wins. "
        "Win and your timer shrinks; lose and it grows.\n"
        f"`!idle duel @user` once a day: the loser hands {rpg.DUEL_PCT}% of their timer to the winner. A tie is a coin toss."
    ),
    "monsters": (
        "Out in the wilds your character runs into monsters — rats and bandits on the plains, trolls and dragons in the mountains, "
        "worse in the caves, the haunted ground and T'rnalvph, where the rewards are better too. Towns (the rings on `!idle map`) are safe.\n"
        "A monster is sized to you, so gear alone doesn't make them easy; its prefix (Veteran, Elite … Corrupted) and kind set how much harder. "
        "Both sides roll up to their power. Win: gold, time off your clock, sometimes an item. Too close to call: someone flees, nothing lost. "
        f"Lose: time added, some gold (1/{rpg.MOB_DEATH_GOLD_DIVISOR} of it, capped by your level), and you're carried to the nearest town's outskirts.\n"
        f"From level 11 a quarter of fights are against a group. Up to level {rpg.MOB_EASY_LEVEL} monsters fight at half strength."
    ),
    "map": (
        f"The realm is a {rpg.MAP_SIZE}×{rpg.MAP_SIZE} grid. Everyone online wanders one step a second, and the edges wrap.\n"
        "Land on the same square as someone and you may fight them, there and then.\n"
        "Some quests are journeys: the party stops wandering and walks to one landmark, then another. `!idle map` shows it all.\n"
        "`!idle travel <town>` walks you to a town on purpose — slowly, and without the chance meetings of the road."
    ),
    "gold": (
        "The realm's own money — nothing to do with the server's coins, and it can't be sent to anyone.\n"
        "You earn it by levelling, winning fights and finishing quests; a collision fight's winner also lifts "
        f"{rpg.GOLD_SPOILS_PCT}% of the loser's purse.\n"
        f"`!idle shop` spends it, but only within {rpg.MARKET_RADIUS} squares of a town (the rings on `!idle map`): an extra item find, "
        "sharpening an item, a once-a-day rush, a second duel, a new class. Walk right into a town and your character "
        f"trades on its own with up to {rpg.AUTO_TRADE_BUDGET_PCT}% of its gold — `!idle shop auto off` stops that. "
        "`!idle duel @user 200` bets gold, anywhere."
    ),
    "alignment": ALIGN_EFFECTS + "\nSet it with `!idle align`, once a day.",
    "quests": (
        f"Now and then, {rpg.QUEST_MIN_PARTY}–{rpg.QUEST_MAX_PARTY} online players of level {rpg.QUEST_MIN_LEVEL}+ are sent on a 12–24 hour quest.\n"
        f"Finish and each quester's timer drops {rpg.QUEST_REWARD_PCT}%. There is nothing to do, and nothing to get wrong."
    ),
    "prestige": (
        f"At level {rpg.PRESTIGE_LEVEL}, `!idle prestige` sends you back to level 0 without your items. "
        f"You keep a ★ and level {rpg.PRESTIGE_BONUS_PCT}% faster for good (up to {rpg.PRESTIGE_MAX_RANKS} ranks)."
    ),
}


def _clean_class(text: str) -> "str | None":
    """A class name fit to print in announcements, or None."""
    text = " ".join(text.split())
    return text if len(text) <= CLASS_MAX and _CLASS_RE.fullmatch(text) else None


class IdleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rng = random.Random()
        self._dirty: set = set()            # (guild_id, uid) rows to save at the next flush
        self._dirty_quests: set = set()
        self._pending: dict = {}            # guild_id -> notes queued by listeners
        self._seen_saved: dict = {}
        self._creating: set = set()         # (guild_id, uid) with a thread being created
        self._rename_due: set = set()
        self._renamed_at: dict = {}

    async def cog_load(self):
        # Started here (not __init__) so tests constructing the cog don't spawn the loop.
        self._loop.start()

    def cog_unload(self):
        self._loop.cancel()

    # ── lookups ──────────────────────────────────────────────────────────

    @staticmethod
    def _chars(guild_id: int) -> dict:
        return state.idle_characters.setdefault(guild_id, {})

    @staticmethod
    def _quest(guild_id: int) -> dict:
        return state.idle_quests.setdefault(guild_id, rpg.new_quest())

    @staticmethod
    def _channel(guild):
        cid = get_guild_cfg(guild.id).get("idle_channel")
        return guild.get_channel(cid) if cid else None

    @staticmethod
    def _namer(guild):
        def name(uid: int) -> str:
            member = guild.get_member(uid)
            return f"**{discord.utils.escape_markdown(member.display_name)}**" if member else f"<@{uid}>"
        return name

    def _online(self, guild, uid: int) -> bool:
        if not getattr(getattr(self.bot, "intents", None), "presences", False):
            return True
        member = guild.get_member(uid)
        return member is not None and str(member.status) != "offline"

    def _title(self, guild, uid: int, char: dict) -> str:
        member = guild.get_member(uid)
        who = member.display_name if member else str(uid)
        stars = "★" * char["prestige"] + " " if char["prestige"] else ""
        return f"{stars}{who} — Lv {char['level']} {char['class']}"[:THREAD_NAME_MAX]

    def _seen(self, guild, uid: int, char: dict, now: int) -> None:
        """The player is provably here: stamp them, and wake a paused clock."""
        char["last_seen"] = now
        key = (guild.id, uid)
        if now - self._seen_saved.get(key, 0) >= SEEN_SAVE_SECS:
            self._seen_saved[key] = now
            self._dirty.add(key)
        if rpg.is_paused(char) and self._channel(guild) is not None:
            rpg.resume(char, now)
            self._dirty.add(key)
            self._pending.setdefault(guild.id, []).append(rpg.Note(
                (uid,),
                f"▶️ {self._namer(guild)(uid)} is back. The clock resumes with "
                f"{format_duration(rpg.time_left(char, now))} to level {char['level'] + 1}.",
            ))

    # ── the tick ─────────────────────────────────────────────────────────

    @tasks.loop(seconds=TICK_SECONDS)
    async def _loop(self):
        # tasks.loop stops for good on an unhandled exception.
        try:
            await self.tick()
        except Exception:
            log.exception("idle: tick failed")

    @_loop.before_loop
    async def _before_loop(self):
        # wait_until_ready gates only the gateway; init_db_state, which loads
        # state.idle_characters, finishes later inside on_ready.
        await self.bot.wait_until_ready()
        await persistence.init_done.wait()

    async def tick(self, now: "int | None" = None) -> None:
        now = int(time.time() if now is None else now)
        for gid in list(state.idle_characters):
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            notes = self._advance(guild, now)
            await self._deliver(guild, notes)
            await self._sweep_renames(guild)
        await self._flush()

    def _advance(self, guild, now: int) -> list:
        """One tick of the rules for one guild. Synchronous on purpose: no
        command or listener can interleave with a half-applied tick."""
        gid = guild.id
        chars, quest = self._chars(gid), self._quest(gid)
        enabled = self._channel(guild) is not None
        name = self._namer(guild)
        pace = rpg.PACES.get(get_guild_cfg(gid).get("idle_pace"), rpg.PACES[rpg.DEFAULT_PACE])
        notes: list = []

        for uid, char in list(chars.items()):
            if self._online(guild, uid):
                self._seen(guild, uid, char, now)
            if rpg.is_paused(char):
                continue
            if not enabled:
                rpg.pause(char, now)
                self._dirty.add((gid, uid))
                continue
            here = rpg.logged_in(char, now)
            horizon = now if here else char["last_seen"] + rpg.GRACE_SECS
            for _ in range(MAX_LEVELS_PER_TICK):
                if char["next_level_at"] > horizon:
                    break
                rpg.level_up(char)
                earned = rpg.level_gold(char["level"])
                char["gold"] += earned
                self._rename_due.add((gid, uid))
                notes.append(rpg.Note(
                    (uid,),
                    f"🎉 {name(uid)} the {char['class']} reached **level {char['level']}**! +{earned:,} gold. "
                    f"The next one takes {format_duration(rpg.ttl(char['level'], char['prestige']))}.",
                    rpg.level_is_news(char["level"], char["prestige"]),
                ))
                notes.append(rpg.find_item(uid, char, self.rng, name))
                notes += rpg.level_up_battle(uid, chars, self.rng, name, now, pace)
            if not here:
                rpg.pause(char, horizon)
                self._dirty.add((gid, uid))
                notes.append(rpg.Note(
                    (uid,),
                    f"⏸️ {name(uid)} hasn't been seen for an hour. Their clock is paused with "
                    f"{format_duration(char['remaining'])} to go.",
                ))
            else:
                notes += rpg.random_events(uid, chars, self.rng, name, now, TICKS_PER_DAY, pace)

        if enabled:
            if self.rng.random() < rpg.TEAM_BATTLE_PER_DAY / TICKS_PER_DAY:
                notes += rpg.team_battle(chars, self.rng, name, now, pace)
            before = dict(quest)
            # Positions ride along with whatever else saves the row (at worst
            # the five-minute last_seen write) — never a write per step.
            notes += rpg.move_players(chars, quest, self.rng, name, now, TICK_SECONDS)
            for uid in rpg.running(chars):
                errand = rpg.auto_trade(uid, chars[uid], self.rng, name, now)
                if errand:
                    notes.append(errand)
                if self.rng.random() < pace.mob_fights_per_day / TICKS_PER_DAY:
                    notes += rpg.mob_encounter(uid, chars[uid], self.rng, name, now)
            notes += rpg.tick_quest(chars, quest, self.rng, name, now)
            if quest != before:
                self._dirty_quests.add(gid)

        notes += self._pending.pop(gid, [])

        for note in notes:
            self._dirty.update((gid, uid) for uid in note.uids)
        return notes

    async def _flush(self) -> None:
        for gid, uid in list(self._dirty):
            self._dirty.discard((gid, uid))
            if uid not in state.idle_characters.get(gid, {}):
                continue
            try:
                await persistence.save_idle_character(gid, uid)
            except Exception:
                log.exception("idle: save of %s/%s failed", gid, uid)
                self._dirty.add((gid, uid))
        for gid in list(self._dirty_quests):
            self._dirty_quests.discard(gid)
            if gid in state.idle_quests:
                await persistence.save_idle_quest(gid)

    # ── posting ──────────────────────────────────────────────────────────

    async def _deliver(self, guild, notes: list, *, skip_main: bool = False) -> None:
        """Everything for one destination goes out as one message (chunked),
        so a busy tick costs one send per thread, not one per event."""
        channel = self._channel(guild)
        if channel is None or not notes:
            return
        if not skip_main:
            public = [n for n in notes if n.public]
            # Pings land in the channel only; the same line in a feed thread
            # shows the name without mentioning anyone a second time.
            await self._send(channel, [n.text for n in public], ping={uid for n in public for uid in n.ping})
            if any(n.show_map for n in public):
                await self._post_map(guild, channel)
        feeds: dict = {}
        for note in notes:
            for uid in note.uids:
                feeds.setdefault(uid, []).append(note.text)
        for uid, lines in feeds.items():
            thread = await self._thread_for(guild, channel, uid)
            if thread is not None:
                await self._send(thread, lines)

    async def _map_file(self, guild, highlight=()) -> discord.File:
        """The realm as a PNG: every character, the landmarks, and a running
        journey's waypoints. Drawn off the event loop."""
        players = []
        for uid, char in self._chars(guild.id).items():
            if char.get("x") is None:
                continue
            member = guild.get_member(uid)
            players.append((uid, member.display_name if member else str(uid), char["x"], char["y"]))
        quest = self._quest(guild.id)
        journey = dict(quest) if quest.get("kind") == "journey" else None
        # Only the travellers this picture is about: everyone's lines would bury the map.
        routes = []
        for uid in highlight:
            char = self._chars(guild.id).get(uid)
            if char and char.get("travel_to") in rpg.LANDMARKS and char.get("x") is not None:
                routes.append((char["x"], char["y"], *rpg.LANDMARKS[char["travel_to"]]))
        png = await asyncio.to_thread(render_map, players, highlight=tuple(highlight), quest=journey, routes=routes)
        return discord.File(io.BytesIO(png), filename=MAP_FILENAME)

    async def _post_map(self, guild, channel) -> None:
        try:
            await channel.send(file=await self._map_file(guild), silent=True, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as e:
            log.warning("idle: map post to %s failed (%s)", channel.id, type(e).__name__)

    async def _send_with_map(self, ctx, embed: discord.Embed, highlight=()) -> None:
        embed.set_image(url=f"attachment://{MAP_FILENAME}")
        await ctx.send(embed=embed, file=await self._map_file(ctx.guild, highlight))

    @staticmethod
    async def _send(dest, lines: list, ping=()) -> None:
        """Always silent. `ping` is the only way a post mentions anyone, and
        even then nobody is notified — they get the mention badge."""
        mentions = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=u) for u in ping]) if ping else NO_MENTIONS
        chunk = ""
        chunks = []
        for line in lines:
            if chunk and len(chunk) + len(line) + 1 > MESSAGE_MAX:
                chunks.append(chunk)
                chunk = ""
            chunk = f"{chunk}\n{line}" if chunk else line[:MESSAGE_MAX]
        if chunk:
            chunks.append(chunk)
        for text in chunks:
            try:
                await dest.send(text, silent=True, allowed_mentions=mentions)
            except discord.HTTPException as e:
                log.warning("idle: send to %s failed (%s)", getattr(dest, "id", "?"), type(e).__name__)
                return

    async def _thread_for(self, guild, channel, uid: int):
        """The character's feed thread under `channel`, made (or remade — it
        was deleted, or the idle channel moved) when there isn't one."""
        char = self._chars(guild.id).get(uid)
        if char is None:
            return None
        tid = char["thread_id"]
        if tid:
            thread = guild.get_thread(tid)
            if thread is None:
                try:
                    # Archived threads drop out of the cache; a send revives them.
                    thread = await guild.fetch_channel(tid)
                except discord.NotFound:
                    thread = None
                except discord.HTTPException:
                    return None   # transient — skip this post rather than open a second thread
            if thread is not None and getattr(thread, "parent_id", None) == channel.id:
                return thread
        return await self._create_thread(guild, channel, uid, char)

    async def _create_thread(self, guild, channel, uid: int, char: dict):
        key = (guild.id, uid)
        create = getattr(channel, "create_thread", None)
        if create is None or key in self._creating:
            return None
        self._creating.add(key)
        try:
            thread = await create(
                name=self._title(guild, uid, char),
                type=discord.ChannelType.public_thread,
                auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES,
            )
        except discord.HTTPException as e:
            log.warning("idle: create_thread in %s failed (%s)", channel.id, type(e).__name__)
            return None
        finally:
            self._creating.discard(key)
        if self._chars(guild.id).get(uid) is not char:
            return None   # the character was deleted while the thread was being made
        char["thread_id"] = thread.id
        await persistence.save_idle_character(guild.id, uid)
        member = guild.get_member(uid)
        if member is not None:
            try:
                await thread.add_user(member)
            except discord.HTTPException:
                pass  # best-effort: the thread is public either way
        await self._send(thread, [
            f"📖 This is {self._namer(guild)(uid)}'s story. Everything that happens to them lands here.\n"
            "There is nothing to do but stay online. `!idle rules` has the details.",
        ])
        return thread

    async def _archive_thread(self, guild, thread_id: "int | None") -> None:
        thread = guild.get_thread(thread_id) if thread_id else None
        if thread is None:
            return
        try:
            await thread.edit(archived=True)
        except discord.HTTPException:
            pass  # best-effort: Discord archives it after a week idle anyway

    async def _sweep_renames(self, guild) -> None:
        now = time.monotonic()
        for key in [k for k in self._rename_due if k[0] == guild.id]:
            gid, uid = key
            char = self._chars(gid).get(uid)
            thread = guild.get_thread(char["thread_id"]) if char and char["thread_id"] else None
            if thread is None:
                self._rename_due.discard(key)
                continue
            if now - self._renamed_at.get(key, -RENAME_INTERVAL) < RENAME_INTERVAL:
                continue
            self._rename_due.discard(key)
            title = self._title(guild, uid, char)
            if title == thread.name:
                continue
            # The slot is spent whether or not the edit lands.
            self._renamed_at[key] = now
            try:
                await thread.edit(name=title)
            except discord.HTTPException as e:
                log.warning("idle: rename of thread %s failed (%s)", thread.id, type(e).__name__)
                self._rename_due.add(key)

    # ── listeners ────────────────────────────────────────────────────────

    def _penalize(self, guild, uid: int, base: int, now: int) -> int:
        self._dirty.add((guild.id, uid))
        return rpg.penalize(self._chars(guild.id)[uid], base, now)

    @commands.Cog.listener()
    async def on_presence_update(self, before, after):
        char = state.idle_characters.get(after.guild.id, {}).get(after.id)
        if char is None:
            return
        now = int(time.time())
        if str(after.status) != "offline":
            self._seen(after.guild, after.id, char, now)
        elif str(before.status) != "offline":
            # The last moment they were provably here; the grace hour runs from it.
            char["last_seen"] = now
            self._dirty.add((after.guild.id, after.id))

    @commands.Cog.listener()
    async def on_message(self, message):
        guild = message.guild
        if guild is None or message.author.bot:
            return
        uid = message.author.id
        char = state.idle_characters.get(guild.id, {}).get(uid)
        if char is None or is_silenced(uid, guild.id):
            return
        # A message anywhere in the guild proves presence — it's what keeps an
        # invisible player logged in.
        self._seen(guild, uid, char, int(time.time()))

    @commands.Cog.listener()
    async def on_thread_member_remove(self, member):
        thread = member.thread
        guild = thread.guild
        char = state.idle_characters.get(guild.id, {}).get(member.id)
        # Leaving the server also drops them from the thread; that's the quit
        # penalty's business (on_member_remove), not a second one here.
        if char is None or char["thread_id"] != thread.id or guild.get_member(member.id) is None:
            return
        seconds = self._penalize(guild, member.id, rpg.PEN_PART, int(time.time()))
        self._pending.setdefault(guild.id, []).append(rpg.Note(
            (member.id,),
            f"🚪 {self._namer(guild)(member.id)} walked out on their own story. {format_duration(seconds)} added to their clock.",
        ))

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        char = state.idle_characters.get(member.guild.id, {}).get(member.id)
        if char is None:
            return
        seconds = self._penalize(member.guild, member.id, rpg.PEN_QUIT, int(time.time()))
        self._pending.setdefault(member.guild.id, []).append(rpg.Note(
            (member.id,),
            f"🏃 **{discord.utils.escape_markdown(member.display_name)}** fled the realm. "
            f"{format_duration(seconds)} added to their clock.",
            True,
        ))

    # ── command plumbing ─────────────────────────────────────────────────

    async def _ready(self, ctx, *, need_channel: bool = False) -> bool:
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "The idle RPG is played in a server.", C_RED))
            return False
        if need_channel and self._channel(ctx.guild) is None:
            await ctx.send(embed=emb(
                "💤 Idle RPG Is Off",
                "No idle channel is set here. An admin can turn the game on with `!settings-channel idle #channel`.",
                C_GREY,
            ))
            return False
        return True

    async def _own_char(self, ctx) -> "dict | None":
        char = self._chars(ctx.guild.id).get(ctx.author.id)
        if char is None:
            await ctx.send(embed=emb("❌ No Character", "You have no character here. `!idle join <class>` makes one.", C_RED))
            return None
        self._seen(ctx.guild, ctx.author.id, char, int(time.time()))
        return char

    async def _target_uid(self, ctx, text: "str | None") -> "int | None":
        """A member, or the raw id of someone who already left the server."""
        if not text:
            return None
        try:
            return (await MemberConverter().convert(ctx, text)).id
        except commands.BadArgument:
            digits = text.strip("<@!>")
            return int(digits) if digits.isdigit() else None

    def _sheet(self, guild, uid: int, char: dict, now: int) -> discord.Embed:
        if not rpg.is_paused(char):
            clock = f"<t:{char['next_level_at']}:R>"
        elif self._channel(guild) is None:
            clock = f"⏸️ paused, {format_duration(char['remaining'])} left — the game is switched off"
        else:
            clock = f"⏸️ paused, {format_duration(char['remaining'])} left — last seen <t:{char['last_seen']}:R>"
        lines = [
            f"**Alignment:** {rpg.alignment_label(char)}",
            f"**Level {char['level'] + 1}:** {clock}",
            f"**Item power:** {rpg.item_sum(char):,} · **Gold:** {char['gold']:,}",
            f"**Adventuring since:** <t:{char['created_at']}:D>",
        ]
        if char.get("x") is not None:
            here = rpg.landmark_at((char["x"], char["y"]))
            town, away = rpg.nearest_town(char)
            market = f"market open ({town})" if away <= rpg.MARKET_RADIUS else f"nearest market: {town}, {away} squares"
            where = f"at {here}" if here else rpg.biome_at(char["x"], char["y"])
            lines.insert(2, f"**Position:** [{char['x']}, {char['y']}] — {where} · {market}")
            if char.get("travel_to") in rpg.LANDMARKS:
                eta = format_duration(rpg.travel_eta_secs(char, char["travel_to"]))
                lines.insert(3, f"**Travelling to:** {char['travel_to']} — about {eta} of walking left")
        if char["mob_kills"] or char["mob_deaths"]:
            lines.append(f"**Monsters slain:** {char['mob_kills']:,} · **Struck down:** {char['mob_deaths']:,}")
        if char["penalty_total"]:
            lines.insert(3, f"**Time lost to penalties:** {format_duration(char['penalty_total'])}")
        if char["prestige"]:
            lines.insert(0, f"**Prestige:** {'★' * char['prestige']}")
        if char["thread_id"]:
            lines.append(f"**Feed:** <#{char['thread_id']}>")
        return emb(f"⚔️ {self._title(guild, uid, char)}", "\n".join(lines), C_BLUE)

    # ── !idle ────────────────────────────────────────────────────────────

    @commands.group(name="idle", aliases=["irpg"], invoke_without_command=True)
    async def cmd_idle(self, ctx: commands.Context):
        """!idle join|status|items|map|travel|shop|top|align|duel|quest|prestige|leave|rules"""
        if not await self._ready(ctx):
            return
        char = self._chars(ctx.guild.id).get(ctx.author.id)
        if char is None:
            await ctx.send(embed=emb(
                "⚔️ Idle RPG",
                "A game you win by doing nothing. Your character levels up while you're online and idle; "
                "items, battles and quests happen on their own.\n\n"
                "`!idle join <class>` to start · `!idle rules` for how it works · `!idle top` for the ladder",
                C_BLUE,
            ))
            return
        now = int(time.time())
        self._seen(ctx.guild, ctx.author.id, char, now)
        await self._send_with_map(ctx, self._sheet(ctx.guild, ctx.author.id, char, now), highlight=(ctx.author.id,))

    @cmd_idle.command(name="join")
    async def cmd_join(self, ctx: commands.Context, *, class_name: str = None):
        if not await self._ready(ctx, need_channel=True):
            return
        gid, uid = ctx.guild.id, ctx.author.id
        chars = self._chars(gid)
        if uid in chars:
            feed = f" Your feed is <#{chars[uid]['thread_id']}>." if chars[uid]["thread_id"] else ""
            await ctx.send(embed=emb("❌ Already Adventuring", f"You already have a character here.{feed}", C_RED))
            return
        if not class_name:
            await ctx.send(embed=emb(
                "❌ Pick a Class",
                f"Say what you are: `!idle join <class>`. It's yours to invent, up to {CLASS_MAX} characters — "
                "`!idle join Drunken Bard`, `!idle join Tax Wizard`.",
                C_RED,
            ))
            return
        class_name = _clean_class(class_name)
        if class_name is None:
            await ctx.send(embed=emb(
                "❌ Pick a Class",
                f"A class is up to {CLASS_MAX} characters: letters, digits, spaces, `'` and `-`.",
                C_RED,
            ))
            return

        now = int(time.time())
        char = rpg.new_character(class_name, now)
        rpg.ensure_position(char, self.rng)
        chars[uid] = char   # claimed before the first await: a second !idle join sees it
        await persistence.save_idle_character(gid, uid)
        channel = self._channel(ctx.guild)
        thread = await self._create_thread(ctx.guild, channel, uid, char)
        name = self._namer(ctx.guild)(uid)
        first = format_duration(rpg.ttl(0))
        if thread is not None:
            where = f"Your story unfolds in {thread.mention}."
        else:
            where = f"I couldn't open your feed thread — check that I have **Create Public Threads** in {channel.mention}. You're in the game regardless."
        await ctx.send(embed=emb(
            "⚔️ A New Adventurer",
            f"{name} the {class_name} sets out. Level 1 is {first} away.\n{where}",
            C_GREEN,
        ))
        if not self._in_idle_channel(ctx):
            await self._send(channel, [f"🆕 {name} the {class_name} has joined the realm. Level 1 in {first}."])

    @cmd_idle.command(name="status", aliases=["info"])
    async def cmd_status(self, ctx: commands.Context, *, member: MemberConverter = None):
        if not await self._ready(ctx):
            return
        target = member or ctx.author
        char = self._chars(ctx.guild.id).get(target.id)
        if char is None:
            await ctx.send(embed=emb("❌ No Character", f"{target.display_name} has no character here.", C_RED))
            return
        now = int(time.time())
        if target.id == ctx.author.id:
            self._seen(ctx.guild, target.id, char, now)
        await self._send_with_map(ctx, self._sheet(ctx.guild, target.id, char, now), highlight=(target.id,))

    @cmd_idle.command(name="map")
    async def cmd_map(self, ctx: commands.Context):
        if not await self._ready(ctx):
            return
        chars = self._chars(ctx.guild.id)
        quest = self._quest(ctx.guild.id)
        body = f"{len(chars)} adventurer{'' if len(chars) == 1 else 's'} in the realm. Everyone online wanders a step a second; meet someone on the same square and you may fight."
        if quest.get("kind") == "journey":
            body += f"\n📜 A party is on a journey — waypoint {quest['stage']} of 2 (`!idle quest`)."
        await self._send_with_map(ctx, emb("🗺️ The Realm", body, C_BLUE), highlight=(ctx.author.id,))

    @cmd_idle.command(name="items")
    async def cmd_items(self, ctx: commands.Context, *, member: MemberConverter = None):
        if not await self._ready(ctx):
            return
        target = member or ctx.author
        char = self._chars(ctx.guild.id).get(target.id)
        if char is None:
            await ctx.send(embed=emb("❌ No Character", f"{target.display_name} has no character here.", C_RED))
            return
        lines = []
        for slot in rpg.ITEM_SLOTS:
            item = char["items"].get(slot)
            if item is None:
                lines.append(f"**{slot.title()}:** —")
            elif item.get("name"):
                lines.append(f"**{slot.title()}:** ✨ {item['name']} (level {item['level']})")
            else:
                lines.append(f"**{slot.title()}:** level {item['level']}")
        lines.append(f"\n**Item power:** {rpg.item_sum(char):,} · **Gold:** {char['gold']:,}")
        await ctx.send(embed=emb(f"{self._title(ctx.guild, target.id, char)} — Items", "\n".join(lines), C_BLUE))

    @cmd_idle.command(name="top")
    async def cmd_top(self, ctx: commands.Context):
        if not await self._ready(ctx):
            return
        now = int(time.time())
        ranked = sorted(self._chars(ctx.guild.id).items(), key=rpg.rank_key(now))[:LEADERBOARD_SIZE]
        if not ranked:
            await ctx.send(embed=emb("🏔️ Idle Ladder", "Nobody is adventuring here yet. `!idle join <class>`.", C_GREY))
            return
        name = self._namer(ctx.guild)
        lines = []
        for i, (uid, char) in enumerate(ranked, 1):
            stars = "★" * char["prestige"] + " " if char["prestige"] else ""
            clock = "⏸️ paused" if rpg.is_paused(char) else f"next <t:{char['next_level_at']}:R>"
            lines.append(f"**{i}.** {name(uid)} — {stars}Lv {char['level']} {char['class']} · {clock}")
        await ctx.send(embed=emb("🏔️ Idle Ladder", "\n".join(lines), C_GOLD))

    @cmd_idle.command(name="align", aliases=["alignment"])
    async def cmd_align(self, ctx: commands.Context, *, alignment: str = None):
        if not await self._ready(ctx) or await self._own_char(ctx) is None:
            return
        if alignment is None:
            picked = await pick_from_list(
                ctx,
                title="⚖️ Alignment",
                description=f"{ALIGN_EFFECTS}\n\nYou can change once a day. Typed form: `!idle align chaotic good`.",
                options=[(rpg.alignment_label({"law": law, "moral": moral}).title(), f"{law} {moral}") for law, moral in rpg.ALIGNMENTS],
                placeholder="Pick an alignment…",
                multi=False,
            )
            if not picked:
                return
            alignment = picked[0]
        parsed = rpg.parse_alignment(alignment)
        if parsed is None:
            await ctx.send(embed=emb(
                "❌ Alignment",
                "Use a law and a moral — `!idle align lawful good`, `!idle align chaotic`, `!idle align neutral`.",
                C_RED,
            ))
            return
        # The dropdown was a long await — read the character again.
        char = self._chars(ctx.guild.id).get(ctx.author.id)
        if char is None:
            return
        now = int(time.time())
        if (char["law"], char["moral"]) == parsed:
            await ctx.send(embed=emb("⚖️ Alignment", f"You're already {rpg.alignment_label(char)}.", C_GREY))
            return
        ready_at = char["align_changed_at"] + rpg.ALIGN_COOLDOWN_SECS
        if now < ready_at:
            await ctx.send(embed=emb("❌ Alignment", f"A change of heart takes time — you can switch again <t:{ready_at}:R>.", C_RED))
            return
        char["law"], char["moral"] = parsed
        char["align_changed_at"] = now
        await persistence.save_idle_character(ctx.guild.id, ctx.author.id)
        await ctx.send(embed=emb("⚖️ Alignment", f"You are now **{rpg.alignment_label(char)}**.\n\n{ALIGN_EFFECTS}", C_GREEN))

    @cmd_idle.command(name="duel")
    async def cmd_duel(self, ctx: commands.Context, member: MemberConverter = None, wager: str = None):
        if not await self._ready(ctx, need_channel=True):
            return
        char = await self._own_char(ctx)
        if char is None:
            return
        if member is None:
            await ctx.send(embed=emb("❌ Usage", "`!idle duel @user [gold]` — one challenge a day; add an amount to bet gold on it.", C_RED))
            return
        gid, uid = ctx.guild.id, ctx.author.id
        chars = self._chars(gid)
        target = chars.get(member.id)
        problem = None
        if member.id == uid:
            problem = "You can't duel yourself."
        elif target is None:
            problem = f"{member.display_name} has no character here."
        elif rpg.is_paused(target):
            problem = f"{member.display_name} is offline — their clock is paused, and so are they."
        elif char["duel_day"] == _ct_today():
            problem = (
                f"You've had today's duel. The next one is ready <t:{next_daily_reset_ts()}:R> "
                "— or buy a second with `!idle shop duel`."
            )
        stake = 0
        if problem is None and wager is not None:
            stake = parse_int_amount(wager) or 0
            if stake <= 0:
                problem = "The wager is an amount of gold — `!idle duel @user 200`."
            elif char["gold"] < stake:
                problem = f"You only have {char['gold']:,} gold."
            elif target["gold"] < stake:
                problem = f"{member.display_name} only has {target['gold']:,} gold."
        if problem:
            await ctx.send(embed=emb("❌ Duel", problem, C_RED))
            return
        prior_day = char["duel_day"]
        char["duel_day"] = _ct_today()   # claimed before the prompt: a second !idle duel sees it
        if stake:
            accepted = await confirm_prompt(
                ctx,
                title="🤺 Wagered Duel",
                description=(
                    f"{member.mention} — {ctx.author.mention} challenges you to a duel for **{stake:,} gold**. "
                    "The winner takes the gold, and a slice of the loser's clock as usual."
                ),
                payer=member,
                timeout=WAGER_ACCEPT_SECS,
                not_yours="Only the challenged player can answer.",
            )
            # The prompt was a long await: both characters and both purses again.
            still_on = (
                accepted and chars.get(uid) is char and chars.get(member.id) is target
                and not rpg.is_paused(target) and char["gold"] >= stake and target["gold"] >= stake
            )
            if not still_on:
                if chars.get(uid) is char:
                    char["duel_day"] = prior_day
                if accepted:
                    await ctx.send(embed=emb("❌ Duel", "The duel fell through — someone can no longer cover the wager.", C_RED))
                return
        notes = rpg.duel(uid, member.id, chars, self.rng, self._namer(ctx.guild), int(time.time()), stake)
        await persistence.save_idle_character(gid, uid)
        await persistence.save_idle_character(gid, member.id)
        await ctx.send(embed=emb("🤺 Duel", "\n".join(n.text for n in notes), C_GOLD))
        await self._deliver(ctx.guild, notes, skip_main=self._in_idle_channel(ctx))

    # ── !idle travel ─────────────────────────────────────────────────────

    @cmd_idle.command(name="travel")
    async def cmd_travel(self, ctx: commands.Context, *, where: str = None):
        if not await self._ready(ctx, need_channel=True):
            return
        char = await self._own_char(ctx)
        if char is None:
            return
        gid, uid = ctx.guild.id, ctx.author.id
        rpg.ensure_position(char, self.rng)
        if where is None:
            options = [
                (f"{town} — {rpg.travel_steps(char, town)} squares, about {format_duration(rpg.travel_eta_secs(char, town))}", town)
                for town in sorted(rpg.TOWNS, key=lambda t: rpg.travel_steps(char, t))
            ]
            if char.get("travel_to"):
                options.append(("Stop travelling — wander again", "stop"))
            going = f"You're walking to **{char['travel_to']}**." if char.get("travel_to") else "You're wandering."
            picked = await pick_from_list(
                ctx,
                title="🧭 Travel",
                description=(
                    f"{going} Pick a town and your character stops wandering and walks there, a step every "
                    f"{int(1 / rpg.JOURNEY_STEP_CHANCE)} seconds or so. Travellers meet nobody on the road — no collision fights.\n\n"
                    "Typed: `!idle travel velvragh` · `!idle travel stop`"
                ),
                options=options, placeholder="Where to…", multi=False,
            )
            if not picked:
                return
            where = picked[0]
            # The dropdown was a long await.
            char = self._chars(gid).get(uid)
            if char is None:
                return

        if where.lower() == "stop":
            was = char.get("travel_to")
            char["travel_to"] = None
            await persistence.save_idle_character(gid, uid)
            await ctx.send(embed=emb("🧭 Travel", f"You give up on {was} and wander again." if was else "You weren't going anywhere.", C_GREY))
            return
        town = rpg.match_town(where)
        if town is None:
            await ctx.send(embed=emb("❌ Travel", "Towns: " + ", ".join(rpg.TOWNS) + ". `!idle travel stop` to wander.", C_RED))
            return
        quest = self._quest(gid)
        if quest.get("kind") == "journey" and uid in quest["members"]:
            await ctx.send(embed=emb("❌ Travel", "You're on a journey quest — it decides where you walk until it's done.", C_RED))
            return
        if rpg.travel_steps(char, town) == 0:
            await ctx.send(embed=emb("🧭 Travel", f"You're already standing in {town}.", C_GREY))
            return
        char["travel_to"] = town
        await persistence.save_idle_character(gid, uid)
        embed = emb(
            "🧭 Travel",
            f"You set out for **{town}**: {rpg.travel_steps(char, town)} squares, about "
            f"{format_duration(rpg.travel_eta_secs(char, town))} of walking while you're online. "
            "You can shop as soon as you're inside its ring. `!idle travel stop` to wander again.",
            C_GREEN,
        )
        await self._send_with_map(ctx, embed, highlight=(uid,))

    # ── !idle shop ───────────────────────────────────────────────────────

    @staticmethod
    def _shop_lines(char: dict) -> list:
        prices = rpg.shop_prices(char)
        return [
            ("find", f"Find — one more item roll · {prices['find']:,} gold"),
            ("sharpen", f"Sharpen — +{rpg.SHARPEN_PCT}% to one of your items · {rpg.PRICE_SHARPEN_PER_ITEM_LEVEL} gold per item level"),
            ("rush", f"Rush — {rpg.RUSH_PCT}% off your clock, once a day · {prices['rush']:,} gold"),
            ("duel", f"Second duel — once a day · {prices['duel']:,} gold"),
            ("class", f"New class — `!idle shop class <name>` · {prices['class']:,} gold"),
        ]

    @cmd_idle.command(name="shop")
    async def cmd_shop(self, ctx: commands.Context, item: str = None, *, arg: str = None):
        if not await self._ready(ctx, need_channel=True):
            return
        char = await self._own_char(ctx)
        if char is None:
            return
        gid, uid = ctx.guild.id, ctx.author.id
        usage = "`!idle shop find` · `sharpen <slot>` · `rush` · `duel` · `class <name>` · `auto on|off`"
        if item is not None and item.lower() == "auto":
            choice = (arg or "").lower()
            if choice not in ("on", "off"):
                state_now = "on" if char["auto_trade"] else "off"
                await ctx.send(embed=emb(
                    "🛒 Idle Shop",
                    f"Auto-trading is **{state_now}**. In a town's centre your character spends up to "
                    f"{rpg.AUTO_TRADE_BUDGET_PCT}% of its gold on a find and a sharpening, at most once every "
                    f"{format_duration(rpg.AUTO_TRADE_COOLDOWN_SECS)}. `!idle shop auto on|off`",
                    C_BLUE,
                ))
                return
            char["auto_trade"] = choice == "on"
            await persistence.save_idle_character(gid, uid)
            await ctx.send(embed=emb("🛒 Idle Shop", f"Auto-trading is now **{choice}**.", C_GREEN))
            return
        if rpg.market_in_reach(char) is None:
            near = rpg.nearest_town(char)
            where = f" The nearest is **{near[0]}**, {near[1]} squares away — `!idle travel` walks you there." if near else ""
            await ctx.send(embed=emb(
                "❌ No Market Here",
                f"You can only trade within {rpg.MARKET_RADIUS} squares of a town.{where}",
                C_RED,
            ))
            return
        if item is None:
            lines = self._shop_lines(char)
            picked = await pick_from_list(
                ctx,
                title="🛒 Idle Shop",
                description=f"The market of **{rpg.market_in_reach(char)}**. You have **{char['gold']:,}** gold.\n\n" + "\n".join(f"• {text}" for _key, text in lines) + f"\n\nTyped: {usage}",
                options=[(text.split(" · ")[0][:100], key) for key, text in lines],
                placeholder="Buy…", multi=False,
            )
            if not picked:
                return
            item = picked[0]
        item = item.lower()
        if item == "sharpen" and not arg:
            owned = [(f"{slot.title()} (level {char['items'][slot]['level']}) · {rpg.sharpen_price(char['items'][slot]):,} gold", slot)
                     for slot in rpg.ITEM_SLOTS if slot in char["items"]]
            if not owned:
                await ctx.send(embed=emb("❌ Idle Shop", "You have nothing to sharpen yet.", C_RED))
                return
            picked = await pick_from_list(
                ctx, title="🛒 Sharpen", description=f"You have **{char['gold']:,}** gold. Which item?",
                options=owned, placeholder="Item…", multi=False,
            )
            if not picked:
                return
            arg = picked[0]

        # The menus were long awaits: the character, and its purse, again.
        char = self._chars(gid).get(uid)
        if char is None or rpg.market_in_reach(char) is None:
            return   # retired, or wandered out of the market, while a menu was open
        if item == "find":
            bought, text = rpg.buy_find(uid, char, self.rng, self._namer(ctx.guild))
        elif item == "sharpen":
            slot = arg.lower()
            if slot not in rpg.ITEM_SLOTS:
                await ctx.send(embed=emb("❌ Idle Shop", f"Slots: {', '.join(rpg.ITEM_SLOTS)}.", C_RED))
                return
            bought, text = rpg.buy_sharpen(char, slot)
        elif item == "rush":
            bought, text = rpg.buy_rush(char, int(time.time()), _ct_today())
        elif item == "duel":
            bought, text = rpg.buy_second_duel(char, _ct_today())
        elif item == "class":
            class_name = _clean_class(arg or "")
            if class_name is None:
                await ctx.send(embed=emb(
                    "❌ Idle Shop",
                    f"`!idle shop class <name>` — up to {CLASS_MAX} characters: letters, digits, spaces, `'` and `-`.",
                    C_RED,
                ))
                return
            bought, text = rpg.buy_class(char, class_name)
            if bought:
                self._rename_due.add((gid, uid))
        else:
            await ctx.send(embed=emb("❌ Idle Shop", f"Usage: {usage}", C_RED))
            return
        if not bought:
            await ctx.send(embed=emb("❌ Idle Shop", text, C_RED))
            return
        await persistence.save_idle_character(gid, uid)
        await ctx.send(embed=emb("🛒 Idle Shop", f"{text}\n**{char['gold']:,}** gold left.", C_GREEN))

    def _in_idle_channel(self, ctx) -> bool:
        """The reply already shows there — don't post the same news under it."""
        channel = self._channel(ctx.guild)
        return channel is not None and ctx.channel.id == channel.id

    @cmd_idle.command(name="quest")
    async def cmd_quest(self, ctx: commands.Context):
        if not await self._ready(ctx, need_channel=True):
            return
        now = int(time.time())
        quest = self._quest(ctx.guild.id)
        name = self._namer(ctx.guild)
        if rpg.quest_active(quest):
            party = ", ".join(name(u) for u in quest["members"])
            if quest["kind"] == "journey":
                goal = quest["p1"] if quest["stage"] == 1 else quest["p2"]
                where = rpg.landmark_at(goal)
                body = (
                    f"{party} must {quest['description']}.\n"
                    f"Waypoint {quest['stage']} of 2: {where + ' ' if where else ''}[{goal[0]}, {goal[1]}]. "
                    f"It ends when all of them have arrived. "
                    f"Each of them comes back {rpg.QUEST_REWARD_PCT}% closer to their next level."
                )
                await self._send_with_map(ctx, emb("📜 Quest", body, C_BLUE), highlight=quest["members"])
                return
            body = (
                f"{party} must {quest['description']}.\nIt ends <t:{quest['ends_at']}:R>. "
                f"Each of them comes back {rpg.QUEST_REWARD_PCT}% closer to their next level."
            )
        else:
            ready = len(rpg.quest_eligible(self._chars(ctx.guild.id)))
            body = (
                f"No quest is running. One starts when at least {rpg.QUEST_MIN_PARTY} adventurers are level "
                f"{rpg.QUEST_MIN_LEVEL}+ and logged in — **{ready}** qualify right now."
            )
            if now < quest["not_before"]:
                body += f"\nThe gods offer the next one no sooner than <t:{quest['not_before']}:R>."
        await ctx.send(embed=emb("📜 Quest", body, C_BLUE))

    @cmd_idle.command(name="prestige")
    async def cmd_prestige(self, ctx: commands.Context):
        if not await self._ready(ctx, need_channel=True):
            return
        char = await self._own_char(ctx)
        if char is None:
            return
        if char["level"] < rpg.PRESTIGE_LEVEL:
            await ctx.send(embed=emb("❌ Prestige", f"Prestige opens at level {rpg.PRESTIGE_LEVEL}. You're level {char['level']}.", C_RED))
            return
        maxed = char["prestige"] >= rpg.PRESTIGE_MAX_RANKS
        perk = "no further speed bonus (you're at the cap), just the star" if maxed else f"levelling {rpg.PRESTIGE_BONUS_PCT}% faster, for good"
        if not await confirm_prompt(
            ctx,
            title="🌟 Prestige",
            description=f"You go back to **level 0** and lose **every item**. In return: a ★ and {perk}.",
            payer=ctx.author,
            not_yours=NOT_YOURS,
        ):
            return
        gid, uid = ctx.guild.id, ctx.author.id
        # Identity + level again: the prompt was a long await.
        if self._chars(gid).get(uid) is not char or char["level"] < rpg.PRESTIGE_LEVEL:
            return
        rpg.do_prestige(char, int(time.time()))
        self._rename_due.add((gid, uid))
        await persistence.save_idle_character(gid, uid)
        note = rpg.Note((uid,), f"🌟 {self._namer(ctx.guild)(uid)} has ascended to prestige {'★' * char['prestige']} and begins again at level 0.", True)
        await ctx.send(embed=emb("🌟 Prestige", note.text, C_GOLD))
        await self._deliver(ctx.guild, [note], skip_main=self._in_idle_channel(ctx))

    @cmd_idle.command(name="leave")
    async def cmd_leave(self, ctx: commands.Context):
        if not await self._ready(ctx):
            return
        gid, uid = ctx.guild.id, ctx.author.id
        char = self._chars(gid).get(uid)
        if char is None:
            await ctx.send(embed=emb("❌ No Character", "You have no character here.", C_RED))
            return
        if not await confirm_prompt(
            ctx,
            title="🪦 Retire Your Character",
            description=f"Your level {char['level']} {char['class']} and everything they carry is deleted for good.",
            payer=ctx.author,
            not_yours=NOT_YOURS,
        ):
            return
        if not await self._remove_character(ctx.guild, uid, char):
            return
        await ctx.send(embed=emb("🪦 Retired", "Your character hangs up their boots.", C_GREY))

    async def _remove_character(self, guild, uid: int, char: dict) -> bool:
        chars = self._chars(guild.id)
        if chars.get(uid) is not char:
            return False   # already gone, or replaced, while a prompt was open
        del chars[uid]
        await persistence.delete_idle_character(guild.id, uid)
        await self._archive_thread(guild, char["thread_id"])
        return True

    @cmd_idle.command(name="rules", aliases=["help"])
    async def cmd_rules(self, ctx: commands.Context, topic: str = None):
        detail = _RULES_TOPICS.get((topic or "").lower())
        if detail is not None:
            await ctx.send(embed=emb(f"📖 Idle RPG — {topic.title()}", detail, C_BLUE))
            return
        await ctx.send(embed=emb(
            "📖 Idle RPG",
            "**Do nothing. Level up.**\n\n"
            "⏳ Your character levels on a timer while you're online.\n"
            "⚔️ Items, fights and lucky breaks happen on their own.\n"
            "📜 High-level players get sent on quests for a big shortcut.\n\n"
            "`!idle status` · `items` · `map` · `travel` · `shop` · `top` · `align` · `duel @user` · `quest`\n"
            f"More: `!idle rules <{'|'.join(_RULES_TOPICS)}>`",
            C_BLUE,
        ))

    # ── !idle admin ──────────────────────────────────────────────────────

    @cmd_idle.group(name="admin", invoke_without_command=True)
    async def cmd_admin(self, ctx: commands.Context):
        await ctx.send(embed=emb(
            "🛠️ Idle RPG Admin",
            "`!idle admin hog @user` — a Hand of God, now\n"
            "`!idle admin push @user <±time>` — move a clock (`-2h` sooner, `+1d` later)\n"
            "`!idle admin gold @user <±amount>` — give or take gold\n"
            "`!idle admin remove @user` — delete a character\n"
            "`!idle admin reset` — wipe this server's game",
            C_GREY,
        ))

    async def _admin_target(self, ctx, who: "str | None", usage: str) -> "tuple[int, dict] | None":
        if not await self._ready(ctx):
            return None
        uid = await self._target_uid(ctx, who)
        char = self._chars(ctx.guild.id).get(uid) if uid else None
        if char is None:
            await ctx.send(embed=emb("❌ Idle Admin", f"No character found. Usage: {usage}", C_RED))
            return None
        return uid, char

    @cmd_admin.command(name="hog")
    async def cmd_admin_hog(self, ctx: commands.Context, *, who: str = None):
        found = await self._admin_target(ctx, who, "`!idle admin hog @user`")
        if found is None:
            return
        uid, _char = found
        note = rpg.hand_of_god(uid, self._chars(ctx.guild.id), self.rng, self._namer(ctx.guild), int(time.time()))
        await persistence.save_idle_character(ctx.guild.id, uid)
        await ctx.send(embed=emb("🙌 Hand of God", note.text, C_GOLD))
        await self._deliver(ctx.guild, [note], skip_main=self._in_idle_channel(ctx))

    @cmd_admin.command(name="gold")
    async def cmd_admin_gold(self, ctx: commands.Context, who: str = None, amount: str = None):
        usage = "`!idle admin gold @user <±amount>`"
        found = await self._admin_target(ctx, who, usage)
        if found is None:
            return
        uid, char = found
        delta = parse_int_amount((amount or "").lstrip("+-"))
        if not delta:
            await ctx.send(embed=emb("❌ Idle Admin", f"Usage: {usage}", C_RED))
            return
        if amount.startswith("-"):
            delta = -min(delta, char["gold"])
        char["gold"] += delta
        await persistence.save_idle_character(ctx.guild.id, uid)
        await ctx.send(embed=emb(
            "🛠️ Gold", f"{self._namer(ctx.guild)(uid)} now has **{char['gold']:,}** gold ({delta:+,}).", C_GREEN,
        ))

    @cmd_admin.command(name="push")
    async def cmd_admin_push(self, ctx: commands.Context, who: str = None, amount: str = None):
        usage = "`!idle admin push @user <±time>` — `-2h` sooner, `+1d` later"
        found = await self._admin_target(ctx, who, usage)
        if found is None:
            return
        uid, char = found
        seconds = parse_duration((amount or "").lstrip("+-"))
        if seconds is None:
            await ctx.send(embed=emb("❌ Idle Admin", f"Usage: {usage}", C_RED))
            return
        now = int(time.time())
        if amount.startswith("-"):
            seconds = -min(seconds, rpg.time_left(char, now))
        rpg.shift(char, seconds)
        await persistence.save_idle_character(ctx.guild.id, uid)
        direction = "later" if seconds > 0 else "sooner"
        await ctx.send(embed=emb(
            "🛠️ Clock Moved",
            f"{self._namer(ctx.guild)(uid)} levels {format_duration(abs(seconds))} {direction} — "
            f"{format_duration(rpg.time_left(char, now))} to go.",
            C_GREEN,
        ))

    @cmd_admin.command(name="remove")
    async def cmd_admin_remove(self, ctx: commands.Context, *, who: str = None):
        found = await self._admin_target(ctx, who, "`!idle admin remove @user`")
        if found is None:
            return
        uid, char = found
        if not await confirm_prompt(
            ctx,
            title="🗑️ Remove Character",
            description=f"Delete <@{uid}>'s level {char['level']} {char['class']} for good?",
            payer=ctx.author,
            not_yours=NOT_YOURS,
        ):
            return
        if await self._remove_character(ctx.guild, uid, char):
            await ctx.send(embed=emb("🗑️ Character Removed", f"<@{uid}>'s character is gone.", C_GREY))

    @cmd_admin.command(name="reset")
    async def cmd_admin_reset(self, ctx: commands.Context):
        if not await self._ready(ctx):
            return
        gid = ctx.guild.id
        count = len(self._chars(gid))
        if not await confirm_prompt(
            ctx,
            title="💥 Reset the Idle RPG",
            description=f"This deletes all **{count}** character(s), their items and the current quest in this server. There is no undo.",
            payer=ctx.author,
            not_yours=NOT_YOURS,
        ):
            return
        chars = state.idle_characters.pop(gid, {})
        state.idle_quests.pop(gid, None)
        self._pending.pop(gid, None)
        await persistence.delete_idle_guild(gid)
        for char in chars.values():
            await self._archive_thread(ctx.guild, char["thread_id"])
        await ctx.send(embed=emb("💥 Idle RPG Reset", f"{len(chars)} character(s) deleted. A new age begins.", C_GREY))


async def setup(bot):
    await bot.add_cog(IdleCog(bot))
