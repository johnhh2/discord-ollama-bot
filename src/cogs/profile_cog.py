"""!profile — one-embed overview of a player.

Aggregates the global economy (wallet, savings, artifacts, real estate),
per-guild bits (lottery tickets, level), the chess-only ranks (max /
cumulative bot Elo defeated), and the daily command streak. Read-only:
never materializes economy/leveling rows for the target beyond what
get_balance already does.
"""
import time

import discord
from discord.ext import commands

from src import state
from src.artifacts import owned_artifact_count
from src.economy import _ct_today, get_balance, get_savings_value
from src.games.bot_chess_rewards import (
    RANK_MAX_EMOJI,
    RANK_TOTAL_EMOJI,
    chess_ranks,
)
from src.helpers import emb, C_BLUE, C_GREY, OptionalMember
from src.leveling import display_level, level_from_xp
from src.persistence import load_lottery, load_records
from src.properties import owned_properties, portfolio_value
from src.streaks import effective_streak, get_command_streak_entry


class ProfileCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(
        name="profile",
        aliases=["user", "player", "investigate", "view", "rank", "elo"],
    )
    async def cmd_profile(self, ctx: commands.Context, target: OptionalMember = None):
        target = target or ctx.author
        if target.bot:
            await ctx.send(embed=emb(
                "🪪 Profile", "Bots don't have profiles — try `!balance` for the house pot.", C_GREY,
            ))
            return
        uid = target.id

        balance = await get_balance(uid)
        savings = int(await get_savings_value(uid))
        lines = [f"💰 Wallet: **{balance:,}** 🪙 · 🏦 Savings: **{savings:,}** 🪙"]

        if ctx.guild is not None:
            lottery = await load_lottery(ctx.guild.id)
            tickets = lottery.get("players", {}).get(str(uid), 0)
            lines.append(f"🎟️ Lottery tickets: **{tickets:,}**")

            records = await load_records(ctx.guild.id)
            held = sum(
                1 for rec in records.values()
                if str(rec.get("holder_id")) == str(uid)
            )
            lines.append(f"🏆 Records held: **{held:,}**")

            lvl_rec = state.leveling.get(str(ctx.guild.id), {}).get(str(uid))
            if lvl_rec is not None:
                global_xp = sum(
                    int(g.get(str(uid), {}).get("xp", 0) or 0)
                    for g in state.leveling.values()
                )
                level = display_level(lvl_rec.get("level", 0))
                global_level = display_level(level_from_xp(global_xp))
                line = f"📊 Level **{level}** — {lvl_rec.get('xp', 0):,} XP"
                if global_level != level:
                    line += f" · 🌐 Global level **{global_level}**"
                lines.append(line)

        streak = effective_streak(get_command_streak_entry(str(uid)), _ct_today())
        if streak:
            lines.append(f"🔥 Command streak: **{streak:,}** day{'' if streak == 1 else 's'}")

        artifacts = owned_artifact_count(uid)
        props = owned_properties(uid)
        holdings = f"🏺 Artifacts: **{artifacts:,}**"
        if props:
            holdings += f" · 🏘️ Properties: **{len(props):,}** (worth {portfolio_value(uid):,} 🪙)"
        lines.append(holdings)

        user_row = state.economy["users"].get(str(uid), {})
        if float(user_row.get("jail_until", 0) or 0) > time.time():
            lines.append(f"⛓️ In jail until <t:{int(user_row['jail_until'])}:R>")

        # ── Chess ranks (only once the user has beaten a bot) ─────────────
        max_elo, total_elo = chess_ranks(uid)
        if max_elo > 0:
            lines.append("\n**♟️ Chess Ranks**")
            lines.append(f"{RANK_MAX_EMOJI} Max Elo defeated: **{max_elo:,}**")
            lines.append(f"{RANK_TOTAL_EMOJI} Total Elo defeated: **{total_elo:,}**")

        embed = discord.Embed(
            title=f"🪪 {target.display_name}'s Profile",
            description="\n".join(lines),
            color=C_BLUE,
        )
        if getattr(target, "avatar", None):
            embed.set_thumbnail(url=target.display_avatar.url)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(ProfileCog(bot))
