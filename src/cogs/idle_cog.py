"""!idle — a per-guild idle RPG. The rules live in src/idlerpg.py; this cog
is the Discord half: commands, the once-a-minute tick, the listeners that
track who is online, and the feed threads.

The game runs in the channel set with `!settings channel idle` and is off
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
from discord import ui
from discord.ext import commands, tasks

import src.persistence as persistence
from src import idlerpg as rpg
from src import state
from src.confirm_view import confirm_choice, confirm_prompt
from src.economy import _ct_now, _ct_today, next_daily_reset_ts
from src.guild_config import get_guild_cfg
from src.idle_map import MAP_FILENAME, render_map
from src.helpers import (
    emb, C_BLUE, C_GOLD, C_GREEN, C_GREY, C_RED,
    format_duration, parse_duration, parse_int_amount,
)
from src.permissions import _wrong_channel_reply, is_silenced
from src.settings_views import Field, open_form, pick_from_list

log = logging.getLogger(__name__)

TICK_SECONDS = 60
TICKS_PER_DAY = 86_400 // TICK_SECONDS
# A late boot may owe a character many levels; past this the rest wait a tick.
MAX_LEVELS_PER_TICK = 25
# With !settings idle-enroll on, this many members a tick are handed a
# character: a whole server made at once would all level, fight and shop in
# the same minute.
ENROLL_PER_TICK = 3
SEEN_SAVE_SECS = 300            # how stale the persisted last_seen may get
THREAD_AUTO_ARCHIVE_MINUTES = 10080   # Discord's maximum; a send un-archives the thread anyway
THREAD_NAME_MAX = 100
# Discord allows two renames per thread per ten minutes (see CLAUDE.md:
# Gambling threads) — titles are brought up to date by the tick, never inline.
RENAME_INTERVAL = 300.0
# The standings board is one edited message, so it is cheap — but a channel
# whose pinned post rewrites itself every minute is its own kind of noise.
BOARD_INTERVAL = 600.0
MESSAGE_MAX = 1900
LEADERBOARD_SIZE = 10
CLASS_MAX = 30
_CLASS_RE = re.compile(r"[\w][\w '\-]*")
NOT_YOURS = "Not your prompt."
WAGER_ACCEPT_SECS = 120.0
DUEL_ROUND_SECS = 3.0   # a beat between rounds, so a duel reads as a fight
NO_MENTIONS = discord.AllowedMentions.none()
MENU_TIMEOUT = 300.0     # the status card's action menu
HELP_TIMEOUT = 900.0     # the help card's topic menu and Join button

# `!idle <sub>` also answers to a bare `!<sub>`. Every subcommand but three:
# `admin` (a bare `!admin` would be nobody's idea of an idle command), and
# `shop` and `help`, which are real commands elsewhere — the market is
# `!store` bare (the coin shop gave up that alias for it), and UtilityCog
# hands `!help` over in idle context (`cmd_rules`). Registered on the bot at
# construction, as ShopCog does with `_SHOP_TOP_ALIASES`; the copies share
# the subcommand's callback, so a fix lands in both spellings.
_TOP_ALIASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("join", "cmd_join", ()),
    ("status", "cmd_status", ("info",)),
    ("map", "cmd_map", ()),
    ("lore", "cmd_lore", ()),
    ("world", "cmd_world", ("boost",)),
    ("bless", "cmd_bless", ()),
    ("title", "cmd_title", ("titles",)),
    ("items", "cmd_items", ()),
    ("top", "cmd_top", ()),
    ("align", "cmd_align", ("alignment",)),
    ("duel", "cmd_duel", ()),
    ("gamble", "cmd_gamble", ("bet",)),
    ("travel", "cmd_travel", ()),
    ("store", "cmd_shop", ()),
    ("quest", "cmd_quest", ()),
    ("prestige", "cmd_prestige", ()),
    ("leave", "cmd_leave", ()),
    ("rules", "cmd_rules", ()),
)
# What runs inside a character's feed thread: `!idle …`, the bare forms
# above, and `!help`, which shows the game's card there.
FEED_THREAD_COMMANDS = frozenset({"idle", "help", *(name for name, _attr, _aliases in _TOP_ALIASES)})
FEED_THREAD_ONLY = (
    "Only the idle game plays in a feed thread — `!idle …`, or `!status`, `!map`, `!travel` and the rest bare. "
    "Everything else goes in the channel."
)
# The sheet's buttons: (label, emoji, subcommand attribute). Each is shown
# only while the invoker could use it (`_available`), so the card reads as
# what you can do now, not a command list.
_ACTIONS = (
    ("Store", "🛒", "cmd_shop"),
    ("Gamble", "🎲", "cmd_gamble"),
    ("Travel", "🧭", "cmd_travel"),
    ("Quest", "📜", "cmd_quest"),
    ("Top", "🏆", "cmd_top"),
    ("Prestige", "★", "cmd_prestige"),
)


class IdleThreadOnly(commands.CheckFailure):
    """Raised by the feed-thread gate for a command that isn't the game's.
    The gate has already replied; on_command_error swallows this."""

ALIGN_EFFECTS = (
    "**Good** +10% item power in battle, prayers with other good players, rarer critical strikes.\n"
    "**Evil** −10% item power, more critical strikes, a chance to rob the good — or be forsaken.\n"
    "**Lawful** half as many godsends and calamities. **Chaotic** twice as many — a wilder ride, the same average."
)

# `!idle rules <topic>` — the bare command stays a few lines on purpose.
_RULES_TOPICS = {
    "levels": (
        f"Level 1 takes {format_duration(rpg.ttl(0))}; each level after takes {int((rpg.LEVEL_MULT - 1) * 100)}% longer.\n"
        f"The clock runs while you've been online in the last {format_duration(rpg.GRACE_SECS)} — idle and do-not-disturb count. "
        "After that it pauses and picks up where it stopped. Being away never costs you anything.\n"
        f"Share one of the server's voice channels with someone (not the AFK one, and not alone) and your clock runs {rpg.VOICE_BONUS_PCT}% faster, "
        f"with {rpg.VOICE_BONUS_PCT}% more gold from everything you earn."
    ),
    "battles": (
        f"Each level-up finds an item for one of ten slots and may start a fight (always, from level {rpg.BATTLE_ALWAYS_LEVEL}).\n"
        "Each side rolls a number from 0 up to their item power (`!idle items`), shown as \"rolled 12 of 40\"; the higher roll wins. "
        "Win and time comes off your clock (`-2min`); lose and it goes on (`+2min`).\n"
        f"You only run into another player if they are within {rpg.BATTLE_RANGE} squares of you on the map — otherwise you face "
        "the Idle Warden, who is always your own match. A fight with the Warden stays in your own thread.\n"
        f"`!idle duel <name>` once a day: {rpg.DUEL_MAX_ROUNDS} rounds of blows, played out in both feeds, and the loser hands "
        f"{rpg.DUEL_PCT}% of their clock to the winner. Nobody is left hurt by it — a duel is a match, not a mugging."
    ),
    "monsters": (
        "Out in the wilds your character runs into monsters — rats and bandits on the plains, trolls and dragons in the mountains, "
        "worse in the caves, the haunted ground and T'rnalvph, where the rewards are better too. Towns (the rings on `!idle map`) are safe.\n"
        "A monster is sized to you, so gear alone doesn't make them easy; its prefix (Veteran, Elite … Corrupted) and kind set how much harder. "
f"A fight runs up to {rpg.MOB_MAX_ROUNDS} rounds of blows both ways and costs **health**, not time — you carry your wounds between fights "
        f"and mend about {100 // rpg.HP_REGEN_DIVISOR}% of yourself a minute. Kill it: gold, a little off your clock, sometimes an item. "
        f"Lose and it costs you time, some gold and everything in your bag, and you wake at a town's edge.\n"
        f"At or under {rpg.CAMP_HP_PCT}% health your character makes camp instead of fighting, so it takes a bad run to fall.\n"
        "Who swings first each round is rolled, leaning to whoever is stronger — a monster can land the opening blow.\n"
        "That bag is the one thing at stake on the walk to a market — a full one is a reason to go.\n"
        f"The {len(rpg.SIGNATURE_DROPS)} worst things in the realm each leave something only they leave: a Dragon its "
        "Wingcase Shield, a Basilisk its Unblinking Eye, and so on.\n"
        f"From level 11 a quarter of fights are against a group. Up to level {rpg.MOB_EASY_LEVEL} monsters fight at half strength.\n"
        "Roughly one every quarter of an hour while you're out in the wilds — none at all inside a town's ring."
    ),
    "map": (
        f"The realm is a {rpg.MAP_SIZE}×{rpg.MAP_SIZE} grid. Everyone online wanders one step a second, and the edges wrap.\n"
        "Land on the same square as someone and you may fight them, there and then.\n"
        "Some quests are journeys: the party stops wandering and walks to one landmark, then another. `!idle map` shows it all.\n"
        "`!idle travel <place>` walks you to a town, or out to a wild region, on purpose — slowly, and meeting monsters but no players on the way."
    ),
    "gold": (
        "The realm's own money — nothing to do with the server's coins, and it can't be sent to anyone.\n"
        "You earn it by levelling, winning fights and finishing quests; a collision fight's winner also lifts "
        f"{rpg.GOLD_SPOILS_PCT}% of the loser's purse.\n"
        f"`!idle shop` spends it, but only within {rpg.MARKET_RADIUS} squares of a town (the rings on `!idle map`): an extra item find, "
        "sharpening an item, a once-a-day rush, a second duel, a new class. Walk right into a town and your character "
        f"trades on its own with up to {rpg.AUTO_TRADE_BUDGET_PCT}% of its gold — `!idle shop auto off` stops that. "
        "`!idle duel <name> 200` bets gold, anywhere.\n"
        f"Towns have gambling tables, too. In one, your character bets on its own now and then — a bigger share the richer it is, "
        f"but never more than {rpg.GAMBLE_VISIT_CAP_PCT}% of the purse it arrived with per visit — and `!idle gamble <gold>` bets by hand. "
        f"Even money; the house wins {rpg.GAMBLE_LOSE_BELOW} in 100."
    ),
    "luck": (
        "🌟 **Godsends** and 🌧️ **calamities** find you about once a day each, in your own feed.\n"
        "A godsend may take time off your clock, hand you gold or an item, mend your wounds, or put you on a cart to a town. "
        "A calamity may add time, lift your purse, dent your gear, leave you hurt (never dead) or set you down lost in the wilds.\n"
        "🙌 The **Hand of God** is rarer and much bigger — 5–75% of a level, four times in five for the better — and the whole server hears it.\n"
        "Alignment bends how often the first two find you: `!idle rules alignment`."
    ),
    "world": (
        "Every few days something happens to the whole realm — a blood moon, a horde coming down out of its own "
        "country, a storm over one region, an hour where everything comes twice as fast. Each keeps its own hours: "
        "the moon only rises at night, hordes arrive around midday, storms whenever they like. It is announced when "
        "it's coming and again when it lands, and it changes what the monsters are worth, or what everyone's clock "
        f"is doing. `!idle world` says what's going on.\n"
        f"🕊️ `!idle bless` spends **{rpg.BLESS_COST:,}** of your gold on {rpg.BLESS_BOOST_PCT}% faster levelling and gold for "
        f"**everyone here**, for {format_duration(rpg.BLESS_SECS)}. They stack up to {rpg.BLESS_MAX}. It is the only "
        "thing in the game you can buy for somebody else."
    ),
    "hunts": (
        f"Walk into a town's ring and sooner or later someone asks you to deal with {rpg.HUNT_MIN}–{rpg.HUNT_MAX} of "
        "a particular kind of monster. You take it on the spot — there's nobody here to accept it — and your "
        "character sets off for the nearest country that kind lives in.\n"
        "Once there it hunts: that kind turns up far more often than it otherwise would, and a kill anywhere counts. "
        "It keeps to that country until the hunt is done, and walks back if something carries it out.\n"
        f"Finish and you get {rpg.HUNT_REWARD_PCT}% off your clock and "
        f"{rpg.HUNT_GOLD_PER_KILL_PER_LEVEL} gold per kill per level. A town has another errand "
        f"{format_duration(rpg.HUNT_REST_SECS)} later."
    ),
    "titles": (
        "Six of them, each earned once by passing a mark and then yours for good — a purse you later spend doesn't "
        "cost you Gold Hoarder.\n"
        + "\n".join(f"🎖️ **{title}** — {need:,} {what}" for (_k, title, need, _r), what in zip(
            rpg.TITLES, ("gold held at once", "monsters slain", "hunts finished", "bets at the tables",
                         "prestige", "times struck down")))
        + "\n\n`!idle title` shows how far off you are; `!idle title <name>` wears one."
    ),
    "alignment": ALIGN_EFFECTS + "\nSet it with `!idle align`, once a day.",
    "quests": (
        f"Now and then, {rpg.QUEST_MIN_PARTY}–{rpg.QUEST_MAX_PARTY} online players of level {rpg.QUEST_MIN_LEVEL}+ are sent on a 12–24 hour quest.\n"
        f"Finish and each quester's clock drops {rpg.QUEST_REWARD_PCT}%, with gold on top. There is nothing to do, and nothing to get wrong."
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


def _off_embed() -> discord.Embed:
    return emb(
        "💤 Idle RPG Is Off",
        "No idle channel is set here. An admin can turn the game on with `!settings channel idle #channel`.",
        C_GREY,
    )


# ── the cards' components ────────────────────────────────────────────────
# A menu on a card the command already sent, so a player can move on from
# the sheet or the help without typing the next command. Picks run the
# subcommand's callback with the card's ctx, so a pick and its typed form
# can't drift. The card keeps its components until the view times out.

class _CardView(ui.View):
    def __init__(self, cog, timeout: float):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.message = None

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(view=None)
        except discord.HTTPException:
            pass  # cosmetic — the card stays, the menu is dead either way


class _ActionButton(ui.Button):
    def __init__(self, label: str, emoji: str, action: str):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        view: _ActionView = self.view  # type: ignore[assignment]
        await interaction.response.defer()
        await view.cog._act(view.ctx, self.action)


class _ActionView(_CardView):
    """The sheet's buttons. Store, gamble, travel and prestige act on the
    invoker's character, so only the invoker may press them."""

    def __init__(self, cog, ctx, actions):
        super().__init__(cog, MENU_TIMEOUT)
        self.ctx = ctx
        for label, emoji, action in _ACTIONS:
            if action in actions:
                self.add_item(_ActionButton(label, emoji, action))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(NOT_YOURS, ephemeral=True)
        return False


