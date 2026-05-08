"""Discord transport for the leveling system. Domain logic is in src/leveling.py."""
import asyncio
import time

import discord
from discord.ext import commands, tasks

from src.helpers import emb, C_BLUE, C_GOLD, MemberConverter, fetch_member
from src.guild_config import get_guild_cfg
from src.economy import add_balance, next_daily_reset_ts
from src import state
from src.leveling import (
    grant_xp, _ensure_lvl_record, _day_reset, xp_for_level, xp_for_next_level, display_level, levelup_coin_reward, _bar,
    XP_MESSAGE, XP_COMMAND, XP_VOICE, XP_SCRATCH, XP_STREAM,
    MSG_DAILY_MAX, CMD_DAILY_MAX, VOICE_DAILY_MAX, STREAM_DAILY_MAX,
    HOUR_SECS, MINS30_SECS,
)


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
        desc = f"{member.mention} reached **Level {lvl}**! +**{reward:,} 🪙**"

        # List any commands unlocked at this exact level (server-enabled only)
        from src.level_unlocks import unlocks_at_level
        unlocked = unlocks_at_level(lvl, guild_id)
        if unlocked:
            desc += "\n\n**🔓 Unlocked**\n" + "\n".join(info["usage"] for _cmd, info in unlocked)

        await channel.send(embed=emb("🎉 Level Up!", desc, C_GOLD))

    # ── !level / !xp command ──────────────────────────────────────────────────
    @commands.command(name="lvl", aliases=["level", "xp"])
    async def cmd_level(self, ctx: commands.Context, member: MemberConverter = None):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Leveling is per-server and not available in DMs.", 0xe74c3c))
            return
        target = member or ctx.author
        uid = target.id
        rec = _ensure_lvl_record(ctx.guild.id, uid)

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

        # Next unlocks (only show ones enabled on this server)
        from src.level_unlocks import next_unlocks
        upcoming = next_unlocks(target.id, ctx.guild.id, count=3)
        if upcoming:
            desc += "\n**🎁 Next Unlocks**\n"
            for lvl_req, _cmd, info in upcoming:
                desc += f"`Lvl {lvl_req}` — {info['usage']}\n"

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
            member = await fetch_member(ctx.guild, int(uid_str))
            if member:
                return member.display_name
            try:
                user = await self.bot.fetch_user(int(uid_str))
                return user.display_name
            except Exception:
                return f"User {uid_str}"

        names = await asyncio.gather(*(resolve_name(uid_str) for uid_str, _ in sorted_users))
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (name, (_, data)) in enumerate(zip(names, sorted_users)):
            prefix = medals[i] if i < 3 else f"{i + 1}."
            lines.append(f"{prefix} **{name}** — **Level {display_level(data.get('level', 0))}** ({data.get('xp', 0):,} XP)")
        lines.append("\n*Also: `!lvl` your level · `!lb` currency · `!lbr` roles*")
        await ctx.send(embed=emb("📊 XP Leaderboard", "\n".join(lines), C_BLUE))


async def setup(bot):
    await bot.add_cog(LevelingCog(bot))
