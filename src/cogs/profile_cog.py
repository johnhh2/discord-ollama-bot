"""!profile — one-embed overview of a player.

Aggregates the global economy (wallet, savings, artifacts, real estate),
per-guild bits (lottery tickets, level), the chess-only ranks (max /
cumulative bot Elo defeated + first-defeat bonus progress), and the daily
command streak. Read-only: never materializes economy/leveling rows for the
target beyond what get_balance already does.
"""
import time

import discord
from discord.ext import commands

from src import state
from src.artifacts import owned_artifact_count
from src.economy import _ct_today, get_balance, get_savings_value
from src.games import chess_bot
from src.games.bot_chess_rewards import (
    FIRST_DEFEAT_BONUS,
    FIRST_DEFEAT_BONUS_MIN_ELO,
    RANK_MAX_EMOJI,
    RANK_TOTAL_EMOJI,
    chess_ranks,
    claimed_bonus_bins,
)
from src.helpers import emb, C_BLUE, C_GREY, OptionalMember
from src.leveling import display_level
from src.persistence import load_lottery
from src.properties import owned_properties, portfolio_value
from src.streaks import effective_streak, get_command_streak_entry


# Every 100-Elo bin from the bonus floor up to the top selectable bin carries
# a one-time first-defeat bonus; used for the "claimed x/y" progress line.
_TOP_BONUS_BIN = chess_bot.round_elo_to_bin(chess_bot.ELO_MAX)
_BONUS_BIN_COUNT = (_TOP_BONUS_BIN - FIRST_DEFEAT_BONUS_MIN_ELO) // 100 + 1


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

            lvl_rec = state.leveling.get(str(ctx.guild.id), {}).get(str(uid))
            if lvl_rec is not None:
                lines.append(
                    f"📊 Level **{display_level(lvl_rec.get('level', 0))}** "
                    f"— {lvl_rec.get('xp', 0):,} XP (`!lvl`)"
                )

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

        # ── Chess ranks ───────────────────────────────────────────────────
        max_elo, total_elo = chess_ranks(uid)
        lines.append("\n**♟️ Chess Ranks**")
        if max_elo > 0:
            claimed = len(claimed_bonus_bins(uid))
            lines.append(f"{RANK_MAX_EMOJI} Max Elo defeated: **{max_elo:,}**")
            lines.append(f"{RANK_TOTAL_EMOJI} Total Elo defeated: **{total_elo:,}**")
            lines.append(
                f"🎁 First-defeat bonuses: **{claimed}/{_BONUS_BIN_COUNT}** claimed "
                f"(+{FIRST_DEFEAT_BONUS:,} 🪙 each, Elo {FIRST_DEFEAT_BONUS_MIN_ELO}+)"
            )
        else:
            lines.append(
                f"No bot defeats yet — `!chessbot [elo]` to earn ranks "
                f"(+{FIRST_DEFEAT_BONUS:,} 🪙 first-win bonus per Elo "
                f"{FIRST_DEFEAT_BONUS_MIN_ELO}+ rank)."
            )

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