class _TopicSelect(ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Read more about…", min_values=1, max_values=1,
            options=[discord.SelectOption(label=topic.title(), value=topic) for topic in _RULES_TOPICS],
        )

    async def callback(self, interaction: discord.Interaction):
        topic = self.values[0]
        # Private: the card is shared, and a topic is one reader's question.
        await interaction.response.send_message(embed=emb(f"📖 Idle RPG — {topic.title()}", _RULES_TOPICS[topic], C_BLUE), ephemeral=True)


class _JoinModal(ui.Modal, title="Join the Idle RPG"):
    class_name = ui.TextInput(label="Your class — invent one", placeholder="Drunken Bard, Tax Wizard…", max_length=CLASS_MAX)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        async def send(embed: discord.Embed, *, error: bool = False) -> None:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=error)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=error)
        await self.cog._join(interaction.guild, interaction.user, str(self.class_name.value), interaction.channel, send)


class _JoinButton(ui.Button):
    def __init__(self):
        super().__init__(label="Join", style=discord.ButtonStyle.success, emoji="⚔️")

    async def callback(self, interaction: discord.Interaction):
        cog = self.view.cog
        guild = interaction.guild
        char = cog._chars(guild.id).get(interaction.user.id)
        if char is not None and char["claimed"]:
            feed = f" Your feed is <#{char['thread_id']}>." if char["thread_id"] else ""
            await interaction.response.send_message(embed=emb("❌ Already Adventuring", f"You already have a character here.{feed}", C_RED), ephemeral=True)
            return
        if cog._channel(guild) is None:
            await interaction.response.send_message(embed=_off_embed(), ephemeral=True)
            return
        await interaction.response.send_modal(_JoinModal(cog))


class _HelpView(_CardView):
    """The help card's menu: anyone may read a topic or join — the card is
    the one players pass on."""

    def __init__(self, cog):
        super().__init__(cog, HELP_TIMEOUT)
        self.add_item(_TopicSelect())
        self.add_item(_JoinButton())


class IdleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rng = random.Random()
        self._dirty: set = set()            # (guild_id, uid) rows to save at the next flush
        self._dirty_quests: set = set()
        self._dirty_events: set = set()     # guild_ids whose world rows changed
        self._pending: dict = {}            # guild_id -> notes queued by listeners
        self._seen_saved: dict = {}
        self._creating: set = set()         # (guild_id, uid) with a thread being created
        self._rename_due: set = set()
        self._renamed_at: dict = {}
        self._board_at: dict = {}
        # Tests hand in a bare namespace and call the callbacks directly.
        if isinstance(bot, commands.Bot):
            for name, attr, aliases in _TOP_ALIASES:
                # cog=self is load-bearing (see ShopCog): without it discord.py
                # binds `self` to the Context.
                source = getattr(self, attr)
                alias = commands.Command(source.callback, name=name, aliases=list(aliases),
                                         help=source.help, usage=source.usage)
                alias.cog = self
                bot.add_command(alias)

    async def cog_load(self):
        # Started here (not __init__) so tests constructing the cog don't spawn the loop.
        self._loop.start()

    def cog_unload(self):
        self._loop.cancel()
        if isinstance(self.bot, commands.Bot):
            for name, _attr, _aliases in _TOP_ALIASES:
                self.bot.remove_command(name)

    async def bot_check(self, ctx: commands.Context) -> bool:
        """Bot-wide gate (a Cog special method, registered for every
        command): inside a character's feed thread only the idle game runs —
        the thread is that story, not a second copy of the channel."""
        if ctx.command is None or ctx.guild is None or not self._is_feed_thread(ctx.guild.id, ctx.channel.id):
            return True
        name = ctx.command.qualified_name
        if name.split(" ")[0] == "idle" or name in FEED_THREAD_COMMANDS:
            return True
        try:
            await _wrong_channel_reply(ctx, FEED_THREAD_ONLY, title="⚔️ Idle Feed")
        except discord.HTTPException:
            pass  # can't reply here (thread locked, no send rights) — still deny
        raise IdleThreadOnly()

    # ── lookups ──────────────────────────────────────────────────────────

    @staticmethod
    def _chars(guild_id: int) -> dict:
        return state.idle_characters.setdefault(guild_id, {})

    @staticmethod
    def _quest(guild_id: int) -> dict:
        return state.idle_quests.setdefault(guild_id, rpg.new_quest())

    @staticmethod
    def _events(guild_id: int) -> list:
        return state.idle_guild_events.setdefault(guild_id, [])

    @staticmethod
    def _channel(guild):
        cid = get_guild_cfg(guild.id).get("idle_channel")
        return guild.get_channel(cid) if cid else None

    @staticmethod
    def _is_feed_thread(guild_id: int, channel_id: int) -> bool:
        return any(char.get("thread_id") == channel_id for char in state.idle_characters.get(guild_id, {}).values())

    def in_idle_context(self, ctx) -> bool:
        """The idle channel or one of its feed threads — where `!help` means
        the game's card (UtilityCog asks)."""
        if ctx.guild is None:
            return False
        channel = self._channel(ctx.guild)
        return (channel is not None and ctx.channel.id == channel.id) or self._is_feed_thread(ctx.guild.id, ctx.channel.id)

    def _available(self, guild, uid: int) -> set:
        """The sheet's buttons the invoker could press right now. The ladder
        is always there; the rest need a character, and each what its
        command would otherwise refuse: a market in reach (the tables also
        want gold), not being walked by a journey, a running quest, the
        prestige level."""
        actions = {"cmd_top"}
        char = self._chars(guild.id).get(uid)
        if char is None:
            return actions
        quest = self._quest(guild.id)
        if rpg.quest_active(quest):
            actions.add("cmd_quest")
        if not (quest.get("kind") == "journey" and uid in quest["members"]):
            actions.add("cmd_travel")
        if rpg.market_in_reach(char) is not None:
            actions.add("cmd_shop")
            if char["gold"] > 0:
                actions.add("cmd_gamble")
        if char["level"] >= rpg.PRESTIGE_LEVEL:
            actions.add("cmd_prestige")
        return actions

    async def _act(self, ctx, action: str) -> None:
        """A press on the sheet: the subcommand, run bare."""
        await getattr(self, action).callback(self, ctx)

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

    @staticmethod
    def _in_voice(guild, uid: int) -> bool:
        """In one of this guild's voice channels with at least one other
        person — the AFK channel and a channel shared only with bots don't
        count, or parking alone in an empty room overnight would."""
        member = guild.get_member(uid)
        channel = getattr(getattr(member, "voice", None), "channel", None)
        if channel is None or channel == getattr(guild, "afk_channel", None):
            return False
        return sum(1 for m in getattr(channel, "members", ()) if not m.bot) >= 2

    def _title(self, guild, uid: int, char: dict) -> str:
        member = guild.get_member(uid)
        who = member.display_name if member else str(uid)
        stars = "★" * char["prestige"] + " " if char["prestige"] else ""
        return f"{stars}{rpg.titled(char, who)} — Lv {char['level']} {char['class']}"[:THREAD_NAME_MAX]

    def _boost(self, guild, char: dict, now: int) -> int:
        """What this character's clock and gold are running at over par."""
        return rpg.boost_pct(char, rpg.guild_boost_pct(self._events(guild.id), now), now)

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
        enrolling = {int(gid) for gid, cfg in state.guild_settings.items() if cfg.get("idle_enroll")}
        for gid in sorted(set(state.idle_characters) | enrolling):
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            await self._enroll(guild, now)
            notes = self._advance(guild, now)
            await self._deliver(guild, notes)
            await self._sweep_renames(guild)
            await self._sweep_board(guild)
        await self._flush()

    async def _enroll(self, guild, now: int) -> None:
        """Hand a character to members who hold one of the bot's own roles
        (state.bot_roles — whoever has taken part in the bot's economy) and
        have none. Nothing is sent to them, now or later: an unclaimed
        character has no feed thread and is never mentioned. `!idle join
        <class>` is how its player takes it up."""
        gid = guild.id
        if not get_guild_cfg(gid).get("idle_enroll") or self._channel(guild) is None:
            return
        chars, opted_out = self._chars(gid), state.idle_optouts.get(gid, set())
        made = 0
        for member in list(getattr(guild, "members", ())):
            if made >= ENROLL_PER_TICK:
                break
            if (
                member.bot or member.id in chars or member.id in opted_out
                or is_silenced(member.id, gid)
                or not any(role.id in state.bot_roles for role in getattr(member, "roles", ()))
            ):
                continue
            char = rpg.new_character(rpg.UNCLAIMED_CLASS, now, claimed=False)
            rpg.ensure_position(char, self.rng)
            rpg.stagger_start(char, self.rng)
            chars[member.id] = char
            made += 1
            await persistence.save_idle_character(gid, member.id)

    def _advance(self, guild, now: int) -> list:
        """One tick of the rules for one guild. Synchronous on purpose: no
        command or listener can interleave with a half-applied tick."""
        gid = guild.id
        chars, quest = self._chars(gid), self._quest(gid)
        enabled = self._channel(guild) is not None
        name = self._namer(guild)
        pace = rpg.PACES.get(get_guild_cfg(gid).get("idle_pace"), rpg.PACES[rpg.DEFAULT_PACE])
        notes: list = []
        events = self._events(gid)
        if enabled:
            # The hour decides which kinds are on the table — a blood moon
            # only rises at night (see idlerpg.WORLD_EVENTS).
            world = rpg.tick_world(events, self.rng, now, TICKS_PER_DAY, _ct_now().hour)
            if world:
                self._dirty_events.add(gid)   # tick_world only speaks when it changed something
                notes += world
        guild_pct = rpg.guild_boost_pct(events, now)
        effect = rpg.world_effect(events, now)
        voiced = {uid for uid in chars if self._in_voice(guild, uid)}
        # uid -> (the boost it is running at, the purse it started the tick
        # on). One snapshot pays the bonus on everything earned since,
        # whatever earned it — levels, fights, hunts, luck.
        boosted: dict = {}

        for uid, char in list(chars.items()):
            # Voice proves presence too — a phone in a call often shows offline.
            if uid in voiced or self._online(guild, uid):
                self._seen(guild, uid, char, now)
            if rpg.is_paused(char):
                continue
            if not enabled:
                rpg.pause(char, now)
                self._dirty.add((gid, uid))
                continue
            pct = rpg.boost_pct(char, guild_pct, now) + (rpg.VOICE_BONUS_PCT if uid in voiced else 0)
            if pct:
                boosted[uid] = (pct, char["gold"])
                rpg.apply_boost(char, TICK_SECONDS, pct)
            rpg.regen_hp(char)
            here = rpg.logged_in(char, now)
            horizon = now if here else char["last_seen"] + rpg.GRACE_SECS
            for _ in range(MAX_LEVELS_PER_TICK):
                if char["next_level_at"] > horizon:
                    break
                rpg.level_up(char)
                earned = rpg.level_gold(char["level"])
                char["gold"] += earned
                self._rename_due.add((gid, uid))
                reached = f"🎉 {name(uid)} the {char['class']} reached **level {char['level']}**! +{earned:,} gold."
                notes.append(rpg.Note(
                    (uid,),
                    f"{reached} The next one takes {format_duration(rpg.ttl(char['level'], char['prestige']))}.",
                    rpg.level_is_news(char["level"], char["prestige"]),
                    # The room doesn't need the player's own countdown; their feed does.
                    public_text=reached,
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
                asked = rpg.offer_hunt(uid, chars[uid], self.rng, name, now, 3600 // TICK_SECONDS)
                if asked:
                    notes.append(asked)
                if self.rng.random() < pace.mob_fights_per_day / TICKS_PER_DAY:
                    notes += rpg.mob_encounter(uid, chars[uid], self.rng, name, now, effect)
            notes += rpg.tick_quest(chars, quest, self.rng, name, now)
            if quest != before:
                self._dirty_quests.add(gid)
            for uid, char in chars.items():
                notes += rpg.check_titles(uid, char, name)

        for uid, (pct, before) in boosted.items():
            char = chars.get(uid)
            bonus = rpg.gold_bonus(char["gold"] - before, pct) if char else 0
            if bonus:
                char["gold"] += bonus
                mark = "🎙️" if uid in voiced else "✨"
                notes.append(rpg.Note((uid,), f"{mark} +{pct}% this minute: +{bonus:,} gold for {name(uid)}."))

        if enabled:
            # After the voice bonus on purpose: table winnings aren't earnings to top up.
            for uid in rpg.running(chars):
                bet = rpg.town_gamble(uid, chars[uid], self.rng, name, now, 3600 // TICK_SECONDS)
                if bet:
                    notes.append(bet)

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
        for gid in list(self._dirty_events):
            self._dirty_events.discard(gid)
            try:
                await persistence.save_idle_guild_events(gid)
            except Exception:
                log.exception("idle: save of %s's world rows failed", gid)
                self._dirty_events.add(gid)

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
            await self._send(channel, [n.public_text or n.text for n in public], ping={uid for n in public for uid in n.ping})
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

    async def _map_file(self, guild, highlight=(), viewer=None) -> discord.File:
        """The realm as a PNG: every character, the landmarks, a running
        journey's waypoints, and the viewer's own route. Drawn off the event loop."""
        players = []
        for uid, char in self._chars(guild.id).items():
            if char.get("x") is None:
                continue
            member = guild.get_member(uid)
            players.append((uid, member.display_name if member else str(uid), char["x"], char["y"]))
        quest = self._quest(guild.id)
        journey = dict(quest) if quest.get("kind") == "journey" else None
        # Only the viewer's own route, and only on a picture of their character:
        # where somebody else is walking is theirs to know.
        routes = []
        char = self._chars(guild.id).get(viewer) if viewer in highlight else None
        goal = rpg.walking_to(viewer, char, quest) if char and char.get("x") is not None else None
        if goal is not None:
            routes.append((char["x"], char["y"], *goal))
        png = await asyncio.to_thread(render_map, players, highlight=tuple(highlight), quest=journey, routes=routes)
        return discord.File(io.BytesIO(png), filename=MAP_FILENAME)

    async def _post_map(self, guild, channel) -> None:
        try:
            await channel.send(file=await self._map_file(guild), silent=True, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as e:
            log.warning("idle: map post to %s failed (%s)", channel.id, type(e).__name__)

    async def _send_with_map(self, ctx, embed: discord.Embed, highlight=(), view: "_CardView | None" = None) -> None:
        embed.set_image(url=f"attachment://{MAP_FILENAME}")
        extra = {"view": view} if view is not None else {}
        message = await ctx.send(embed=embed, file=await self._map_file(ctx.guild, highlight, viewer=ctx.author.id), **extra)
        if view is not None:
            view.message = message

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
        if char is None or not char["claimed"]:
            # Unclaimed: no thread, because adding a member to one notifies them.
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

    # ── the standings board ──────────────────────────────────────────────
    #
    # One message in the idle channel, edited in place every BOARD_INTERVAL
    # rather than reposted — sizzlorox/Idle-RPG-Bot keeps nine of these and
    # edits them on a ten-minute cron. Editing beats posting for the same
    # reason the thread titles are swept: Discord rate-limits either way, and
    # a channel that fills up with stale ladders is worse than no ladder.

    def _board_embed(self, guild) -> discord.Embed:
        now = int(time.time())
        chars = self._chars(guild.id)
        name = self._namer(guild)
        ranked = rpg.ladder(chars, now, rpg.BOARD_SIZE)
        lines = []
        for i, (uid, char) in enumerate(ranked, 1):
            stars = "★" * char["prestige"] + " " if char["prestige"] else ""
            clock = "⏸️" if rpg.is_paused(char) else f"next <t:{char['next_level_at']}:R>"
            lines.append(f"**{i}.** {name(uid)} — {stars}Lv {char['level']} {char['class']} · {clock}")
        embed = emb(
            "🏔️ The Realm's Standings",
            "\n".join(lines) or "Nobody is adventuring here yet. `!idle join <class>`.",
            C_GOLD,
        )
        for heading, read, show in rpg.BOARD_COLUMNS:
            top = rpg.board_ranking(chars, read)
            if top:
                embed.add_field(
                    name=heading,
                    value="\n".join(f"{i}. {name(uid)} — {show(value)}" for i, (uid, value) in enumerate(top, 1)),
                    inline=True,
                )
        embed.set_footer(text="Updated every few minutes")
        return embed

    async def _sweep_board(self, guild) -> None:
        channel = self._channel(guild)
        if channel is None:
            return
        key = guild.id
        now = time.monotonic()
        if now - self._board_at.get(key, -BOARD_INTERVAL) < BOARD_INTERVAL:
            return
        self._board_at[key] = now      # the slot is spent whether or not the edit lands
        cfg = get_guild_cfg(key)
        embed = self._board_embed(guild)
        message_id = cfg.get("idle_board_message")
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embed)
                return
            except discord.NotFound:
                pass                    # deleted, or the idle channel moved — post a new one
            except discord.HTTPException as e:
                log.warning("idle: board edit in %s failed (%s)", channel.id, type(e).__name__)
                return
        try:
            posted = await channel.send(embed=embed, silent=True, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as e:
            log.warning("idle: board post to %s failed (%s)", channel.id, type(e).__name__)
            return
        cfg["idle_board_message"] = posted.id
        await persistence.save_guild_settings(key)
        try:
            await posted.pin()
        except discord.HTTPException:
            pass                        # a full pin list or no Manage Messages; the board still works

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
            f"🚪 {self._namer(guild)(member.id)} walked out on their own story.{rpg.clock_tail(seconds)}",
        ))

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        # Names are never stored — every line reads the member's current one —
        # but a thread title is text Discord holds, so it has to be refreshed.
        if before.display_name != after.display_name and after.id in state.idle_characters.get(after.guild.id, {}):
            self._rename_due.add((after.guild.id, after.id))

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        char = state.idle_characters.get(member.guild.id, {}).get(member.id)
        if char is None:
            return
        seconds = self._penalize(member.guild, member.id, rpg.PEN_QUIT, int(time.time()))
        self._pending.setdefault(member.guild.id, []).append(rpg.Note(
            (member.id,),
            f"🏃 **{discord.utils.escape_markdown(member.display_name)}** fled the realm."
            f"{rpg.clock_tail(seconds)}",
            True,
        ))

    # ── command plumbing ─────────────────────────────────────────────────

    async def _ready(self, ctx, *, need_channel: bool = False) -> bool:
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "The idle RPG is played in a server.", C_RED))
            return False
        if need_channel and self._channel(ctx.guild) is None:
            await ctx.send(embed=_off_embed())
            return False
        return True

    async def _own_char(self, ctx) -> "dict | None":
        char = self._chars(ctx.guild.id).get(ctx.author.id)
        if char is None:
            await ctx.send(embed=emb("❌ No Character", "You have no character here. `!idle join <class>` makes one.", C_RED))
            return None
        self._seen(ctx.guild, ctx.author.id, char, int(time.time()))
        return char

    def _match_player(self, guild, who: str) -> "tuple[int | None, str | None]":
        """(uid, None), or (None, what to tell the caller). Names are the
        point: the game pings nobody, and a mention in a command pings its
        target before the bot has read it — so a typed `@Name`, part of a
        name, or an id all work (a real mention still resolves; refusing it
        wouldn't un-send the ping). An exact name wins; then members with a
        character here are preferred, which settles most clashes; what is
        still ambiguous is listed rather than guessed at."""
        text = who.strip().lstrip("@").strip()
        digits = text.strip("<@!>")
        if digits.isdigit():
            return int(digits), None       # an id may belong to someone who already left
        if not text:
            return None, "Give me a name."
        query = text.lower()
        named = [m for m in guild.members if not m.bot and query in (m.display_name.lower(), m.name.lower())]
        found = named or [m for m in guild.members if not m.bot and (query in m.display_name.lower() or query in m.name.lower())]
        playing = [m for m in found if m.id in self._chars(guild.id)]
        found = playing or found
        if len(found) == 1:
            return found[0].id, None
        if not found:
            return None, f"Nobody here is called `{text}`."
        names = ", ".join(f"**{discord.utils.escape_markdown(m.display_name)}**" for m in found[:6])
        more = f" and {len(found) - 6} more" if len(found) > 6 else ""
        return None, f"`{text}` could be {names}{more}. Give me a little more of the name."

    async def _find_member(self, ctx, who):
        """The member a player command was pointed at; replies and returns
        None when the name doesn't settle on one. (Tests hand in members.)"""
        if not isinstance(who, str):
            return who
        uid, problem = self._match_player(ctx.guild, who)
        member = ctx.guild.get_member(uid) if uid else None
        if member is None:
            await ctx.send(embed=emb("❌ Who?", problem or f"Nobody here has the id `{uid}`.", C_RED))
        return member

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
        if not char["claimed"]:
            lines.append("*Unclaimed — its player can take it up with `!idle join <class>`.*")
        if self._in_voice(guild, uid):
            lines.append(f"🎙️ **In voice:** levelling and gold {rpg.VOICE_BONUS_PCT}% faster")
        boost = self._boost(guild, char, now)
        if boost:
            own = f", {char['boost_pct']}% of it theirs until <t:{char['boost_until']}:t>" if now < char.get("boost_until", 0) else ""
            lines.append(f"✨ **Boosted:** levelling and gold {boost}% faster{own} — `!idle world`")
        if rpg.hunting(char):
            if char.get("hunt_x") is not None and char.get("x") is not None:
                # Silent for the whole walk, so say how far: an hour of quiet
                # at the journey pace looked like a hang.
                steps = rpg.steps_to(char, (char["hunt_x"], char["hunt_y"]))
                how = (f"walking to [{char['hunt_x']}, {char['hunt_y']}], {steps} squares, about "
                       f"{format_duration(int(steps / rpg.JOURNEY_STEP_CHANCE))} away")
            else:
                how = "hunting"
            lines.append(f"📜 **Hunt:** {char['hunt_killed']}/{char['hunt_count']} {char['hunt_mob']}s — {how}")
        if char.get("loot"):
            pieces = len(char["loot"])
            lines.append(f"🎒 **Bag:** {pieces} piece{'' if pieces == 1 else 's'} worth {rpg.loot_value(char):,} gold at a market")
        if char.get("x") is not None:
            here = rpg.landmark_at((char["x"], char["y"]))
            town, away = rpg.nearest_town(char)
            market = f"market open ({town})" if away <= rpg.MARKET_RADIUS else f"nearest market: {town}, {away} squares"
            where = f"at {here}" if here else f"in {rpg.biome_at(char['x'], char['y'])} country"
            lines.insert(2, f"**Position:** [{char['x']}, {char['y']}] — {where} · {market}")
            if char.get("travel_to") in rpg.LANDMARKS:
                eta = format_duration(rpg.travel_eta_secs(char, char["travel_to"]))
                lines.insert(3, f"**Travelling to:** {char['travel_to']} — about {eta} of walking left")
        if char["gambles"]:
            lines.append(f"**At the tables:** {char['gambles']:,} bets · won {char['gamble_won']:,} · lost {char['gamble_lost']:,}")
        bar = "█" * (rpg.hp_pct(char) // 10) + "░" * (10 - rpg.hp_pct(char) // 10)
        lines.insert(2, f"**Health:** `{bar}` {rpg.hp_of(char):,}/{rpg.max_hp(char):,}"
                        + (" — resting, too hurt to fight" if rpg.needs_rest(char) else ""))
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

    @commands.group(name="idle", aliases=["irpg"], invoke_without_command=True,
                    help="Show your character sheet and the map, with buttons for what you can do right now")
    async def cmd_idle(self, ctx: commands.Context):
        """!idle join|status|items|map|travel|shop|gamble|top|align|duel|world|bless|title|lore|quest|prestige|leave|rules — each also works bare (`!map`)"""
        if not await self._ready(ctx, need_channel=True):
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
        await self._send_with_map(ctx, self._sheet(ctx.guild, ctx.author.id, char, now), highlight=(ctx.author.id,),
                                  view=_ActionView(self, ctx, self._available(ctx.guild, ctx.author.id)))

    @cmd_idle.command(name="join",
                      help="Start a character with a class you invent, or claim the one already playing in your name",
                      usage="<class>")
    async def cmd_join(self, ctx: commands.Context, *, class_name: str = None):
        if not await self._ready(ctx, need_channel=True):
            return

        async def send(embed: discord.Embed, *, error: bool = False) -> None:
            await ctx.send(embed=embed)
        await self._join(ctx.guild, ctx.author, class_name, ctx.channel, send)

    async def _join(self, guild, member, class_name: "str | None", here, send) -> None:
        """`!idle join` and the help card's Join button. `send(embed, error=)`
        is the reply path — a refusal is flagged so the button can answer it
        privately; `here` is where the reply lands, so the join news isn't
        posted twice under it."""
        gid, uid = guild.id, member.id
        chars = self._chars(gid)
        waiting = chars.get(uid)
        if waiting is not None and waiting["claimed"]:
            feed = f" Your feed is <#{waiting['thread_id']}>." if waiting["thread_id"] else ""
            await send(emb("❌ Already Adventuring", f"You already have a character here.{feed}", C_RED), error=True)
            return
        if not class_name:
            head = (
                f"A level {waiting['level']} {waiting['class']} has been playing in your name. Name a class to make it yours: "
                if waiting is not None else "Say what you are: "
            )
            await send(emb(
                "❌ Pick a Class",
                f"{head}`!idle join <class>`. It's yours to invent, up to {CLASS_MAX} characters — "
                "`!idle join Drunken Bard`, `!idle join Tax Wizard`.",
                C_RED,
            ), error=True)
            return
        class_name = _clean_class(class_name)
        if class_name is None:
            await send(emb(
                "❌ Pick a Class",
                f"A class is up to {CLASS_MAX} characters: letters, digits, spaces, `'` and `-`.",
                C_RED,
            ), error=True)
            return

        now = int(time.time())
        if state.idle_optouts.get(gid) and uid in state.idle_optouts[gid]:
            state.idle_optouts[gid].discard(uid)
            await persistence.delete_idle_optout(gid, uid)
        if waiting is not None:
            await self._claim(guild, uid, waiting, class_name, now, send)
            return
        char = rpg.new_character(class_name, now)
        rpg.ensure_position(char, self.rng)
        chars[uid] = char   # claimed before the first await: a second !idle join sees it
        await persistence.save_idle_character(gid, uid)
        channel = self._channel(guild)
        thread = await self._create_thread(guild, channel, uid, char)
        name = self._namer(guild)(uid)
        first = format_duration(rpg.ttl(0))
        if thread is not None:
            where = f"Your story unfolds in {thread.mention}."
        else:
            where = f"I couldn't open your feed thread — check that I have **Create Public Threads** in {channel.mention}. You're in the game regardless."
        await send(emb(
            "⚔️ A New Adventurer",
            f"{name} the {class_name} sets out. Level 1 is {first} away.\n{where}",
            C_GREEN,
        ))
        if here.id != channel.id:
            await self._send(channel, [f"🆕 {name} the {class_name} has joined the realm. Level 1 in {first}."])

    async def _claim(self, guild, uid: int, char: dict, class_name: str, now: int, send) -> None:
        """`!idle join <class>` by a member the bot enrolled: the character
        keeps everything it has earned, takes the class for free, and gets
        its feed thread — the player asked, so being added to it is fair."""
        gid = guild.id
        char["claimed"], char["class"] = True, class_name   # before the first await: a second join sees "claimed"
        self._seen(guild, uid, char, now)
        await persistence.save_idle_character(gid, uid)
        channel = self._channel(guild)
        thread = await self._create_thread(guild, channel, uid, char)
        where = (
            f"Your story unfolds in {thread.mention}." if thread is not None
            else f"I couldn't open your feed thread — check that I have **Create Public Threads** in {channel.mention}."
        )
        await send(emb(
            "⚔️ Character Claimed",
            f"{self._namer(guild)(uid)} takes up their level {char['level']} adventurer as a **{class_name}** — "
            f"items, gold and all.\n{where}",
            C_GREEN,
        ))

    @cmd_idle.command(name="status", aliases=["info"],
                      help="Show a character sheet and the map — yours, or another player's by name")
    async def cmd_status(self, ctx: commands.Context, *, member: str = None):
        if not await self._ready(ctx):
            return
        target = await self._find_member(ctx, member) if member else ctx.author
        if target is None:
            return
        char = self._chars(ctx.guild.id).get(target.id)
        if char is None:
            await ctx.send(embed=emb("❌ No Character", f"{target.display_name} has no character here.", C_RED))
            return
        now = int(time.time())
        if target.id == ctx.author.id:
            self._seen(ctx.guild, target.id, char, now)
        await self._send_with_map(ctx, self._sheet(ctx.guild, target.id, char, now), highlight=(target.id,),
                                  view=_ActionView(self, ctx, self._available(ctx.guild, ctx.author.id)))

    @cmd_idle.command(name="map", help="Show the map of the realm with every character on it and your own route")
    async def cmd_map(self, ctx: commands.Context):
        if not await self._ready(ctx):
            return
        # The picture alone: an embed would shrink it to the embed's width.
        await ctx.send(file=await self._map_file(ctx.guild, highlight=(ctx.author.id,), viewer=ctx.author.id))

    @cmd_idle.command(name="lore", help="Read the lore of a place on the map; bare lists the places", usage="[place]")
    async def cmd_lore(self, ctx: commands.Context, *, where: str = None):
        if not await self._ready(ctx):
            return
        place = rpg.match_place(where) if where else None
        if place is None:
            listed = "\n".join(f"• {name}" for name in rpg.LANDMARKS)
            await ctx.send(embed=emb(
                "📜 Lore",
                ("Nowhere on the map goes by that name.\n\n" if where else "")
                + f"`!idle lore <place>`\n{listed}",
                C_GREY if where else C_BLUE,
            ))
            return
        kind = "a market town" if place in rpg.TOWNS else f"{rpg.biome_at(*rpg.LANDMARKS[place])} country"
        await ctx.send(embed=emb(
            f"📜 {place}",
            f"{rpg.LORE[place]}\n\n*{kind}, at {list(rpg.LANDMARKS[place])} — `!idle travel {place}`*",
            C_BLUE,
        ))

    @cmd_idle.command(name="world", aliases=["boost"],
                      help="Say what's happening to the realm, the blessings running and how much faster you're levelling")
    async def cmd_world(self, ctx: commands.Context):
        if not await self._ready(ctx):
            return
        now = int(time.time())
        events = self._events(ctx.guild.id)
        row = rpg.world_row(events)
        if row is None:
            lines = ["The realm is quiet. Something happens to it every few days, and there is no telling what."]
        else:
            rules = rpg.WORLD_EVENTS[row["kind"]]
            if row["starts_at"] > now:
                lines = [rules.omen.format(detail=row["detail"]), f"It arrives <t:{row['starts_at']}:R>."]
            else:
                lines = [rules.begins.format(detail=row["detail"]), f"It passes <t:{row['ends_at']}:R>."]
        lines.append(f"\n🕊️ {rpg.bless_line(rpg.bless_count(events, now))}")
        lines.append(f"`!idle bless` buys the whole server an hour of it for {rpg.BLESS_COST:,} gold.")
        char = self._chars(ctx.guild.id).get(ctx.author.id)
        if char is not None:
            boost = self._boost(ctx.guild, char, now)
            lines.append("\nYou are levelling and earning at the usual rate." if not boost
                         else f"\nYou are levelling and earning **{boost}% faster**.")
        await ctx.send(embed=emb("🌍 The Realm Today", "\n".join(lines), C_BLUE))

    @cmd_idle.command(name="bless",
                      help=f"Spend {rpg.BLESS_COST:,} gold on {rpg.BLESS_BOOST_PCT}% faster levelling and gold for everyone here, for {format_duration(rpg.BLESS_SECS)}")
    async def cmd_bless(self, ctx: commands.Context):
        if not await self._ready(ctx, need_channel=True):
            return
        char = await self._own_char(ctx)
        if char is None:
            return
        gid, uid = ctx.guild.id, ctx.author.id
        now = int(time.time())
        if char["gold"] < rpg.BLESS_COST:
            await ctx.send(embed=emb(
                "❌ Not Enough Gold",
                f"A blessing costs **{rpg.BLESS_COST:,}** gold and you have **{char['gold']:,}**.",
                C_RED,
            ))
            return
        agreed = await confirm_prompt(
            ctx,
            title="🕊️ Bless the Realm",
            description=(
                f"**{rpg.BLESS_COST:,}** of your gold buys **everyone** here "
                f"{rpg.BLESS_BOOST_PCT}% faster levelling and gold for {format_duration(rpg.BLESS_SECS)} — "
                f"you included, and stacking with anyone else's up to {rpg.BLESS_MAX}.\n\n"
                f"{rpg.bless_line(rpg.bless_count(self._events(gid), now))}\n"
                f"You have **{char['gold']:,}** gold."
            ),
            payer=ctx.author,
        )
        if not agreed:
            return
        # The prompt was a long await: the character, and its purse, again.
        char = self._chars(gid).get(uid)
        if char is None:
            return
        cast, text = rpg.cast_bless(uid, char, self._events(gid), int(time.time()))
        if not cast:
            await ctx.send(embed=emb("❌ Not Enough Gold", text, C_RED))
            return
        await persistence.save_idle_character(gid, uid)
        await persistence.save_idle_guild_events(gid)
        await ctx.send(embed=emb("🕊️ Blessed", f"{text}\nYou have {char['gold']:,} gold left.", C_GREEN))
        channel = self._channel(ctx.guild)
        if channel is not None:
            await self._send(channel, [f"🕊️ {self._namer(ctx.guild)(uid)} has blessed the realm. {text}"])

    @cmd_idle.command(name="title", aliases=["titles"],
                      help="List your titles and how far off the rest are, or wear one (none takes it off)",
                      usage="[name|none]")
    async def cmd_title(self, ctx: commands.Context, *, which: str = None):
        if not await self._ready(ctx):
            return
        char = await self._own_char(ctx)
        if char is None:
            return
        gid, uid = ctx.guild.id, ctx.author.id
        earned = char.get("titles") or []
        if which is None:
            lines = []
            for key, title, need, read in rpg.TITLES:
                worn = " ← worn" if char.get("title") == key else ""
                lines.append(f"🎖️ **{title}**{worn}" if key in earned
                             else f"🔒 {title} — {read(char):,} of {need:,}")
            lines.append("\n`!idle title <name>` to wear one, `!idle title none` to take it off.")
            await ctx.send(embed=emb(f"🎖️ {ctx.author.display_name}'s Titles", "\n".join(lines), C_BLUE))
            return
        wanted = which.strip().lower()
        if wanted in ("none", "clear", "off"):
            char["title"] = None
            await persistence.save_idle_character(gid, uid)
            await ctx.send(embed=emb("🎖️ Titles", "You go by your own name again.", C_GREEN))
            return
        picked = next((key for key in earned if rpg.TITLE_NAMES[key].lower() == wanted or key == wanted), None)
        if picked is None:
            await ctx.send(embed=emb(
                "❌ No Such Title",
                "You haven't earned that one. `!idle title` lists what you have and what's left.",
                C_RED,
            ))
            return
        char["title"] = picked
        await persistence.save_idle_character(gid, uid)
        self._rename_due.add((gid, uid))
        await ctx.send(embed=emb("🎖️ Titles", f"You are **{rpg.TITLE_NAMES[picked]}** from now on.", C_GREEN))

    @cmd_idle.command(name="items",
                      help="List a character's items by slot, item power, gold and bag — yours, or another player's")
    async def cmd_items(self, ctx: commands.Context, *, member: str = None):
        if not await self._ready(ctx):
            return
        target = await self._find_member(ctx, member) if member else ctx.author
        if target is None:
            return
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
                mark = "✨ " if item["name"] in rpg.UNIQUE_NAMES else ""
                lines.append(f"**{slot.title()}:** {mark}{item['name']} (level {item['level']})")
            else:
                lines.append(f"**{slot.title()}:** level {item['level']}")
        lines.append(f"\n**Item power:** {rpg.item_sum(char):,} · **Gold:** {char['gold']:,}")
        bag = char.get("loot") or []
        if bag:
            carried = ", ".join(f"{item['name'] or item['slot']} ({item['level']})" for item in bag)
            lines.append(f"\n🎒 **Bag** ({len(bag)}/{rpg.LOOT_MAX}) — {carried}\n"
                         f"Worth **{rpg.loot_value(char):,}** gold; sold on the next town errand, or with `!idle shop sell`.")
        await ctx.send(embed=emb(f"{self._title(ctx.guild, target.id, char)} — Items", "\n".join(lines), C_BLUE))

    @cmd_idle.command(name="top", help="Show this server's idle ladder")
    async def cmd_top(self, ctx: commands.Context):
        if not await self._ready(ctx):
            return
        now = int(time.time())
        ranked = rpg.ladder(self._chars(ctx.guild.id), now, LEADERBOARD_SIZE)
        if not ranked:
            await ctx.send(embed=emb("🏔️ Idle Ladder", "Nobody is adventuring here yet. `!idle join <class>`.", C_GREY))
            return
        name = self._namer(ctx.guild)
        lines = []
        for i, (uid, char) in enumerate(ranked, 1):
            stars = "★" * char["prestige"] + " " if char["prestige"] else ""
            clock = "⏸️ paused" if rpg.is_paused(char) else f"next <t:{char['next_level_at']}:R>"
            unclaimed = "" if char["claimed"] else " · *unclaimed*"
            lines.append(f"**{i}.** {name(uid)} — {stars}Lv {char['level']} {char['class']} · {clock}{unclaimed}")
        await ctx.send(embed=emb("🏔️ Idle Ladder", "\n".join(lines), C_GOLD))

    @cmd_idle.command(name="align", aliases=["alignment"],
                      help="Set your alignment (a law and a moral), once a day; bare opens a picker",
                      usage="[lawful|neutral|chaotic] [good|neutral|evil]")
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

    @cmd_idle.command(name="duel",
                      help=f"Challenge a player to a duel once a day — the loser gives {rpg.DUEL_PCT}% of their clock, and gold if wagered",
                      usage="<name> [gold]")
    async def cmd_duel(self, ctx: commands.Context, member: str = None, wager: str = None):
        if not await self._ready(ctx, need_channel=True):
            return
        char = await self._own_char(ctx)
        if char is None:
            return
        if member is None:
            await ctx.send(embed=emb("❌ Usage", "`!idle duel <name> [gold]` — one challenge a day; add an amount to bet gold on it. Put a name with spaces in quotes: `!idle duel \"Rat King\" 200`.", C_RED))
            return
        member = await self._find_member(ctx, member)
        if member is None:
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
                problem = "The wager is an amount of gold — `!idle duel <name> 200`."
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
        # Fought and settled in one breath; what follows is only the telling,
        # so a restart mid-story costs nothing but the story.
        story, notes = rpg.duel(uid, member.id, chars, self.rng, self._namer(ctx.guild), int(time.time()), stake)
        await persistence.save_idle_character(gid, uid)
        await persistence.save_idle_character(gid, member.id)
        await ctx.send(embed=emb("🤺 Duel", story[0], C_GOLD))
        await self._narrate(ctx.guild, (uid, member.id), story)
        await ctx.send(embed=emb("🤺 Duel", "\n".join(n.text for n in notes), C_GOLD))
        await self._deliver(ctx.guild, notes)

    async def _narrate(self, guild, uids, story: list) -> None:
        """Play a settled fight out in the duellists' own feeds, a beat
        between rounds. Their threads only — the channel hears nothing."""
        channel = self._channel(guild)
        if channel is None:
            return
        threads = []
        for uid in uids:
            thread = await self._thread_for(guild, channel, uid)
            if thread is not None:
                threads.append(thread)
        for round_no, line in enumerate(story):
            if round_no:
                await asyncio.sleep(DUEL_ROUND_SECS)
            for thread in threads:
                await self._send(thread, [line])

    # ── !idle gamble ─────────────────────────────────────────────────────

    @cmd_idle.command(name="gamble", aliases=["bet"],
                      help=f"Bet gold at even money at a town's tables — the house wins {rpg.GAMBLE_LOSE_BELOW} in 100; bare offers stakes",
                      usage="[gold|half|all]")
    async def cmd_gamble(self, ctx: commands.Context, amount: str = None):
        if not await self._ready(ctx, need_channel=True):
            return
        char = await self._own_char(ctx)
        if char is None:
            return
        if amount is None:
            terms = (
                f"`!idle gamble <gold|half|all>` — even money, the house wins {rpg.GAMBLE_LOSE_BELOW} rolls in 100. "
                f"Only within {rpg.MARKET_RADIUS} squares of a town. You have **{char['gold']:,}** gold.\n"
                f"In town your character also bets on its own now and then — never more than "
                f"{rpg.GAMBLE_VISIT_CAP_PCT}% of the purse it arrived with per visit."
            )
            gold = char["gold"]
            picked = await confirm_choice(
                ctx, title="🎲 The Tables", description=terms,
                choices=[
                    {"label": f"Quarter ({gold // 4:,})", "value": str(gold // 4)},
                    {"label": f"Half ({gold // 2:,})", "value": "half", "default": True},
                    {"label": f"All ({gold:,})", "value": "all"},
                    {"label": "Other amount…", "value": "other"},
                ],
                payer=ctx.author, not_yours=NOT_YOURS,
            )
            if picked is None:
                return
            if picked == "other":
                values = await open_form(
                    ctx, title="🎲 The Tables", description=f"You have **{gold:,}** gold.",
                    fields=[Field("amount", "Stake", placeholder="200, 1.5k, half, all", max_length=12)], button="Bet…",
                )
                if not values or not values.get("amount"):
                    return
                picked = values["amount"]
            # Re-read the purse: the prompt was a long await and the character
            # keeps playing (see CLAUDE.md: Idle RPG, markets).
            char = await self._own_char(ctx)
            if char is None:
                return
            amount = picked
        word = amount.lower()
        stake = char["gold"] if word == "all" else char["gold"] // 2 if word == "half" else parse_int_amount(amount)
        if stake is None:
            await ctx.send(embed=emb("❌ The Tables", "`!idle gamble <gold|half|all>` — `200`, `1.5k`, `half`, `all`.", C_RED))
            return
        won, text = rpg.manual_gamble(char, stake, self.rng)
        if won is None:
            await ctx.send(embed=emb("❌ The Tables", text, C_RED))
            return
        await persistence.save_idle_character(ctx.guild.id, ctx.author.id)
        await ctx.send(embed=emb("🎲 The Tables", text, C_GREEN if won else C_GREY))

    # ── !idle travel ─────────────────────────────────────────────────────

    @cmd_idle.command(name="travel",
                      help="Walk your character to a town or wild place on the map (stop to wander again); bare lists them",
                      usage="[place|stop]")
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
                (
                    f"{place} ({'market' if place in rpg.TOWNS else rpg.biome_at(*rpg.LANDMARKS[place])}) — "
                    f"{rpg.travel_steps(char, place)} squares, about {format_duration(rpg.travel_eta_secs(char, place))}"[:100],
                    place,
                )
                for place in sorted(rpg.LANDMARKS, key=lambda p: rpg.travel_steps(char, p))
            ]
            if char.get("travel_to"):
                options.append(("Stop travelling — wander again", "stop"))
            going = f"You're walking to **{char['travel_to']}**." if char.get("travel_to") else "You're wandering."
            picked = await pick_from_list(
                ctx,
                title="🧭 Travel",
                description=(
                    f"{going} Pick a place and your character stops wandering and walks there, a step every "
                    f"{int(1 / rpg.JOURNEY_STEP_CHANCE)} seconds or so. Towns have markets; the rest is wilderness, where the monsters are. "
                    "Travellers meet no other players on the road, but monsters find them all the same.\n\n"
                    "Typed: `!idle travel velvragh` · `!idle travel trnalvph` · `!idle travel stop`"
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
        town = rpg.match_place(where)
        if town is None:
            wilds = [place for place in rpg.LANDMARKS if place not in rpg.TOWNS]
            await ctx.send(embed=emb(
                "❌ Travel",
                f"**Towns:** {', '.join(rpg.TOWNS)}\n**Wilds:** {', '.join(wilds)}\n`!idle travel stop` to wander.",
                C_RED,
            ))
            return
        quest = self._quest(gid)
        if quest.get("kind") == "journey" and uid in quest["members"]:
            await ctx.send(embed=emb("❌ Travel", "You're on a journey quest — it decides where you walk until it's done.", C_RED))
            return
        if rpg.travel_steps(char, town) == 0:
            await ctx.send(embed=emb("🧭 Travel", f"You're already {'in' if town in rpg.TOWNS else 'at'} {town}.", C_GREY))
            return
        char["travel_to"] = town
        await persistence.save_idle_character(gid, uid)
        embed = emb(
            "🧭 Travel",
            f"You set out for **{town}**: {rpg.travel_steps(char, town)} squares, about "
            f"{format_duration(rpg.travel_eta_secs(char, town))} of walking while you're online. "
            + ("You can shop as soon as you're inside its ring. " if town in rpg.TOWNS
               else f"It's {rpg.biome_at(*rpg.LANDMARKS[town])} country — once there you wander again, among its monsters. ")
            + "`!idle travel stop` to wander again.",
            C_GREEN,
        )
        await self._send_with_map(ctx, embed, highlight=(uid,))

    # ── !idle shop ───────────────────────────────────────────────────────

    @staticmethod
    def _shop_lines(char: dict) -> list:
        prices = rpg.shop_prices(char)
        bag = char.get("loot") or []
        return ([("sell", f"Sell — empty your bag of {len(bag)} piece{'' if len(bag) == 1 else 's'} · +{rpg.loot_value(char):,} gold")] if bag else []) + [
            ("find", f"Find — one more item roll · {prices['find']:,} gold"),
            ("sharpen", f"Sharpen — +{rpg.SHARPEN_PCT}% to one of your items · {rpg.PRICE_SHARPEN_PER_ITEM_LEVEL} gold per item level"),
            ("rush", f"Rush — {rpg.RUSH_PCT}% off your clock, once a day · {prices['rush']:,} gold"),
            ("duel", f"Second duel — once a day · {prices['duel']:,} gold"),
            ("class", f"New class — `!idle shop class <name>` · {prices['class']:,} gold"),
        ]

    @cmd_idle.command(name="shop",
                      help="Spend gold at a town's market — sell your bag, find or sharpen an item, a rush, a duel, a new class",
                      usage="[sell|find|sharpen [slot]|rush|duel|class <name>|auto <on|off>]")
    async def cmd_shop(self, ctx: commands.Context, item: str = None, *, arg: str = None):
        if not await self._ready(ctx, need_channel=True):
            return
        char = await self._own_char(ctx)
        if char is None:
            return
        gid, uid = ctx.guild.id, ctx.author.id
        usage = "`!idle shop sell` · `find` · `sharpen <slot>` · `rush` · `duel` · `class <name>` · `auto on|off`"
        if item is not None and item.lower() == "auto":
            choice = (arg or "").lower()
            if choice not in ("on", "off"):
                state_now = "on" if char["auto_trade"] else "off"
                await ctx.send(embed=emb(
                    "🛒 Idle Shop",
                    f"Auto-trading is **{state_now}**. In a town's centre your character spends up to "
                    f"{rpg.AUTO_TRADE_BUDGET_PCT}% of its gold on a find and a sharpening, at most once every "
                    f"{format_duration(rpg.AUTO_TRADE_COOLDOWN_SECS)}. `!idle shop auto on|off`\n"
                    "Your bag is emptied on that same errand either way — selling is income, not spending.",
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
        if item == "sell":
            pieces, paid = rpg.sell_loot(char)
            bought = bool(pieces)
            text = (f"Sold {pieces} piece{'' if pieces == 1 else 's'} for +{paid:,} gold."
                    if pieces else "Your bag is empty.")
        elif item == "find":
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

    @cmd_idle.command(name="quest",
                      help="Show the running quest and its party, or what it takes for the next one to start")
    async def cmd_quest(self, ctx: commands.Context):
        if not await self._ready(ctx, need_channel=True):
            return
        now = int(time.time())
        quest = self._quest(ctx.guild.id)
        name = self._namer(ctx.guild)
        if rpg.quest_active(quest):
            party = rpg.and_list([name(u) for u in quest["members"]])
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

    @cmd_idle.command(name="prestige",
                      help=f"At level {rpg.PRESTIGE_LEVEL}, start over at level 0 without items for a ★ and {rpg.PRESTIGE_BONUS_PCT}% faster levelling for good")
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
        perk = ("another ★ — you're at the speed cap, so no further bonus" if maxed
                else f"a ★ and levelling {rpg.PRESTIGE_BONUS_PCT}% faster, for good")
        if not await confirm_prompt(
            ctx,
            title="🌟 Prestige",
            description=f"You go back to **level 0** and lose **every item**. In return: {perk}.",
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

    @cmd_idle.command(name="leave", help="Retire your character for good, after a confirm")
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
            description=f"Your level {char['level']} {char['class']} and everything they carry are deleted for good.",
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
        # Remembered, or the enrollment sweep would hand them another in a minute.
        state.idle_optouts.setdefault(guild.id, set()).add(uid)
        await persistence.save_idle_optout(guild.id, uid)
        await persistence.delete_idle_character(guild.id, uid)
        await self._archive_thread(guild, char["thread_id"])
        return True

    @cmd_idle.command(name="rules", aliases=["help"],
                      help="Explain how the idle game works; add a topic for the details",
                      usage=f"[{'|'.join(_RULES_TOPICS)}]")
    async def cmd_rules(self, ctx: commands.Context, topic: str = None):
        detail = _RULES_TOPICS.get((topic or "").lower())
        if detail is not None:
            await ctx.send(embed=emb(f"📖 Idle RPG — {topic.title()}", detail, C_BLUE))
            return
        char = self._chars(ctx.guild.id).get(ctx.author.id) if ctx.guild else None
        if char is None:
            start = "▶️ **Start:** `!idle join <class>` — invent your class: `!idle join Drunken Bard`.\n\n"
        elif not char["claimed"]:
            start = f"▶️ **Start:** a level {char['level']} {char['class']} is already adventuring in your name — `!idle join <class>` makes it yours.\n\n"
        else:
            # Shown to players too: they are the ones who pass this card on.
            start = "▶️ **New here?** `!idle join <class>` starts a character — the class is yours to invent.\n\n"
        view = _HelpView(self)
        view.message = await ctx.send(embed=emb(
            "📖 Idle RPG",
            "**Do nothing. Level up.**\n\n"
            "⏳ Your character levels on a timer while you're online.\n"
            "⚔️ Items, fights and lucky breaks happen on their own.\n"
            "📜 High-level players get sent on quests for a big shortcut.\n\n"
            f"{start}"
            "`!idle status` · `items` · `map` · `travel` · `shop` · `gamble` · `top` · `align` · `duel <name>`\n"
            "`!idle world` · `bless` · `title` · `lore` · `quest` · `prestige` — each works bare too (`!map`, `!travel`, `!store`).\n"
            f"More: pick a topic below, or `!idle rules <{'|'.join(_RULES_TOPICS)}>`",
            C_BLUE,
        ), view=view)

    # ── !idle admin ──────────────────────────────────────────────────────

    @cmd_idle.group(name="admin", invoke_without_command=True, help="List the idle admin commands")
    async def cmd_admin(self, ctx: commands.Context):
        await ctx.send(embed=emb(
            "🛠️ Idle RPG Admin",
            "`!idle admin hog <name>` — a Hand of God, now\n"
            "`!idle admin push <name> <±time>` — move a clock (`-2h` sooner, `+1d` later)\n"
            "`!idle admin gold <name> <±amount>` — give or take gold\n"
            "`!idle admin move <name> <place|x y>` — set a character down somewhere\n"
            "`!idle admin remove <name>` — delete a character\n"
            "`!idle admin reset` — wipe this server's game\n"
            "Names, not mentions — part of a name is enough, and one with spaces goes in quotes: `!idle admin push \"Rat King\" -5m`.",
            C_GREY,
        ))

    async def _admin_target(self, ctx, who: "str | None", usage: str) -> "tuple[int, dict] | None":
        if not await self._ready(ctx):
            return None
        if not who:
            await ctx.send(embed=emb("❌ Idle Admin", f"Usage: {usage}", C_RED))
            return None
        uid, problem = self._match_player(ctx.guild, who)
        char = self._chars(ctx.guild.id).get(uid) if uid else None
        if char is None:
            await ctx.send(embed=emb("❌ Idle Admin", problem or f"They have no character here. Usage: {usage}", C_RED))
            return None
        return uid, char

    @cmd_admin.command(name="hog", help="Strike a character with a Hand of God right now", usage="<name>")
    async def cmd_admin_hog(self, ctx: commands.Context, *, who: str = None):
        found = await self._admin_target(ctx, who, "`!idle admin hog <name>`")
        if found is None:
            return
        uid, _char = found
        note = rpg.hand_of_god(uid, self._chars(ctx.guild.id), self.rng, self._namer(ctx.guild), int(time.time()))
        await persistence.save_idle_character(ctx.guild.id, uid)
        await ctx.send(embed=emb("🙌 Hand of God", note.text, C_GOLD))
        await self._deliver(ctx.guild, [note], skip_main=self._in_idle_channel(ctx))

    @cmd_admin.command(name="gold", help="Give a character gold, or take it with a leading -",
                       usage="<name> <±amount>")
    async def cmd_admin_gold(self, ctx: commands.Context, who: str = None, amount: str = None):
        usage = "`!idle admin gold <name> <±amount>`"
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

    @cmd_admin.command(name="move", aliases=["teleport", "tp"],
                       help="Set a character down at a named place or at x y coordinates", usage="<name> <place|x y>")
    async def cmd_admin_move(self, ctx: commands.Context, who: str = None, *, where: str = None):
        usage = "`!idle admin move <name> <place|x y>` — `velvragh`, `trnalvph`, or `120 300`"
        found = await self._admin_target(ctx, who, usage)
        if found is None:
            return
        uid, char = found
        parts = (where or "").split()
        if len(parts) == 2 and all(p.lstrip("-").isdigit() for p in parts):
            x, y = (int(p) for p in parts)
            if not (0 <= x <= rpg.MAP_SIZE and 0 <= y <= rpg.MAP_SIZE):
                await ctx.send(embed=emb("❌ Idle Admin", f"The realm runs 0–{rpg.MAP_SIZE} on both axes.", C_RED))
                return
            place = rpg.landmark_at((x, y)) or rpg.biome_at(x, y)
        else:
            spot = rpg.match_place(where or "")
            if spot is None:
                await ctx.send(embed=emb("❌ Idle Admin", f"Usage: {usage}\n**Places:** {', '.join(rpg.LANDMARKS)}", C_RED))
                return
            x, y = rpg.LANDMARKS[spot]
            place = spot
        char["x"], char["y"] = x, y
        char["travel_to"] = None      # they are there; nothing left to walk to
        await persistence.save_idle_character(ctx.guild.id, uid)
        market = rpg.market_in_reach(char)
        await ctx.send(embed=emb(
            "🛠️ Moved",
            f"{self._namer(ctx.guild)(uid)} now stands at **[{x}, {y}]** — {place}."
            + (f" {market}'s market is open here." if market else " Open country: monsters can find them."),
            C_GREEN,
        ))

    @cmd_admin.command(name="push", help="Move a character's clock: -2h brings the next level sooner, +1d later",
                       usage="<name> <±time>")
    async def cmd_admin_push(self, ctx: commands.Context, who: str = None, amount: str = None):
        usage = "`!idle admin push <name> <±time>` — `-2h` sooner, `+1d` later"
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
        await ctx.send(embed=emb(
            "🛠️ Clock Moved",
            f"{self._namer(ctx.guild)(uid)} has {format_duration(rpg.time_left(char, now))} to go. "
            f"{rpg.clock_delta(seconds) or '±0sec'}",
            C_GREEN,
        ))

    @cmd_admin.command(name="remove", help="Delete a player's character, after a confirm", usage="<name>")
    async def cmd_admin_remove(self, ctx: commands.Context, *, who: str = None):
        found = await self._admin_target(ctx, who, "`!idle admin remove <name>`")
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

    @cmd_admin.command(name="reset",
                       help="Delete every character, their items and the current quest in this server, after a confirm")
    async def cmd_admin_reset(self, ctx: commands.Context):
        if not await self._ready(ctx):
            return
        gid = ctx.guild.id
        count = len(self._chars(gid))
        if not await confirm_prompt(
            ctx,
            title="💥 Reset the Idle RPG",
            description=f"This deletes all **{count}** character{'' if count == 1 else 's'}, their items and the current quest in this server. There is no undo.",
            payer=ctx.author,
            not_yours=NOT_YOURS,
        ):
            return
        chars = state.idle_characters.pop(gid, {})
        state.idle_quests.pop(gid, None)
        # The rows go with the DB's; leaving them would write a blood moon
        # back out on the next flush, over a realm that no longer has anyone in it.
        state.idle_guild_events.pop(gid, None)
        self._dirty_events.discard(gid)
        self._pending.pop(gid, None)
        await persistence.delete_idle_guild(gid)
        for char in chars.values():
            await self._archive_thread(ctx.guild, char["thread_id"])
        gone = len(chars)
        await ctx.send(embed=emb("💥 Idle RPG Reset", f"{gone} character{'' if gone == 1 else 's'} deleted. A new age begins.", C_GREY))


async def setup(bot):
    await bot.add_cog(IdleCog(bot))
