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

from src.helpers import emb, C_BLUE, C_GREEN, C_GOLD
from src.persistence import save_leveling, get_guild_cfg, save_guild_settings
from src import state

# ── XP constants ──────────────────────────────────────────────────────────────
XP_MESSAGE   = 10
XP_COMMAND   =  5
XP_VOICE     =  5

MSG_HOURLY_MAX   = 1
MSG_DAILY_MAX    = 5
CMD_HOURLY_MAX   = 1
CMD_DAILY_MAX    = 5
VOICE_15MIN_MAX  = 1
VOICE_DAILY_MAX  = 32

HOUR_SECS   = 3600
MINS15_SECS =  900
DAY_SECS    = 86400


# ── Level math ────────────────────────────────────────────────────────────────
# XP cost to go from level n to level n+1: 50 + n^1.9
# Total XP to reach level n = sum(50 + i^1.9 for i in range(n))

def _xp_cost(n: int) -> int:
    """XP required to advance from level n to level n+1."""
    return int(25 + n ** 1.9 / 2)


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
            "voice_last_15": 0.0,   # epoch of last voice XP grant
            "voice_today": 0,
            "voice_day_ts": 0.0,
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


def grant_xp(uid: int, source: str, bot=None, guild_id: int = None) -> tuple[int, bool]:
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
        if now - rec["voice_last_15"] < MINS15_SECS:
            return 0, False
        if rec["voice_today"] >= VOICE_DAILY_MAX:
            return 0, False
        rec["voice_last_15"] = now
        rec["voice_today"] += 1
        xp = XP_VOICE

    else:
        return 0, False

    old_level = rec["level"]
    rec["xp"] += xp
    new_level = level_from_xp(rec["xp"])
    rec["level"] = new_level
    leveled_up = new_level > old_level
    save_leveling()
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

    # ── Voice XP loop: tick every 15 minutes ─────────────────────────────────
    @tasks.loop(seconds=MINS15_SECS)
    async def _voice_task(self):
        """Award voice XP to every non-bot member currently in a voice channel."""
        for guild in self.bot.guilds:
            for vc in guild.voice_channels:
                for member in vc.members:
                    if member.bot:
                        continue
                    xp, leveled_up = grant_xp(member.id, "voice", guild_id=guild.id)
                    if leveled_up and get_guild_cfg(guild.id).get("levelup_channel"):
                        await self._announce_levelup(member, guild.id)

    @_voice_task.before_loop
    async def _before_voice_task(self):
        await self.bot.wait_until_ready()

    # ── Level-up announcement ─────────────────────────────────────────────────
    async def _announce_levelup(self, member: discord.Member, guild_id: int):
        rec = state.leveling.get(str(guild_id), {}).get(str(member.id), {})
        lvl = rec.get("level", 0)
        cfg = get_guild_cfg(guild_id)
        channel_id = cfg.get("levelup_channel")
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return
        await channel.send(embed=emb(
            "🎉 Level Up!",
            f"{member.mention} reached **Level {lvl}**!",
            C_GOLD,
        ))

    # ── !level / !xp command ──────────────────────────────────────────────────
    @commands.command(name="lvl", aliases=["level", "xp"])
    async def cmd_level(self, ctx: commands.Context, member: discord.Member = None):
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

        def _hour_remaining(last_key: str) -> str:
            secs_left = HOUR_SECS - (now - rec.get(last_key, 0.0))
            if secs_left <= 0:
                return "ready"
            m, s = divmod(int(secs_left), 60)
            return f"{m}m {s}s"

        def _mins15_remaining() -> str:
            secs_left = MINS15_SECS - (now - rec.get("voice_last_15", 0.0))
            if secs_left <= 0:
                return "ready"
            m, s = divmod(int(secs_left), 60)
            return f"{m}m {s}s"

        msg_used,   msg_cap   = _day_used("msg_today",   "msg_day_ts",   MSG_DAILY_MAX)
        cmd_used,   cmd_cap   = _day_used("cmd_today",   "cmd_day_ts",   CMD_DAILY_MAX)
        voice_used, voice_cap = _day_used("voice_today", "voice_day_ts", VOICE_DAILY_MAX)

        msg_bar   = _bar(msg_used,   msg_cap,   10)
        cmd_bar   = _bar(cmd_used,   cmd_cap,   10)
        voice_bar = _bar(voice_used, voice_cap, 10)

        desc = (
            f"**Level {level}** — {xp:,} XP total\n"
            f"`{bar}` {xp_in_level:,} / {xp_needed:,} XP to level {level+1}\n\n"
            f"**Sources** *(daily used / cap)*\n"
            f"💬 Messages  `{msg_bar}` {msg_used}/{msg_cap}  · next: {_hour_remaining('msg_last_hour')}\n"
            f"⚡ Commands  `{cmd_bar}` {cmd_used}/{cmd_cap}  · next: {_hour_remaining('cmd_last_hour')}\n"
            f"🔊 Voice     `{voice_bar}` {voice_used}/{voice_cap}  · next: {_mins15_remaining()}\n"
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


async def setup(bot):
    await bot.add_cog(LevelingCog(bot))
