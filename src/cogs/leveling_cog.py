"""
Leveling system
===============
XP sources and caps:
  - Non-command message : 10 XP,  max 1 grant/hour,  max 5 grants/day
  - Command             :  5 XP,  max 1 grant/hour,  max 5 grants/day
  - Voice activity      :  5 XP,  max 1 grant/15 min, max 32 grants/day

Level curve: total XP required for level N = 100 * N * (N+1) / 2
  Level 1 → 100 XP total
  Level 2 → 300 XP total
  Level 3 → 600 XP total  (each level costs 100*(level) more than the last)
"""
import time

import discord
from discord.ext import commands, tasks

from src.helpers import emb, C_BLUE, C_GREEN, C_GOLD, MemberConverter
from src.persistence import save_leveling, get_guild_cfg, save_guild_settings
from src.economy import add_balance, next_daily_reset_ts
from src.permissions import check_command_permission
from src import state

# ── XP constants ──────────────────────────────────────────────────────────────
XP_MESSAGE   = 10
XP_COMMAND   =  5
XP_VOICE     = 10
XP_SCRATCH   =  5
XP_STREAM    = 15

MSG_HOURLY_MAX    = 1
MSG_DAILY_MAX     = 5
CMD_HOURLY_MAX    = 1
CMD_DAILY_MAX     = 5
VOICE_30MIN_MAX   = 1
VOICE_DAILY_MAX   = 16
STREAM_HOURLY_MAX = 1
STREAM_DAILY_MAX  = 3

HOUR_SECS   = 3600
MINS30_SECS = 1800
MINS15_SECS =  900
DAY_SECS    = 86400


# ── Level math ────────────────────────────────────────────────────────────────
# XP cost to go from level n to level n+1: 50 + n^1.9
# Total XP to reach level n = sum(50 + i^1.9 for i in range(n))

def _xp_cost(n: int) -> int:
    """XP required to advance from level n to level n+1."""
    return int(100 + n ** 1.9 * 2)


def xp_for_level(n: int) -> int:
    """Total XP required to *reach* level n (level 0 = 0 XP)."""
    return sum(_xp_cost(i) for i in range(n))


def level_from_xp(xp: int) -> int:
    """Level a user is at given total XP."""
    if xp <= 0:
        return 0
    level = 0
    total = 0
    while True:
        cost = _xp_cost(level)
        if total + cost > xp:
            return level
        total += cost
        level += 1


def xp_for_next_level(level: int) -> int:
    """Total XP required to reach level+1."""
    return xp_for_level(level + 1)


def display_level(internal_level: int) -> int:
    """Convert 0-based internal level to the 1-based level shown to users."""
    return internal_level + 1


def levelup_coin_reward(display_lvl: int) -> int:
    """Coins awarded on reaching a given display level."""
    if display_lvl <= 4:   tier = 0
    elif display_lvl <= 9:  tier = 1
    elif display_lvl <= 29: tier = 2
    elif display_lvl <= 59: tier = 3
    elif display_lvl <= 99: tier = 4
    elif display_lvl <= 149: tier = 5
    else:                    tier = 6
    return 500 * (2 ** tier)


# ── User record helpers ───────────────────────────────────────────────────────
# Storage layout: state.leveling = {guild_id_str: {uid_str: {...}}}

def _ensure_user(guild_id: int, uid: int) -> dict:
    gkey = str(guild_id)
    ukey = str(uid)
    guild_data = state.leveling.setdefault(gkey, {})
    if ukey not in guild_data:
        guild_data[ukey] = {
            "xp": 0,
            "level": 0,
            # hourly / daily rate-limit tracking
            "msg_last_hour": 0.0,   # epoch of last msg XP grant
            "msg_today": 0,         # grants this calendar day
            "msg_day_ts": 0.0,      # epoch when msg_today was last reset
            "cmd_last_hour": 0.0,
            "cmd_today": 0,
            "cmd_day_ts": 0.0,
            "voice_last_30": 0.0,   # epoch of last voice XP grant
            "voice_today": 0,
            "voice_day_ts": 0.0,
            "stream_last_hour": 0.0,
            "stream_today": 0,
            "stream_day_ts": 0.0,
        }
    return guild_data[ukey]


def _day_reset(rec: dict, key_today: str, key_day_ts: str):
    """Reset daily counter if it's a new calendar day (UTC)."""
    now = time.time()
    last = rec.get(key_day_ts, 0.0)
    import datetime
    if datetime.datetime.utcfromtimestamp(now).date() > datetime.datetime.utcfromtimestamp(last).date():
        rec[key_today] = 0
        rec[key_day_ts] = now


async def grant_xp(uid: int, source: str, bot=None, guild_id: int = None) -> tuple[int, bool]:
    """
    Attempt to grant XP for *source* ('msg', 'cmd', 'voice').

    Returns (xp_granted, leveled_up).
    Does nothing and returns (0, False) if rate-limited or no guild_id.
    """
    if not guild_id:
        return 0, False
    rec = _ensure_user(guild_id, uid)
    now = time.time()

    if source == "msg":
        _day_reset(rec, "msg_today", "msg_day_ts")
        if now - rec["msg_last_hour"] < HOUR_SECS:
            return 0, False
        if rec["msg_today"] >= MSG_DAILY_MAX:
            return 0, False
        rec["msg_last_hour"] = now
        rec["msg_today"] += 1
        xp = XP_MESSAGE

    elif source == "cmd":
        _day_reset(rec, "cmd_today", "cmd_day_ts")
        if now - rec["cmd_last_hour"] < HOUR_SECS:
            return 0, False
        if rec["cmd_today"] >= CMD_DAILY_MAX:
            return 0, False
        rec["cmd_last_hour"] = now
        rec["cmd_today"] += 1
        xp = XP_COMMAND

    elif source == "voice":
        _day_reset(rec, "voice_today", "voice_day_ts")
        if now - rec.get("voice_last_30", 0.0) < MINS30_SECS:
            return 0, False
        if rec["voice_today"] >= VOICE_DAILY_MAX:
            return 0, False
        rec["voice_last_30"] = now
        rec["voice_today"] += 1
        xp = XP_VOICE

    elif source == "scratch":
        xp = XP_SCRATCH  # capped naturally by the scratchoff system (3/day)

    elif source == "stream":
        rec.setdefault("stream_last_hour", 0.0)
        rec.setdefault("stream_today", 0)
        rec.setdefault("stream_day_ts", 0.0)
        _day_reset(rec, "stream_today", "stream_day_ts")
        if now - rec["stream_last_hour"] < HOUR_SECS:
            return 0, False
        if rec["stream_today"] >= STREAM_DAILY_MAX:
            return 0, False
        rec["stream_last_hour"] = now
        rec["stream_today"] += 1
        xp = XP_STREAM

    else:
        return 0, False

    old_level = rec["level"]
    rec["xp"] += xp
    new_level = level_from_xp(rec["xp"])
    rec["level"] = new_level
    leveled_up = new_level > old_level
    await save_leveling()
    return xp, leveled_up


# ── XP bar renderer ───────────────────────────────────────────────────────────

def _bar(filled: int, total: int, width: int = 20) -> str:
    done = round(filled / total * width) if total else 0
    done = max(0, min(width, done))
    return "█" * done + "░" * (width - done)


# ── Cog ───────────────────────────────────────────────────────────────────────

class LevelingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._voice_task.start()

    def cog_unload(self):
        self._voice_task.cancel()

    async def _do_voice_tick(self):
        """Award voice/stream XP to every eligible member currently in a voice channel."""
        for guild in self.bot.guilds:
            cfg_cache = get_guild_cfg(guild.id)
            for vc in guild.voice_channels:
                # Skip private channels (any permission overrides that deny view access to @everyone)
                everyone = guild.default_role
                if vc.overwrites_for(everyone).view_channel is False:
                    continue

                # Skip channels with only 1 non-bot member
                human_members = [m for m in vc.members if not m.bot]
                if len(human_members) < 2:
                    continue

                for member in human_members:
                    vs = member.voice
                    if vs is None:
                        continue

                    # Voice XP: must not be muted/deafened
                    if not (vs.self_mute or vs.self_deaf or vs.mute or vs.deaf):
                        _, leveled_up = await grant_xp(member.id, "voice", guild_id=guild.id)
                        if leveled_up and cfg_cache.get("levelup_channel"):
                            await self._announce_levelup(member, guild.id)

                    # Stream XP: must currently be streaming; hourly rate-limit handled inside grant_xp
                    if vs.self_stream:
                        _, leveled_up = await grant_xp(member.id, "stream", guild_id=guild.id)
                        if leveled_up and cfg_cache.get("levelup_channel"):
                            await self._announce_levelup(member, guild.id)

    @tasks.loop(seconds=300)  # tick every 5 min; rate-limits inside grant_xp control actual XP frequency
    async def _voice_task(self):
        await self._do_voice_tick()

    @_voice_task.before_loop
    async def _before_voice_task(self):
        await self.bot.wait_until_ready()
        await self._do_voice_tick()  # fire immediately on startup, don't wait 5 min

    # ── Level-up announcement ─────────────────────────────────────────────────
    async def _announce_levelup(self, member: discord.Member, guild_id: int):
        rec = state.leveling.get(str(guild_id), {}).get(str(member.id), {})
        lvl = display_level(rec.get("level", 0))
        reward = levelup_coin_reward(lvl)
        await add_balance(member.id, reward)
        cfg = get_guild_cfg(guild_id)
        channel_id = cfg.get("levelup_channel")
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return
        await channel.send(embed=emb(
            "🎉 Level Up!",
            f"{member.mention} reached **Level {lvl}**! +**{reward:,} 🪙**",
            C_GOLD,
        ))

    # ── !level / !xp command ──────────────────────────────────────────────────
    @commands.command(name="lvl", aliases=["level", "xp"])
    async def cmd_level(self, ctx: commands.Context, member: MemberConverter = None):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Leveling is per-server and not available in DMs.", 0xe74c3c))
            return
        target = member or ctx.author
        uid = target.id
        rec = _ensure_user(ctx.guild.id, uid)

        xp    = rec["xp"]
        level = rec["level"]
        now   = time.time()

        # ── XP bar ────────────────────────────────────────────────────────────
        xp_this_level  = xp_for_level(level)
        xp_next_level  = xp_for_next_level(level)
        xp_in_level    = xp - xp_this_level          # progress within current level
        xp_needed      = xp_next_level - xp_this_level  # width of current level band
        bar = _bar(xp_in_level, xp_needed)

        # ── Rate-limit bars ───────────────────────────────────────────────────
        def _day_used(key_today: str, key_day_ts: str, cap: int) -> tuple[int, int]:
            _day_reset(rec, key_today, key_day_ts)
            return rec.get(key_today, 0), cap

        def _hour_remaining(last_key: str, used: int, cap: int) -> str:
            if used >= cap:
                return f"<t:{next_daily_reset_ts()}:R>"
            ready_at = int(rec.get(last_key, 0.0)) + HOUR_SECS
            if ready_at <= now:
                return "ready"
            return f"<t:{ready_at}:R>"

        def _mins30_remaining(used: int, cap: int) -> str:
            if used >= cap:
                return f"<t:{next_daily_reset_ts()}:R>"
            ready_at = int(rec.get("voice_last_30", 0.0)) + MINS30_SECS
            if ready_at <= now:
                return "ready"
            return f"<t:{ready_at}:R>"

        msg_used,    msg_cap    = _day_used("msg_today",    "msg_day_ts",    MSG_DAILY_MAX)
        cmd_used,    cmd_cap    = _day_used("cmd_today",    "cmd_day_ts",    CMD_DAILY_MAX)
        voice_used,  voice_cap  = _day_used("voice_today",  "voice_day_ts",  VOICE_DAILY_MAX)
        stream_used, stream_cap = _day_used("stream_today", "stream_day_ts", STREAM_DAILY_MAX)

        msg_bar    = _bar(msg_used,    msg_cap,    10)
        cmd_bar    = _bar(cmd_used,    cmd_cap,    10)
        voice_bar  = _bar(voice_used,  voice_cap,  10)
        stream_bar = _bar(stream_used, stream_cap, 10)

        desc = (
            f"**Level {display_level(level)}** — {xp:,} XP total\n"
            f"`{bar}` {xp_in_level:,} / {xp_needed:,} XP to level {display_level(level + 1)}\n\n"
            f"**Sources** *(daily used / cap)*\n"
            f"🎫 Scratch   **{XP_SCRATCH} XP**  per scratchoff (3/day)\n"
            f"⚡ Commands  **{XP_COMMAND} XP**  `{cmd_bar}` {cmd_used}/{cmd_cap}  · next: {_hour_remaining('cmd_last_hour', cmd_used, cmd_cap)}\n"
            f"💬 Messages  **{XP_MESSAGE} XP**  `{msg_bar}` {msg_used}/{msg_cap}  · next: {_hour_remaining('msg_last_hour', msg_used, msg_cap)}\n"
            f"🔊 Voice     **{XP_VOICE} XP**  `{voice_bar}` {voice_used}/{voice_cap}  · next: {_mins30_remaining(voice_used, voice_cap)}\n"
            f"📺 Stream    **{XP_STREAM} XP**  `{stream_bar}` {stream_used}/{stream_cap}  · next: {_hour_remaining('stream_last_hour', stream_used, stream_cap)}\n"
        )

        display = target.display_name
        embed = discord.Embed(
            title=f"📊 {display}'s Level",
            description=desc,
            color=C_BLUE,
        )
        if target.avatar:
            embed.set_thumbnail(url=target.display_avatar.url)
        await ctx.send(embed=embed)


    # ── !levels XP leaderboard ────────────────────────────────────────────────
    @commands.command(name="levels", aliases=["lbxp", "xplb", "lbx", "xlb", "lblvl", "lblevel"])
    async def cmd_levels(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Leveling is per-server and not available in DMs.", 0xe74c3c))
            return
        guild_data = state.leveling.get(str(ctx.guild.id), {})
        if not guild_data:
            await ctx.send(embed=emb("📊 XP Leaderboard", "No XP data yet.", C_BLUE))
            return

        sorted_users = sorted(guild_data.items(), key=lambda x: x[1].get("xp", 0), reverse=True)[:10]

        async def resolve_name(uid_str: str) -> str:
            from src.helpers import fetch_member
            member = await fetch_member(ctx.guild, int(uid_str))
            if member:
                return member.display_name
            try:
                user = await self.bot.fetch_user(int(uid_str))
                return user.display_name
            except Exception:
                return f"User {uid_str}"

        import asyncio
        names = await asyncio.gather(*(resolve_name(uid_str) for uid_str, _ in sorted_users))
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (name, (_, data)) in enumerate(zip(names, sorted_users)):
            prefix = medals[i] if i < 3 else f"{i + 1}."
            lines.append(f"{prefix} **{name}** — **Level {display_level(data.get('level', 0))}** ({data.get('xp', 0):,} XP)")
        lines.append("\n*Also: `!lb` currency · `!lbr` roles*")
        await ctx.send(embed=emb("📊 XP Leaderboard", "\n".join(lines), C_BLUE))


    # ── !migratelevels ────────────────────────────────────────────────────────
    @commands.command(name="migratelevels")
    async def cmd_migratelevels(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Run this in a server.", 0xe74c3c))
            return
        guild_data = state.leveling.get(str(ctx.guild.id), {})
        updated = 0
        for rec in guild_data.values():
            correct = level_from_xp(rec.get("xp", 0))
            if rec.get("level") != correct:
                rec["level"] = correct
                updated += 1
        if updated:
            await save_leveling()
        await ctx.send(embed=emb(
            "✅ Migration complete",
            f"Recomputed levels for **{updated}** user(s).",
            C_GREEN,
        ))


async def setup(bot):
    await bot.add_cog(LevelingCog(bot))
