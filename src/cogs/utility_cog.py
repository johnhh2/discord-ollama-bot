import asyncio
import json
import logging
import random
import re
import time

import aiohttp
import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_BLUE, C_GREY,
    send_ephemeral, toggle_member_role, get_memory_mb, format_uptime, _log_audit,
)
from src.economy import (
    get_guild_ask_model, get_guild_roleplay_model,
    get_guild_coding_model,
)
from src.permissions import (
    is_admin, check_puzzle_channel,
    requires_perm,
)
from src.persistence import (
    save_bot_settings, load_saved_quotes,
    insert_issue, get_issue_by_message, update_issue_status,
    list_issues, soft_delete_issue,
    insert_error_mute, delete_error_mute,
    insert_feature_request, get_feature_request_by_message,
    get_feature_request_by_feature_id,
    update_feature_request_status, link_feature_to_request,
)
from src.guild_config import get_guild_cfg
from src.ai import (
    check_ollama_connected, keep_typing,
    ollama_semaphore, OLLAMA_NUM_PREDICT, OLLAMA_REQUEST_TIMEOUT,
    TOKEN_BUCKET_MAX, TOKEN_BUCKET_REFILL_PER_SEC, _peek_token_budget,
)
from src.config import (
    OLLAMA_MODEL, OLLAMA_BASE_URL, PUZZLE_REWARDS,
)
from src.puzzle import (
    PUZZLE_RIDDLE_PROMPT, build_coding_prompt,
    extract_puzzle_fields, normalize_code,
)
from src import state


def _msg_text(msg: discord.Message) -> str:
    """Best-effort plain text from a Message, including embed title/description/fields."""
    parts: list[str] = []
    if msg.content:
        parts.append(msg.content)
    for e in msg.embeds:
        if e.title:
            parts.append(str(e.title))
        if e.description:
            parts.append(str(e.description))
        for f in e.fields:
            name = str(f.name) if f.name else ""
            value = str(f.value) if f.value else ""
            if name and value:
                parts.append(f"{name}: {value}")
            elif value:
                parts.append(value)
        if e.footer and e.footer.text:
            parts.append(str(e.footer.text))
    return " | ".join(p for p in parts if p)


# ─────────────────────────────────────────────────────────────────────────────
# Gambler role helpers
# ─────────────────────────────────────────────────────────────────────────────

async def get_or_create_gamblers_role(guild: discord.Guild) -> discord.Role | None:
    """Return the 'Gamblers' role, creating it if it doesn't exist."""
    role = discord.utils.get(guild.roles, name="Gamblers")
    if role is None:
        try:
            role = await guild.create_role(name="Gamblers", reason="Auto-created for gambler role tracking")
        except Exception:
            return None
    return role


PUZZLE_RIDDLE_REWARD = 20


class UtilityCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # epoch of last !puzzle generation per uid, for the per-user cooldown.
        self._last_puzzle_by_uid: dict[int, float] = {}
        # Per-admin listing cache for `!issue delete <N>`: {uid: [issue_id, ...]}
        # where the index in the list corresponds to the 1-indexed number the
        # user saw in their most recent `!issues` invocation. In-memory only —
        # if a delete comes after a restart we just tell them to re-list.
        self._issues_listing_by_user: dict[int, list[int]] = {}

    @commands.command(name="gambler-role", aliases=["gamblerole", "gamblers"])
    async def cmd_gambler_role(self, ctx: commands.Context, toggle: str = None):
        if not ctx.guild:
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if not cfg.get("gambler_role_enabled", False):
            await ctx.send(embed=emb("🎲 Gambler Role", "The gambler role feature is not enabled on this server.", C_GREY))
            return

        if toggle is None or toggle.lower() not in ("on", "off"):
            has_role = discord.utils.get(ctx.author.roles, name="Gamblers") is not None
            status = "✅ you have it" if has_role else "❌ you don't have it"
            await ctx.send(embed=emb("🎲 Gambler Role", f"Gamblers role: {status}\nUse `!gambler-role on` or `!gambler-role off` to opt in/out.", C_GOLD))
            return

        role = await get_or_create_gamblers_role(ctx.guild)
        if role is None:
            await ctx.send(embed=emb("❌ Error", "Could not find or create the Gamblers role.", C_RED))
            return

        adding = toggle.lower() == "on"
        already = role in ctx.author.roles
        if adding and already:
            await ctx.send(embed=emb("🎲 Gambler Role", f"**{ctx.author.display_name}** already has the Gamblers role.", C_GREY))
        elif not adding and not already:
            await ctx.send(embed=emb("🎲 Gambler Role", f"**{ctx.author.display_name}** doesn't have the Gamblers role.", C_GREY))
        else:
            reason = "User opted in via !gambler-role" if adding else "User opted out via !gambler-role"
            if await toggle_member_role(ctx.author, role, adding, reason=reason):
                msg = "✅ You've been added to the **Gamblers** role. You'll be pinged when a progressive jackpot is won!" if adding else "✅ You've been removed from the **Gamblers** role."
                await ctx.send(embed=emb("🎲 Gambler Role", msg, C_GREEN))
            else:
                await ctx.send(embed=emb("❌ Error", f"Failed to {'add' if adding else 'remove'} the role.", C_RED))

    @commands.command(name="help", aliases=["h"])
    async def cmd_help(self, ctx: commands.Context):
        from src.level_unlocks import fmt_line
        gid = ctx.guild.id if ctx.guild else 0
        uid = ctx.author.id

        help_embed = discord.Embed(title="📖 Commands", color=0x3498db)
        eco_lines = [
            "`!economy` — Economy overview and command list",
            fmt_line("savings", "`!savings` — 🐷 Piggy bank with 1% daily interest", uid, gid),
            "`!crime` — Steal, mug, and jailbreak commands",
        ]
        help_embed.add_field(name="💰 Economy", inline=False, value="\n".join(eco_lines))
        help_embed.add_field(name="🎮 Games / Gambling", inline=False, value=(
            "`!games` — View all games and gambling commands"
        ))
        lb_lines = [
            "`!leaderboard` — Top 10 richest users",
            "`!roles` — View role thresholds and your progress",
            "`!levels` — Top 10 users by XP level",
            "`!records` — All-time records for economy and games",
        ]
        help_embed.add_field(name="🏆 Leaderboards", inline=False, value="\n".join(lb_lines))
        help_embed.add_field(name="🤖 AI", inline=False, value=(
            "`!ai` — View AI connection status and command info"
        ))
        help_embed.add_field(name="🛒 Shop", inline=False, value=(
            "`!shop` — Browse items"
        ))

        # Only show NSFW if enabled in guild
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            nsfw_enabled = cfg.get("nsfw_enabled", False)
        else:
            nsfw_enabled = False

        if nsfw_enabled:
            help_embed.add_field(name="🔞 NSFW", inline=False, value=(
                fmt_line("nsfw", "`!nsfw [tags]` — Random NSFW image", uid, gid)
                + "\n`!ew` — Delete the last NSFW image you posted in this channel"
            ))

        help_embed.add_field(name="🎉 Fun", inline=False, value=(
            "`!dog` — Random dog picture\n"
            "`!cat` — Random cat picture\n"
            "`!quote` — Save a quoted message (reply) or display a random saved quote\n"
            "`!searchquote [#channel] [@user]` — Find spicy/volatile messages to quote\n"
            "`!tip` — Show a random tip about hidden commands"
        ))
        utility_val = (
            "`!stats` — Show bot statistics\n"
            "`!stop` — Stop roleplay / forfeit active game\n"
            "`!subscribe [voice-channel]` — DM you when a voice channel fills up\n"
            "`!bugreport <message>` — Send a bug report to the maintainer"
        )
        help_embed.add_field(name="🔧 Utility", inline=False, value=utility_val)
        await send_ephemeral(ctx, embed=help_embed)


    @commands.command(name="stats", aliases=["stat"])
    async def cmd_stats(self, ctx: commands.Context):
        elapsed = time.monotonic() - state.bot_start_time
        msg_rate = state.stats_messages_seen / (elapsed / 60) if elapsed > 0 else 0
        text_channels = sum(len(g.text_channels) for g in self.bot.guilds)
        voice_channels = sum(len(g.voice_channels) for g in self.bot.guilds)
        ai_connected = await check_ollama_connected()
        vram_text = state.bot_settings.get("vram_text", "16GB")

        embed = discord.Embed(title="📊 Bot Stats", color=C_BLUE)
        indent = "⠀ "  # Invisible character + space for indentation that Discord preserves
        embed.add_field(name="🤖 Bot", value=f"{indent}{self.bot.user}\n{self.bot.user.id}", inline=True)
        embed.add_field(name="⚙️ Shard", value=f"{indent}#0 / 1", inline=True)
        embed.add_field(name="💬 Commands Ran", value=f"{indent}{state.stats_commands_ran} Commands", inline=True)
        embed.add_field(name="📨 Messages", value=f"{indent}{state.stats_messages_seen} ({msg_rate:.2f}/min)", inline=True)
        embed.add_field(name="🧠 Memory", value=f"{indent}{get_memory_mb():.2f} MB", inline=True)
        embed.add_field(name="⏱️ Uptime", value=f"{indent}{format_uptime()}", inline=True)
        embed.add_field(name="🌐 Presence", value=(
            f"{indent}{len(self.bot.guilds)} Servers\n"
            f"{indent}{text_channels} Text Channels\n"
            f"{indent}{voice_channels} Voice Channels"
        ), inline=True)
        ai_enabled = state.bot_settings.get("ai_enabled", True)
        ai_status = "Online" if ai_connected else "Offline"
        ai_status_emoji = "🟢" if (ai_connected and ai_enabled) else "🔴"
        passive_status = "Enabled" if ai_enabled else "**Disabled**"
        ask_model = get_guild_ask_model(ctx.guild.id) if ctx.guild else OLLAMA_MODEL
        roleplay_model = get_guild_roleplay_model(ctx.guild.id) if ctx.guild else OLLAMA_MODEL
        coding_model = get_guild_coding_model(ctx.guild.id) if ctx.guild else OLLAMA_MODEL
        embed.add_field(name=f"{ai_status_emoji} AI Status", value=(
            f"{indent}Status: {ai_status} · Passive: {passive_status}\n"
            f"{indent}Ask model: `{ask_model}`\n"
            f"{indent}Roleplay model: `{roleplay_model}`\n"
            f"{indent}Coding model: `{coding_model}`\n"
            f"{indent}vRAM: {vram_text}"
        ), inline=True)
        await send_ephemeral(ctx, embed=embed)


    @commands.group(name="ai", invoke_without_command=True)
    async def cmd_ai(self, ctx: commands.Context):
        ai_connected = await check_ollama_connected()
        ask_model = get_guild_ask_model(ctx.guild.id) if ctx.guild else OLLAMA_MODEL
        roleplay_model = get_guild_roleplay_model(ctx.guild.id) if ctx.guild else OLLAMA_MODEL
        coding_model = get_guild_coding_model(ctx.guild.id) if ctx.guild else OLLAMA_MODEL

        ai_enabled = state.bot_settings.get("ai_enabled", True)
        ai_status = "Online" if ai_connected else "Offline"
        ai_status_emoji = "🟢" if (ai_connected and ai_enabled) else "🔴"
        passive_status = "Enabled" if ai_enabled else "**Disabled**"
        embed_color = C_BLUE if ai_enabled else C_RED

        embed = discord.Embed(title="🤖 AI Commands", color=embed_color)

        embed.add_field(
            name=f"{ai_status_emoji} Connection",
            value=f"Status: **{ai_status}** · Passive responses: {passive_status}",
            inline=False
        )

        tokens_left = _peek_token_budget(ctx.author.id)
        if tokens_left >= TOKEN_BUCKET_MAX:
            budget_value = (
                f"✅ **{int(tokens_left):,} / {TOKEN_BUCKET_MAX:,}** tokens — "
                f"refills 512 per minute"
            )
        else:
            seconds_to_full = (TOKEN_BUCKET_MAX - tokens_left) / TOKEN_BUCKET_REFILL_PER_SEC
            budget_value = (
                f"⏳ **{int(tokens_left):,} / {TOKEN_BUCKET_MAX:,}** tokens "
                f"(full in {seconds_to_full:.0f}s) — refills 512 per minute"
            )
        embed.add_field(
            name="🪙 Your Token Budget",
            value=budget_value,
            inline=False
        )

        embed.add_field(
            name="💬 !ask",
            value=(
                f"Ask the AI a question\n"
                f"Cost: **10 🪙**\n"
                f"Model: `{ask_model}`\n"
                f"Usage: `!ask <question>`"
            ),
            inline=False
        )

        story_aliases = get_guild_cfg(ctx.guild.id).get("story_aliases", {}) if ctx.guild else {}
        aliases_line = (
            "Aliases: " + ", ".join(f"`!{k}`" for k in story_aliases)
            if story_aliases
            else "Server admins can register custom-prompt aliases via `!settings story-alias`"
        )
        embed.add_field(
            name="📖 !story",
            value=(
                f"Generate an original short story on any topic\n"
                f"Cost: **500 🪙** · `!continue` for next chapter (10 🪙) · `!tldr` to summarize\n"
                f"Model: `{ask_model}`\n"
                f"Usage: `!story <prompt>`\n"
                f"{aliases_line}"
            ),
            inline=False
        )

        embed.add_field(
            name="🎭 !roleplay",
            value=(
                f"Start an AI roleplay session\n"
                f"Cost: **50 🪙** · `!tldr` to summarize the last response\n"
                f"Model: `{roleplay_model}`\n"
                f"Usage: `!roleplay <character> [@user1 @user2 ...]`"
            ),
            inline=False
        )

        embed.add_field(
            name="🗺️ !rpg",
            value=(
                f"Start an interactive text adventure game\n"
                f"Cost: **50 🪙**\n"
                f"Model: `{roleplay_model}`\n"
                f"Usage: `!rpg [@user1 @user2 ...]`\n"
            ),
            inline=False
        )

        embed.add_field(
            name="🧩 !puzzle coding / riddle",
            value=(
                f"AI-generated puzzles\n"
                f"`!puzzle coding [easy|medium|hard|extreme] [@user …]` — figure out the code output · **10–50 🪙**\n"
                f"`!puzzle riddle [@user …]` — curated one-word riddle · **{PUZZLE_RIDDLE_REWARD:,} 🪙**\n"
                f"`!puzzle riddleai [@user …]` — AI-generated riddle · **{PUZZLE_RIDDLE_REWARD:,} 🪙**\n"
                f"Model: `{coding_model}`\n"
                f"Only the creator can answer by default; mention users to invite them."
            ),
            inline=False
        )

        embed.add_field(
            name="⏹️ !closeall",
            value=(
                "Close every AI thread you own (ask/story/roleplay/rpg) in this server\n"
                "Runs from any AI channel"
            ),
            inline=False
        )

        if is_admin(ctx):
            embed.add_field(
                name="⚙️ !ai on / !ai off (bot admin)",
                value="Toggle passive AI responses globally.",
                inline=False,
            )

        await send_ephemeral(ctx, embed=embed)

    @cmd_ai.command(name="on", aliases=["online"])
    @requires_perm
    async def ai_on(self, ctx: commands.Context):
        state.bot_settings["ai_enabled"] = True
        await save_bot_settings()
        await ctx.send(embed=emb("🤖 AI Enabled", "Passive AI responses are now **online**.", C_GREEN))

    @cmd_ai.command(name="off", aliases=["offline"])
    @requires_perm
    async def ai_off(self, ctx: commands.Context):
        state.bot_settings["ai_enabled"] = False
        await save_bot_settings()
        await ctx.send(embed=emb("🤖 AI Disabled", "Passive AI responses are now **offline**.", C_RED))


    @commands.command(name="game", aliases=["games"])
    async def cmd_game(self, ctx: commands.Context):
        embed = discord.Embed(title="🎮 Games & Gambling", color=C_BLUE)

        embed.add_field(
            name="💰 Gambling",
            value=(
                "`!flip <amount>` — 50/50 coinflip\n"
                "`!slots <amount>` — 3-reel slot machine with progressive jackpot\n"
                "`!scratches` — Use all 3 daily scratchoffs at once\n"
                "`!scratchoff` — Single scratchoff (3 attempts/day)\n"
                "`!blackjack <amount>` — Interactive blackjack (type `hit` / `stand`)"
            ),
            inline=False
        )

        embed.add_field(
            name="🎯 Competitive",
            value=(
                "`!hangman [@user1 @user2]` — Start hangman\n"
                "`!race @user1 [@user2 ...] [amount]` — Race against others (optional bet)\n"
                "`!ttt @user [amount]` — Tic-Tac-Toe (use `!m <1-9>`)\n"
                "`!c4 @user [amount]` — Connect 4 (use `!m <1-7>`)\n"
                "`!chess @user [amount]` — Chess vs another player (use `!move <e2e4>` or just type the move)\n"
                "`!chess @TheBot [elo]` — Chess vs Stockfish (elo 100-3190, default 1320)\n"
                "`!chess view <id>` — Replay a finished game from its report id\n"
            ),
            inline=False
        )

        embed.add_field(
            name="🧩 Puzzles",
            value=(
                "`!puzzle coding [easy|medium|hard|extreme] [@user …]` — AI-generated coding puzzle\n"
                "Reward: **10–50 🪙** depending on difficulty.\n"
                f"`!puzzle riddle [@user …]` — curated one-word riddle · Reward: **{PUZZLE_RIDDLE_REWARD:,} 🪙**\n"
                f"`!puzzle riddleai [@user …]` — AI-generated riddle · Reward: **{PUZZLE_RIDDLE_REWARD:,} 🪙**\n"
                "Only the creator can answer by default; mention users to invite them."
            ),
            inline=False
        )

        utility_val = "`!stop` — Forfeit/stop active game"
        if ctx.guild and get_guild_cfg(ctx.guild.id).get("gambler_role_enabled", False):
            utility_val += "\n`!gambler-role on|off` — Opt in/out of the Gamblers role"
        embed.add_field(
            name="🏁 Utility",
            value=utility_val,
            inline=False
        )

        await send_ephemeral(ctx, embed=embed)



    @commands.command(name="puzzle")
    async def cmd_puzzle(self, ctx: commands.Context, *args):
        if await check_puzzle_channel(ctx):
            return

        # Parse args: subcommand, optional difficulty, optional @mentions (in any order after subcommand)
        subcommand = None
        difficulty = None
        invited_ids: set[int] = {ctx.author.id}

        pos_args = []
        for arg in args:
            # Mentions come through as <@123> or <@!123>
            if arg.startswith("<@") and arg.endswith(">"):
                uid_str = arg.strip("<@!>")
                if uid_str.isdigit():
                    invited_ids.add(int(uid_str))
            else:
                pos_args.append(arg)

        if pos_args:
            subcommand = pos_args[0]
        if len(pos_args) > 1:
            difficulty = pos_args[1]

        if subcommand is None:
            embed = discord.Embed(title="🧩 Puzzle Commands", color=C_BLUE)
            embed.add_field(
                name="!puzzle coding [difficulty] [@user …]",
                value=(
                    "AI generates a code snippet — figure out its output!\n"
                    "**Difficulties:** `easy` (10 🪙) · `medium` (20 🪙) · `hard` (35 🪙) · `extreme` (50 🪙)\n"
                    "Default difficulty: `medium`\n"
                    "Only you can answer by default. Mention users to invite them too.\n"
                    "Example: `!puzzle coding hard @Alice @Bob`"
                ),
                inline=False,
            )
            embed.add_field(
                name="!puzzle riddle [@user …]",
                value=(
                    "Classic riddle from our curated list — answer in one word!\n"
                    f"Reward: **{PUZZLE_RIDDLE_REWARD:,} 🪙**\n"
                    "Only you can answer by default. Mention users to invite them too.\n"
                    "Example: `!puzzle riddle @Alice`"
                ),
                inline=False,
            )
            embed.add_field(
                name="!puzzle riddleai [@user …]",
                value=(
                    "AI-generated riddle — answer in one word!\n"
                    f"Reward: **{PUZZLE_RIDDLE_REWARD:,} 🪙**\n"
                    "Only you can answer by default. Mention users to invite them too.\n"
                    "Example: `!puzzle riddleai @Alice`"
                ),
                inline=False,
            )
            await ctx.send(embed=embed)
            return

        if subcommand.lower() not in ("coding", "riddle", "riddleai"):
            await ctx.send(f"Unknown puzzle type `{subcommand}`. Try `!puzzle coding`, `!puzzle riddle`, or `!puzzle riddleai`.")
            return

        uid = ctx.author.id
        _PUZZLE_COOLDOWN = 6 * 3600
        if ctx.author.bot:
            _now = time.time()
            _last = self._last_puzzle_by_uid.get(uid, 0)
            if _now - _last < _PUZZLE_COOLDOWN:
                _remaining = int(_PUZZLE_COOLDOWN - (_now - _last))
                _h, _m = divmod(_remaining // 60, 60)
                await ctx.send(embed=emb("🧩 Cooldown", f"You can start another puzzle in **{_h}h {_m}m**.", C_GOLD))
                return
        if any(p["user_id"] == uid for p in state.active_puzzles.values()):
            await ctx.send(embed=emb("⚠️ Puzzle Active", f"**{ctx.author.display_name}** already has a puzzle running! Solve it or use `!stop` to cancel.", C_GOLD))
            return

        cid = ctx.channel.id
        if cid in state.active_puzzles:
            await ctx.send(embed=emb("⚠️ Puzzle Active", "A puzzle is already running in this channel! Solve it first.", C_GOLD))
            return

        if ctx.author.bot:
            self._last_puzzle_by_uid[uid] = time.time()

        # ── Riddle branch (static list) ────────────────────────────────────────────
        if subcommand.lower() == "riddle":
            if not state.RIDDLES_LIST:
                await ctx.send(embed=emb("❌ No Riddles", "Riddle list failed to load. Ask an admin to check `assets/riddles.csv` exists.", C_RED))
                return

            reward = PUZZLE_RIDDLE_REWARD
            entry = random.choice(state.RIDDLES_LIST)
            riddle_text = entry["riddle"]
            answer = entry["answer"]

            state.active_puzzles[cid] = {"answer": answer, "reward": reward, "user_id": uid, "invited_ids": invited_ids}

            guests = invited_ids - {uid}
            if guests:
                invite_line = "\nInvited: " + " ".join(f"<@{i}>" for i in guests)
                footer = f"Only {ctx.author.display_name} and invited users can answer · Use !stop to cancel"
            else:
                invite_line = ""
                footer = f"Only {ctx.author.display_name} can answer · Use !stop to cancel"
            embed = discord.Embed(
                title="🧩 Riddle",
                description=f"{riddle_text}\n\nType the **one-word answer** to win **{reward:,} 🪙**!{invite_line}",
                color=C_GOLD,
            )
            embed.set_footer(text=footer)
            await ctx.send(embed=embed)
            return

        # ── Riddle AI branch ───────────────────────────────────────────────────────
        if subcommand.lower() == "riddleai":
            reward = PUZZLE_RIDDLE_REWARD
            messages = [
                {"role": "system", "content": PUZZLE_RIDDLE_PROMPT},
                {"role": "user", "content": "Generate a riddle. Output the JSON object only."},
            ]
            guild_id = ctx.guild.id if ctx.guild else None
            coding_model = get_guild_coding_model(guild_id) if guild_id else OLLAMA_MODEL
            thinking_msg = await ctx.send(embed=emb("🧩 Generating riddle...", f"Reward: **{reward:,} 🪙**", C_BLUE))

            if not state.bot_settings.get("ai_enabled", True):
                await thinking_msg.edit(embed=emb("🤖 AI Offline", "Passive AI responses are currently disabled.", C_RED))
                return

            state.active_puzzles[cid] = {"generating": True, "user_id": uid, "reward": reward, "invited_ids": invited_ids}

            try:
                async with aiohttp.ClientSession() as session:
                    if ollama_semaphore.locked():
                        await thinking_msg.edit(embed=emb("⏳ Queued", "Another AI request is running. Your riddle will generate next...", C_BLUE))
                    async with ollama_semaphore:
                        if cid not in state.active_puzzles:
                            await thinking_msg.edit(embed=emb("🚫 Cancelled", "Puzzle generation was cancelled.", C_RED))
                            return
                        await thinking_msg.edit(embed=emb("🧩 Generating riddle...", f"Reward: **{reward:,} 🪙**", C_BLUE))
                        typing_task = asyncio.create_task(keep_typing(ctx.channel))
                        try:
                            payload = {
                                "model": coding_model,
                                "messages": messages,
                                "stream": False,
                                "options": {"num_predict": OLLAMA_NUM_PREDICT},
                            }
                            async with session.post(
                                f"{OLLAMA_BASE_URL}/api/chat",
                                json=payload,
                                timeout=OLLAMA_REQUEST_TIMEOUT,
                            ) as resp:
                                resp.raise_for_status()
                                data = await resp.json()
                                raw = data.get("message", {}).get("content", "")
                        finally:
                            typing_task.cancel()
            except aiohttp.ClientError as e:
                state.active_puzzles.pop(cid, None)
                _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"Ollama offline: {e}")
                await thinking_msg.edit(embed=emb("❌ AI Offline", "Could not connect to the AI.", C_RED))
                return
            except asyncio.TimeoutError:
                state.active_puzzles.pop(cid, None)
                _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], "Ollama riddle generation timed out (>120s)")
                await thinking_msg.edit(embed=emb("⏱️ Timed Out", "The AI took too long to generate a riddle. Try again.", C_RED))
                return

            if cid not in state.active_puzzles:
                await thinking_msg.edit(embed=emb("🚫 Cancelled", "Puzzle generation was cancelled.", C_RED))
                return

            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if not json_match:
                state.active_puzzles.pop(cid, None)
                _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"AI malformed response (no JSON): {raw[:200]}")
                await thinking_msg.edit(embed=emb("❌ Parse Error", "The AI returned an unexpected format. Try again.", C_RED))
                return
            try:
                puzzle_data = json.loads(json_match.group())
                riddle_text = str(puzzle_data["riddle"])
                answer = str(puzzle_data["answer"]).lower().strip()
            except (json.JSONDecodeError, KeyError) as e:
                state.active_puzzles.pop(cid, None)
                _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"AI malformed response ({type(e).__name__}): {raw[:200]}")
                await thinking_msg.edit(embed=emb("❌ Parse Error", "The AI returned an unexpected format. Try again.", C_RED))
                return

            if " " in answer or not answer.isalpha():
                state.active_puzzles.pop(cid, None)
                _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"AI generated non-single-word answer: {answer[:200]}")
                await thinking_msg.edit(embed=emb("❌ Bad Riddle", "The AI generated a multi-word answer. Try again.", C_RED))
                return

            state.active_puzzles[cid] = {
                "answer": answer,
                "reward": reward,
                "user_id": uid,
                "invited_ids": invited_ids,
            }

            guests = invited_ids - {uid}
            if guests:
                invite_line = "\nInvited: " + " ".join(f"<@{i}>" for i in guests)
                footer = f"Only {ctx.author.display_name} and invited users can answer · Use !stop to cancel"
            else:
                invite_line = ""
                footer = f"Only {ctx.author.display_name} can answer · Use !stop to cancel"
            embed = discord.Embed(
                title="🧩 Riddle",
                description=f"{riddle_text}\n\nType the **one-word answer** to win **{reward:,} 🪙**!{invite_line}",
                color=C_GOLD,
            )
            embed.set_footer(text=footer)
            try:
                await ctx.send(embed=embed)
                await thinking_msg.delete()
            except discord.HTTPException as e:
                state.active_puzzles.pop(cid, None)
                _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"Discord error sending riddle: {e}")
                await thinking_msg.edit(embed=emb("❌ Discord Error", "Failed to send the riddle. Please try again.", C_RED))
            return

        # ── Coding branch ──────────────────────────────────────────────────────────
        # Resolve difficulty
        difficulty = (difficulty or "medium").lower()
        if difficulty not in PUZZLE_REWARDS:
            await ctx.send(f"Unknown difficulty `{difficulty}`. Choose: {', '.join(PUZZLE_REWARDS)}")
            return

        reward = PUZZLE_REWARDS[difficulty]
        system_prompt = build_coding_prompt(difficulty)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": (
                f"Generate a {difficulty} coding output puzzle. "
                "Follow STEP 1, STEP 2, and STEP 3 from the instructions. "
                "Mentally trace execution before writing the answer field. "
                "Output the JSON object only."
            )},
        ]

        guild_id = ctx.guild.id if ctx.guild else None
        coding_model = get_guild_coding_model(guild_id) if guild_id else OLLAMA_MODEL
        thinking_msg = await ctx.send(embed=emb("🧩 Generating puzzle...", f"Difficulty: **{difficulty}** · Reward: **{reward:,} 🪙**", C_BLUE))

        if not state.bot_settings.get("ai_enabled", True):
            await thinking_msg.edit(embed=emb("🤖 AI Offline", "Passive AI responses are currently disabled.", C_RED))
            return

        # Register immediately so !stop can cancel during generation
        state.active_puzzles[cid] = {"generating": True, "user_id": uid, "reward": reward, "invited_ids": invited_ids}

        try:
            async with aiohttp.ClientSession() as session:
                if ollama_semaphore.locked():
                    await thinking_msg.edit(embed=emb("⏳ Queued", "Another AI request is running. Your puzzle will generate next...", C_BLUE))
                async with ollama_semaphore:
                    if cid not in state.active_puzzles:
                        # Cancelled while waiting in queue
                        await thinking_msg.edit(embed=emb("🚫 Cancelled", "Puzzle generation was cancelled.", C_RED))
                        return
                    await thinking_msg.edit(embed=emb("🧩 Generating puzzle...", f"Difficulty: **{difficulty}** · Reward: **{reward:,} 🪙**", C_BLUE))
                    typing_task = asyncio.create_task(keep_typing(ctx.channel))
                    try:
                        payload = {
                            "model": coding_model,
                            "messages": messages,
                            "stream": False,
                            "options": {"num_predict": OLLAMA_NUM_PREDICT},
                        }
                        async with session.post(
                            f"{OLLAMA_BASE_URL}/api/chat",
                            json=payload,
                            timeout=OLLAMA_REQUEST_TIMEOUT,
                        ) as resp:
                            resp.raise_for_status()
                            data = await resp.json()
                            raw = data.get("message", {}).get("content", "")
                    finally:
                        typing_task.cancel()
        except aiohttp.ClientError as e:
            state.active_puzzles.pop(cid, None)
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"Ollama offline: {e}")
            await thinking_msg.edit(embed=emb("❌ AI Offline", "Could not connect to the AI.", C_RED))
            return
        except asyncio.TimeoutError:
            state.active_puzzles.pop(cid, None)
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], "Ollama puzzle generation timed out (>120s)")
            await thinking_msg.edit(embed=emb("⏱️ Timed Out", "The AI took too long to generate a puzzle. Try again.", C_RED))
            return

        if cid not in state.active_puzzles:
            # Cancelled after generation completed but before we parsed
            await thinking_msg.edit(embed=emb("🚫 Cancelled", "Puzzle generation was cancelled.", C_RED))
            return

        puzzle_data = extract_puzzle_fields(raw)
        if puzzle_data is None:
            state.active_puzzles.pop(cid, None)
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"AI malformed response (no JSON): {raw[:200]}")
            await thinking_msg.edit(embed=emb("❌ Parse Error", "The AI returned an unexpected format. Try again.", C_RED))
            return
        try:
            code_snippet = normalize_code(puzzle_data["code"])
            answer = str(puzzle_data["answer"])
            language = puzzle_data.get("language", "Unknown")
        except KeyError as e:
            state.active_puzzles.pop(cid, None)
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"AI malformed response ({type(e).__name__}): {raw[:200]}")
            await thinking_msg.edit(embed=emb("❌ Parse Error", "The AI returned an unexpected format. Try again.", C_RED))
            return

        if "\n" in answer:
            state.active_puzzles.pop(cid, None)
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"AI generated multi-line answer: {answer[:200]}")
            await thinking_msg.edit(embed=emb("❌ Bad Puzzle", "The AI generated a multi-line answer. Try again.", C_RED))
            return

        state.active_puzzles[cid] = {
            "answer": answer,
            "code_snippet": code_snippet,
            "reward": reward,
            "user_id": uid,
            "invited_ids": invited_ids,
        }

        guests = invited_ids - {uid}
        if guests:
            invite_line = "\nInvited: " + " ".join(f"<@{i}>" for i in guests)
            footer = f"Only {ctx.author.display_name} and invited users can answer · Use !stop to cancel"
        else:
            invite_line = ""
            footer = f"Only {ctx.author.display_name} can answer · Use !stop to cancel"
        embed = discord.Embed(
            title=f"🧩 Coding Puzzle — {difficulty.capitalize()} · {language}",
            description=f"What will the output of this code be?\n```{language.lower()}\n{code_snippet}\n```\nType the **exact output** to win **{reward:,} 🪙**!{invite_line}",
            color=C_GOLD,
        )
        embed.set_footer(text=footer)
        try:
            await ctx.send(embed=embed)
            await thinking_msg.delete()
        except discord.HTTPException as e:
            state.active_puzzles.pop(cid, None)
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"Discord error sending puzzle: {e}")
            await thinking_msg.edit(embed=emb("❌ Discord Error", "Failed to send the puzzle. Please try again.", C_RED))


    @commands.command(name="adminhelp", aliases=["helpadmin"])
    @requires_perm
    async def cmd_adminhelp(self, ctx: commands.Context):
        admin_embed = discord.Embed(title="⚙️ Admin Commands", color=C_GOLD)
        admin_embed.add_field(name="🔧 Server Settings", inline=False, value=(
            "`!settings` — View current server settings"
        ))
        admin_embed.add_field(name="🔍 Moderation", inline=False, value=(
            "`!audit` — Last 5 failed command attempts\n"
            "`!clear <n>` — Delete last n messages (any author)\n"
            "`!saved` — Show saved data (admin-only)"
        ))
        if is_admin(ctx):
            admin_embed.add_field(name="🪙 Economy", inline=False, value=(
                "`!admingive @user <amount>` — Add or remove coins from a user\n"
                "`!event <amount> [hours]` — Start a reaction event\n"
                "`!adminragebait @user [n]` — Force ragebait on user (default 5 messages)"
            ))
            admin_embed.add_field(name="🤖 AI", inline=False, value=(
                "`!model [name]` — View or change the AI model\n"
                "`!roleplaymodel [name]` — View or change the roleplay model\n"
                "`!codingmodel [name]` — View or change the coding puzzle model"
            ))
            admin_embed.add_field(name="⚙️ Config", inline=False, value=(
                "`!setprompt <prompt>` — Set a custom system prompt for this channel\n"
                "`!clearprompt` — Reset this channel's prompt to default\n"
                "`!godmode [user]` — Toggle free costs on/off (for yourself or a user)\n"
                "`!setperm @user <server_admin|bot_admin|clear>` — Grant or clear a per-guild permission override for a user\n"
                "`!vramtext [text]` — View or set the vRAM display text in !stats"
            ))
            admin_embed.add_field(name="📢 Bot Control", inline=False, value=(
                "`!say <text>` — Make the bot repeat text in channel\n"
                "`!botinvitelink` — Display bot invite link\n"
                "`!invitelink` — Display server invite link\n"
                "`!restart` — Restart the bot process"
            ))
        await send_ephemeral(ctx, embed=admin_embed)


    @commands.command(name="saved", aliases=["persistent", "saves"])
    @requires_perm
    async def cmd_saved(self, ctx: commands.Context):
        """Show a snapshot of the bot's persisted in-memory state."""
        embed = discord.Embed(title="💾 Saved Data", color=C_GOLD)

        embed.add_field(
            name="🛡️ Insurance",
            value=f"**{len(state.insurance)}** users with active state.insurance",
            inline=False,
        )
        embed.add_field(
            name="🎭 Mock",
            value=f"**{len(state.active_mocks)}** users being mocked",
            inline=False,
        )
        embed.add_field(
            name="🎯 Ragebait",
            value=f"**{len(state.active_ragebaits)}** users with ragebait active",
            inline=False,
        )
        embed.add_field(
            name="🍆 Tax",
            value=f"**{len(state.active_taxes)}** users with active tax",
            inline=False,
        )
        embed.add_field(
            name="🔮 Curse",
            value=f"**{len(state.active_curses)}** users with curse active",
            inline=False,
        )
        embed.add_field(
            name="👑 Godmode",
            value=f"**{len(state.godmode_users)}** users with godmode",
            inline=False,
        )
        embed.add_field(
            name="💰 Slot Jackpot",
            value=f"**{state.slot_jackpot:,} 🪙** in jackpot",
            inline=False,
        )
        embed.add_field(
            name="♟️ Chess Games",
            value=f"**{len(state.active_chess_games)}** active correspondence chess games",
            inline=False,
        )

        _all_saved = await load_saved_quotes()
        _guild_id = str(ctx.guild.id) if ctx.guild else "dm"
        saved_quotes_count = len(_all_saved.get(_guild_id, []))
        embed.add_field(
            name="📜 Quotes",
            value=f"**{saved_quotes_count}** saved quotes (this server) | **{len(state.quote_log)}** in searchquote log (max 10)",
            inline=False,
        )

        total_users = len(state.economy.get("users", {}))
        total_balance = sum(u.get("balance", 0) for u in state.economy.get("users", {}).values())
        embed.add_field(
            name="🪙 Economy",
            value=f"**{total_users}** users with **{total_balance:,} 🪙** total balance",
            inline=False,
        )

        await send_ephemeral(ctx, embed=embed)

    @commands.command(name="bugreport", aliases=["bug"])
    @requires_perm
    async def cmd_bugreport(self, ctx: commands.Context, *, report: str = None):
        await self._submit_issue(ctx, kind="bug", report=report)

    @commands.command(name="featurerequest", aliases=["feature", "frequest"])
    @requires_perm
    async def cmd_featurerequest(self, ctx: commands.Context, *, description: str = None):
        """User-facing feature-request submission.

        Posts an embed to the per-guild `feature_request_channel` and seeds
        only two reactions: ✅ (accept → spawns a feature issue) and 🛑
        (reject). Distinct from `!issue feature` which is bot-admin only.
        """
        title = "📖 Feature Request"
        if description is None or not description.strip():
            await ctx.send(embed=emb(
                title,
                "Usage: `!featurerequest <description of the feature>`",
                C_GREY,
            ))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb(title, "Feature requests can only be filed in a server.", C_RED))
            return

        cfg = get_guild_cfg(ctx.guild.id)
        chan_id = cfg.get("feature_request_channel")
        if not chan_id:
            await ctx.send(embed=emb(
                title,
                "Feature requests are not configured for this server. Ask an admin to run `!settings feature-request-channel #channel`.",
                C_GREY,
            ))
            return

        try:
            channel = self.bot.get_channel(int(chan_id)) or await self.bot.fetch_channel(int(chan_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            channel = None
        if channel is None:
            await ctx.send(embed=emb(title, "Could not reach the feature-request channel.", C_RED))
            return

        guild_name = ctx.guild.name
        chan_ref = ctx.channel.mention if hasattr(ctx.channel, "mention") else str(ctx.channel)
        desc_lines = [
            f"**Time:** <t:{int(time.time())}:f>",
            f"**User:** {ctx.author.display_name} (`{ctx.author.id}`)",
            f"**Guild:** {guild_name} (`{ctx.guild.id}`)",
            f"**Channel:** {chan_ref}",
            "",
            f"**Description:**\n{description[:1500]}",
        ]

        try:
            request_msg = await channel.send(embed=emb(title, "\n".join(desc_lines), C_GOLD))
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send(embed=emb(title, "Could not post the feature request — please try again later.", C_RED))
            return

        try:
            await insert_feature_request(
                guild_id=ctx.guild.id,
                channel_id=request_msg.channel.id,
                message_id=request_msg.id,
                reporter_id=ctx.author.id,
                description=description[:1500],
            )
        except Exception as e:
            logging.error(f"[featurerequest] failed to persist row: {e}", exc_info=True)

        for emoji in _FEATURE_REQUEST_REACTIONS:
            try:
                await request_msg.add_reaction(emoji)
            except (discord.Forbidden, discord.HTTPException):
                pass

        # When the user filed from the feature-request channel itself, the
        # posted embed is its own confirmation — skip the redundant ack.
        if ctx.channel.id == request_msg.channel.id:
            return

        jumplink = (
            f"https://discord.com/channels/{ctx.guild.id}/"
            f"{request_msg.channel.id}/{request_msg.id}"
        )
        await ctx.send(embed=emb(
            title,
            f"Thanks — [your feature request]({jumplink}) has been submitted.",
            C_GREEN,
        ))

    @commands.command(name="issue")
    @requires_perm
    async def cmd_issue(self, ctx: commands.Context, kind: str = None, *, rest: str = None):
        """Bot-admin gateway for logging non-bug items + maintenance.

        Logging form: `!issue <bug|feature|task|improvement> <description>`.
        Delete form:  `!issue delete <N>` (or `remove`) — soft-deletes the
        Nth issue from the caller's most recent `!issues` listing.
        """
        kind_norm = (kind or "").lower().strip()
        if kind_norm in ("delete", "remove"):
            await self._delete_issue_by_number(ctx, rest)
            return
        if kind_norm not in _ISSUE_KINDS:
            allowed = "|".join(_ISSUE_KINDS)
            await ctx.send(embed=emb(
                "📒 Issue",
                f"Usage: `!issue <{allowed}> <description>` or `!issue delete <N>`",
                C_GREY,
            ))
            return
        await self._submit_issue(ctx, kind=kind_norm, report=rest)

    @commands.command(name="issues")
    @requires_perm
    async def cmd_issues(self, ctx: commands.Context, filt: str = None):
        """List issues for triage, newest first.

        Default filter is the non-terminal set (`open`, `not_started`, `wip`).
        Optional filter argument: `all`, or one of `open` / `not_started` /
        `wip` / `completed` / `rejected`. The displayed numbering is cached
        per caller so `!issue delete <N>` lines up with what you just saw.
        """
        filt_norm = (filt or "").lower().strip()
        if filt_norm == "":
            statuses: tuple[str, ...] | None = ("open", "not_started", "wip")
            header = "Open issues"
        elif filt_norm == "all":
            statuses = None
            header = "All issues"
        elif filt_norm in _ISSUE_VALID_STATUSES:
            statuses = (filt_norm,)
            header = f"Issues — {filt_norm}"
        else:
            allowed = "|".join(("all",) + tuple(_ISSUE_VALID_STATUSES))
            await ctx.send(embed=emb(
                "📒 Issues",
                f"Usage: `!issues [{allowed}]`",
                C_GREY,
            ))
            return

        rows = await list_issues(statuses=statuses, limit=_ISSUES_LIST_LIMIT)
        if not rows:
            await ctx.send(embed=emb("📒 Issues", f"_No {filt_norm or 'open'} issues._", C_GREY))
            self._issues_listing_by_user[ctx.author.id] = []
            return

        self._issues_listing_by_user[ctx.author.id] = [r["id"] for r in rows]

        lines: list[str] = []
        for n, row in enumerate(rows, start=1):
            lines.append(_format_issue_listing_line(n, row))
        body = "\n".join(lines)
        if len(rows) >= _ISSUES_LIST_LIMIT:
            body += f"\n\n_Showing first {_ISSUES_LIST_LIMIT}; older rows truncated._"
        body += "\n\nUse `!issue delete <N>` to remove an entry from this list."
        await ctx.send(embed=emb(f"📒 {header} ({len(rows)})", body, C_GREY))

    async def _delete_issue_by_number(self, ctx: commands.Context, arg: str | None) -> None:
        if not arg or not arg.strip().isdigit():
            await ctx.send(embed=emb(
                "📒 Issue",
                "Usage: `!issue delete <N>` — number is the position in your last `!issues` listing.",
                C_GREY,
            ))
            return
        n = int(arg.strip())
        listing = self._issues_listing_by_user.get(ctx.author.id)
        if not listing:
            await ctx.send(embed=emb(
                "📒 Issue",
                "Run `!issues` first — the number refers to the position in that listing.",
                C_GREY,
            ))
            return
        if n < 1 or n > len(listing):
            await ctx.send(embed=emb(
                "📒 Issue",
                f"There are only {len(listing)} issue(s) in your last listing.",
                C_RED,
            ))
            return
        issue_id = listing[n - 1]
        try:
            await soft_delete_issue(issue_id)
        except Exception as e:
            logging.error(f"[issue] soft-delete of id={issue_id} failed: {e}", exc_info=True)
            await ctx.send(embed=emb("📒 Issue", "Could not delete that issue — see logs.", C_RED))
            return
        # Drop from the cached listing so subsequent !issue delete N+1 still
        # lines up with what's left.
        del listing[n - 1]
        await ctx.send(embed=emb("📒 Issue", f"Deleted issue **#{n}** from the listing.", C_GREEN))

    async def _submit_issue(self, ctx: commands.Context, *, kind: str, report: str | None):
        meta = _ISSUE_KINDS[kind]
        title = f"{meta['emoji']} {meta['title']}"

        if report is None or not report.strip():
            usage = meta["usage"]
            await ctx.send(embed=emb(title, f"Usage: {usage}", C_GREY))
            return

        chan_id = state.bot_settings.get("internal_issue_channel")
        if not chan_id:
            await ctx.send(embed=emb(
                title,
                f"{meta['title']} submission is not configured on this bot.",
                C_GREY,
            ))
            return

        try:
            channel = self.bot.get_channel(int(chan_id)) or await self.bot.fetch_channel(int(chan_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            channel = None
        if channel is None:
            await ctx.send(embed=emb(title, f"Could not reach the {meta['title'].lower()} channel.", C_RED))
            return

        guild_name = ctx.guild.name if ctx.guild else "DM"
        guild_id_str = str(ctx.guild.id) if ctx.guild else "—"
        chan_ref = ctx.channel.mention if hasattr(ctx.channel, "mention") else str(ctx.channel)
        source_link = _source_command_jumplink(ctx)
        desc_lines = [
            f"**Time:** <t:{int(time.time())}:f>",
            f"**User:** {ctx.author.display_name} (`{ctx.author.id}`)",
            f"**Guild:** {guild_name} (`{guild_id_str}`)",
            f"**Channel:** {chan_ref}",
            f"**Source command:** {source_link}",
            "",
            f"**{meta['report_label']}:**\n{report[:1500]}",
        ]

        if meta["include_history"]:
            history_lines = []
            try:
                async for msg in ctx.channel.history(limit=6):
                    if msg.id == ctx.message.id:
                        continue
                    if len(history_lines) >= 5:
                        break
                    history_lines.append(f"[{msg.author.display_name}]: {_msg_text(msg)[:200]}")
                history_lines.reverse()
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                pass
            if history_lines:
                log_block = "\n".join(history_lines)
                if len(log_block) > 1500:
                    log_block = log_block[-1500:]
                desc_lines.append(f"\n**Last 5 messages:**\n```\n{log_block}\n```")

        # Bake the initial status footer into the description at creation so
        # admins see "Status: Not started" without waiting for a reaction.
        seeded_footer = _issue_status_footer("not_started")
        if seeded_footer:
            desc_lines.append(f"\n{seeded_footer}")

        try:
            report_msg = await channel.send(embed=emb(title, "\n".join(desc_lines), C_RED))
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send(embed=emb(title, f"Could not post the {meta['title'].lower()} — please try again later.", C_RED))
            return

        try:
            await insert_issue(
                guild_id=ctx.guild.id if ctx.guild else None,
                channel_id=report_msg.channel.id,
                message_id=report_msg.id,
                reporter_id=ctx.author.id,
                report=report[:1500],
                kind=kind,
                source_channel_id=ctx.channel.id if hasattr(ctx.channel, "id") else None,
                source_message_id=ctx.message.id if getattr(ctx, "message", None) else None,
            )
        except Exception as e:
            logging.error(f"[issue:{kind}] failed to persist issue row: {e}", exc_info=True)
            # Still seed the reactions — admins can mark it during the same
            # uptime via the message cache even without a DB row. (Persistence
            # only matters across restarts.)

        for emoji in _ISSUE_STATUS_EMOJIS:
            try:
                await report_msg.add_reaction(emoji)
            except (discord.Forbidden, discord.HTTPException):
                pass

        await ctx.send(embed=emb(title, meta["ack"], C_GREEN))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Bot-admin reaction-triage on issue and feature-request embeds.

        - In `internal_issue_channel`: ✅/🛑/⚙️/❌ flip the issue status (any
          kind); 🔇 mutes the error key for kind='error'.
        - In a guild's `feature_request_channel`: ✅ accepts the request
          (spawns a feature issue + links it) and ❌ rejects it.

        Uses raw events so reactions after a restart still resolve to the
        persisted row.
        """
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        if payload.user_id not in state.bot_admins:
            return

        emoji = str(payload.emoji)

        # Feature-request channel branch — short-circuit before falling through
        # to the issue triage logic. Lookup is keyed by guild so two servers
        # using the same channel id (unlikely but possible) don't collide.
        if emoji in _FEATURE_REQUEST_EMOJI_TO_DECISION and payload.guild_id is not None:
            cfg = get_guild_cfg(payload.guild_id)
            fr_chan_id = cfg.get("feature_request_channel")
            if fr_chan_id and str(payload.channel_id) == str(fr_chan_id):
                await self._handle_feature_request_reaction(payload, emoji)
                return

        if emoji not in _ISSUE_EMOJI_TO_STATUS and emoji != _ISSUE_MUTE_EMOJI:
            return

        chan_id = state.bot_settings.get("internal_issue_channel")
        if not chan_id or str(payload.channel_id) != str(chan_id):
            return

        issue = await get_issue_by_message(payload.message_id)
        if issue is None or issue.get("deleted"):
            return

        if emoji == _ISSUE_MUTE_EMOJI:
            await self._toggle_issue_mute(payload, issue, mute=True)
            return

        new_status = _ISSUE_EMOJI_TO_STATUS[emoji]
        if issue["status"] == new_status:
            return

        try:
            channel = self.bot.get_channel(int(chan_id)) or await self.bot.fetch_channel(int(chan_id))
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        muted = bool(issue.get("mute_key")) and issue["mute_key"] in state.error_mutes
        new_embed = _render_issue_status_embed(
            message.embeds[0] if message.embeds else None,
            new_status,
            muted=muted,
        )
        if new_embed is None:
            return
        try:
            await message.edit(embed=new_embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            logging.error(f"[bug] failed to edit issue embed {payload.message_id}: {e}")
            return

        try:
            await update_issue_status(payload.message_id, new_status, payload.user_id)
        except Exception as e:
            logging.error(f"[bug] failed to persist status {new_status} for {payload.message_id}: {e}", exc_info=True)

        # A completed issue is done with triage — clear its reaction bar so it
        # reads as closed and can't be re-triaged with a stale reaction.
        if new_status == "completed":
            try:
                await message.clear_reactions()
            except (discord.Forbidden, discord.HTTPException) as e:
                logging.error(f"[bug] failed to clear reactions on {payload.message_id}: {e}")

        # Propagate to the originating feature_request, if this issue is a
        # spawned feature linked to one.
        if issue.get("kind") == "feature":
            await self._refresh_feature_request_for_issue(issue["id"], feature_status=new_status)

        # Notify the original reporter on completion. Bug reports DM the
        # !bugreport author; spawned features DM the !featurerequest author
        # only if a feature_request row links back. Other kinds (admin-filed
        # feature/task/improvement/error) don't DM.
        if new_status == "completed":
            await self._dm_reporter_on_completion(issue)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """Bot admin unreacting 🔇 unmutes the error.

        Only 🔇 is meaningful on remove — pulling a status reaction is just
        moving between options and doesn't roll back the issue status (the
        next status reaction wins).
        """
        emoji = str(payload.emoji)
        if emoji != _ISSUE_MUTE_EMOJI:
            return
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        if payload.user_id not in state.bot_admins:
            return

        chan_id = state.bot_settings.get("internal_issue_channel")
        if not chan_id or str(payload.channel_id) != str(chan_id):
            return

        issue = await get_issue_by_message(payload.message_id)
        if issue is None or issue.get("deleted"):
            return
        await self._toggle_issue_mute(payload, issue, mute=False)

    async def _toggle_issue_mute(
        self,
        payload: discord.RawReactionActionEvent,
        issue: dict,
        *,
        mute: bool,
    ) -> None:
        """Add/remove the mute_key from `state.error_mutes` + DB, then re-render
        the embed footer to reflect the new mute state.

        No-op when the issue isn't an error report (no mute_key to toggle), or
        when the mute is already in the desired state.
        """
        if issue.get("kind") != "error":
            return
        mute_key = issue.get("mute_key")
        if not mute_key:
            return

        currently_muted = mute_key in state.error_mutes
        if mute and currently_muted:
            return
        if not mute and not currently_muted:
            return

        if mute:
            state.error_mutes.add(mute_key)
            try:
                await insert_error_mute(mute_key, payload.user_id)
            except Exception as e:
                state.error_mutes.discard(mute_key)
                logging.error(f"[mute] failed to persist mute {mute_key!r}: {e}", exc_info=True)
                return
        else:
            state.error_mutes.discard(mute_key)
            try:
                await delete_error_mute(mute_key)
            except Exception as e:
                state.error_mutes.add(mute_key)
                logging.error(f"[mute] failed to delete mute {mute_key!r}: {e}", exc_info=True)
                return

        try:
            channel = self.bot.get_channel(int(payload.channel_id)) or await self.bot.fetch_channel(int(payload.channel_id))
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        new_embed = _render_issue_status_embed(
            message.embeds[0] if message.embeds else None,
            issue["status"],
            muted=mute,
        )
        if new_embed is None:
            return
        try:
            await message.edit(embed=new_embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            logging.error(f"[mute] failed to edit embed for {payload.message_id}: {e}")

    async def _handle_feature_request_reaction(
        self,
        payload: discord.RawReactionActionEvent,
        emoji: str,
    ) -> None:
        """Apply an accept/reject decision to a feature_request row.

        On accept: persist status='accepted', spawn a kind='feature' issue row
        in `internal_issue_channel`, link the two, and re-render the request
        embed with the linked feature's status. On reject: persist
        status='rejected' and re-render.

        Same-status re-reacts are no-ops (no duplicate feature issue spawned).
        """
        request = await get_feature_request_by_message(payload.message_id)
        if request is None:
            return
        decision = _FEATURE_REQUEST_EMOJI_TO_DECISION[emoji]
        if request["status"] == decision:
            return

        # Fetch the request embed up front; if we can't, bail before mutating.
        try:
            req_channel = self.bot.get_channel(int(payload.channel_id)) or await self.bot.fetch_channel(int(payload.channel_id))
            req_message = await req_channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        feature_status: str | None = None
        feature_issue_id: int | None = None

        if decision == "accepted":
            # Spawn a kind='feature' issue in the internal_issue_channel.
            issue_chan_id = state.bot_settings.get("internal_issue_channel")
            if not issue_chan_id:
                logging.warning(
                    "[featurerequest] accepted but internal_issue_channel unset; "
                    "skipping spawn of linked feature issue."
                )
            else:
                try:
                    issue_chan = self.bot.get_channel(int(issue_chan_id)) or await self.bot.fetch_channel(int(issue_chan_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                    issue_chan = None
                if issue_chan is not None:
                    feature_issue_id = await self._spawn_feature_from_request(issue_chan, request)
                    feature_status = "not_started"

        try:
            await update_feature_request_status(payload.message_id, decision, payload.user_id)
        except Exception as e:
            logging.error(
                f"[featurerequest] failed to persist status={decision} for "
                f"message_id={payload.message_id}: {e}", exc_info=True,
            )

        if feature_issue_id is not None:
            try:
                await link_feature_to_request(payload.message_id, feature_issue_id)
            except Exception as e:
                logging.error(f"[featurerequest] failed to link feature {feature_issue_id}: {e}", exc_info=True)

        new_embed = _render_feature_request_embed(
            req_message.embeds[0] if req_message.embeds else None,
            decision,
            feature_status=feature_status,
        )
        if new_embed is None:
            return
        try:
            await req_message.edit(embed=new_embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            logging.error(f"[featurerequest] failed to edit embed for {payload.message_id}: {e}")

    async def _spawn_feature_from_request(self, issue_chan, request: dict) -> int | None:
        """Post a kind='feature' issue embed mirroring the feature_request,
        seed the standard issue reactions, and return the inserted issue id.

        Returns None on any failure so the caller can persist the request
        status without forcing a link to a non-existent issue.
        """
        title = "📖 Feature"
        desc_lines = [
            f"**Time:** <t:{int(time.time())}:f>",
            f"**Reporter:** <@{request['reporter_id']}> (`{request['reporter_id']}`)",
            f"**From request:** [Jump to request](https://discord.com/channels/{request['guild_id']}/{request['channel_id']}/{request['message_id']})",
            "",
            f"**Description:**\n{(request['description'] or '')[:1500]}",
        ]
        try:
            issue_msg = await issue_chan.send(embed=emb(title, "\n".join(desc_lines), C_RED))
        except (discord.Forbidden, discord.HTTPException) as e:
            logging.error(f"[featurerequest] could not post linked feature issue: {e}")
            return None

        try:
            issue_id = await insert_issue(
                guild_id=request.get("guild_id"),
                channel_id=issue_msg.channel.id,
                message_id=issue_msg.id,
                reporter_id=request["reporter_id"],
                report=(request["description"] or "")[:1500],
                kind="feature",
            )
        except Exception as e:
            logging.error(f"[featurerequest] failed to insert spawned feature issue row: {e}", exc_info=True)
            return None

        for em in _ISSUE_STATUS_EMOJIS:
            try:
                await issue_msg.add_reaction(em)
            except (discord.Forbidden, discord.HTTPException):
                pass
        return issue_id

    async def _refresh_feature_request_for_issue(
        self, feature_issue_id: int, *, feature_status: str,
    ) -> None:
        """Re-render the originating feature_request embed (if any) to reflect
        a status change on its spawned feature issue.

        No-op when no feature_request points at this issue id, or when we
        can't reach the request channel/message.
        """
        try:
            request = await get_feature_request_by_feature_id(feature_issue_id)
        except Exception as e:
            logging.error(f"[featurerequest] lookup by feature_issue_id={feature_issue_id} failed: {e}", exc_info=True)
            return
        if request is None:
            return
        try:
            req_chan = self.bot.get_channel(int(request["channel_id"])) or await self.bot.fetch_channel(int(request["channel_id"]))
            req_message = await req_chan.fetch_message(int(request["message_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            return
        new_embed = _render_feature_request_embed(
            req_message.embeds[0] if req_message.embeds else None,
            request["status"],
            feature_status=feature_status,
        )
        if new_embed is None:
            return
        try:
            await req_message.edit(embed=new_embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            logging.error(f"[featurerequest] failed to mirror status to request {request['message_id']}: {e}")

        # When the linked feature issue is completed, the request is done too —
        # clear its reaction bar to match the completed issue embed.
        if feature_status == "completed":
            try:
                await req_message.clear_reactions()
            except (discord.Forbidden, discord.HTTPException) as e:
                logging.error(f"[featurerequest] failed to clear reactions on request {request['message_id']}: {e}")

    async def _dm_reporter_on_completion(self, issue: dict) -> None:
        """DM the original reporter that their bug/feature has been resolved.

        For kind='bug', the jumplink targets the bug-report embed itself
        (that *is* the user's submission). For kind='feature', look up a
        linked feature_request and use the request embed as the original;
        if the feature was admin-filed (no linked request), don't DM.

        Best-effort: DM failures (Forbidden / closed DMs) are logged and
        swallowed so the status change still completes cleanly.
        """
        kind = issue.get("kind")
        reporter_id = issue.get("reporter_id")
        if not reporter_id:
            return

        # origin_guild/chan/msg point at a *user-visible* channel — for bugs
        # that's the channel where they ran !bugreport, for features it's the
        # !featurerequest embed in the per-guild request channel. We avoid
        # linking to internal_issue_channel itself because non-admins can't see
        # it.
        origin_guild: int | str | None = None
        origin_chan: int | None = None
        origin_msg: int | None = None
        if kind == "bug":
            origin_guild = issue.get("guild_id") or "@me"
            origin_chan = issue.get("source_channel_id")
            origin_msg = issue.get("source_message_id")
            label = "bug report"
        elif kind == "feature":
            try:
                request = await get_feature_request_by_feature_id(issue["id"])
            except Exception as e:
                logging.error(f"[notify-complete] feature_request lookup failed: {e}", exc_info=True)
                return
            if request is None:
                return  # admin-filed via !issue feature, no user to notify
            origin_guild = request.get("guild_id") or "@me"
            origin_chan = request.get("channel_id")
            origin_msg = request.get("message_id")
            reporter_id = request.get("reporter_id") or reporter_id
            label = "feature request"
        else:
            return

        try:
            user = self.bot.get_user(int(reporter_id)) or await self.bot.fetch_user(int(reporter_id))
        except (discord.NotFound, discord.HTTPException):
            return
        if user is None:
            return

        if origin_chan and origin_msg:
            jumplink = f"https://discord.com/channels/{origin_guild}/{origin_chan}/{origin_msg}"
            body = f"Your {label} has been marked **completed**.\n\n[Jump to your submission]({jumplink})"
        else:
            # Older rows (pre-source-columns) or any case where the source
            # coords weren't captured — DM without a link rather than
            # leading the user to the admin-only bug-report channel.
            body = f"Your {label} has been marked **completed**."
        try:
            await user.send(embed=emb("✅ Resolved", body, C_GREEN))
        except (discord.Forbidden, discord.HTTPException) as e:
            logging.info(f"[notify-complete] could not DM reporter {reporter_id}: {e}")


# Per-kind metadata for !bugreport / !issue. The emoji prefixes the embed
# title and the rest drives the usage/ack copy and whether to include the
# last-5 in-channel messages (only bug reports get the history block).
_ISSUE_KINDS: dict[str, dict] = {
    "bug": {
        "emoji": "⚠️",
        "title": "Bug Report",
        "report_label": "Description",
        "usage": "`!bugreport <description of the bug>`",
        "ack": "Thanks — your bug report has been sent to the bot admins.",
        "include_history": True,
    },
    "feature": {
        "emoji": "📖",
        "title": "Feature",
        "report_label": "Description",
        "usage": "`!issue feature <description of the feature>`",
        "ack": "Feature logged.",
        "include_history": False,
    },
    "task": {
        "emoji": "☑️",
        "title": "Task",
        "report_label": "Description",
        "usage": "`!issue task <description of the task>`",
        "ack": "Task logged.",
        "include_history": False,
    },
    "improvement": {
        "emoji": "⬆️",
        "title": "Improvement",
        "report_label": "Description",
        "usage": "`!issue improvement <description of the improvement>`",
        "ack": "Improvement suggestion logged.",
        "include_history": False,
    },
}

# Emojis the bot seeds onto each new issue embed. Order is the order
# they appear in Discord's reaction bar.
# Statuses accepted as a filter argument to `!issues`. 'open' is the seeded
# initial status; the rest are reachable via the reaction-triage flow.
_ISSUE_VALID_STATUSES: tuple[str, ...] = (
    "open", "not_started", "wip", "completed", "rejected",
)
# Max rows returned by !issues. Keep below Discord's 4096-char embed cap; at
# ~120 chars per line that's ~30 rows safely.
_ISSUES_LIST_LIMIT = 25

# Short single-line status indicator used in the !issues listing.
_ISSUE_STATUS_GLYPHS: dict[str, str] = {
    "open":        "🆕",
    "not_started": "❌",
    "wip":         "⚙️",
    "completed":   "✅",
    "rejected":    "🛑",
}


def _source_command_jumplink(ctx) -> str:
    """Render a jumplink to the command message that produced this issue.

    Returns a fallback string if the link can't be composed (e.g. DM
    invocation has no guild id). The link goes to Discord's web routing,
    which works in-app on every client.
    """
    msg = getattr(ctx, "message", None)
    if msg is None or not getattr(msg, "id", None):
        return "—"
    chan = getattr(ctx, "channel", None)
    chan_id = getattr(chan, "id", None)
    if chan_id is None:
        return "—"
    guild_id = ctx.guild.id if getattr(ctx, "guild", None) else "@me"
    return f"[Jump to message](https://discord.com/channels/{guild_id}/{chan_id}/{msg.id})"


def _format_issue_listing_line(n: int, row: dict) -> str:
    """One row in `!issues` output: `N. <status> <kind> — <snippet> <jumplink>`.

    Snippet is the first 80 chars of `report` with newlines collapsed; the
    jumplink lets a bot admin go straight to the embed in the bug-report
    channel without copy-pasting an id.
    """
    status = row.get("status") or "open"
    glyph = _ISSUE_STATUS_GLYPHS.get(status, "•")
    kind = (row.get("kind") or "bug").lower()
    snippet = (row.get("report") or "").replace("\n", " ").strip()
    if len(snippet) > 80:
        snippet = snippet[:77] + "…"
    guild_id = row.get("guild_id") or "@me"
    chan_id = row.get("channel_id")
    msg_id = row.get("message_id")
    link = f"https://discord.com/channels/{guild_id}/{chan_id}/{msg_id}"
    return f"`{n:>2}.` {glyph} **{kind}** — {snippet} [↗]({link})"


# Emojis the bot seeds onto each new issue embed. Order is the order
# they appear in Discord's reaction bar.
_ISSUE_STATUS_EMOJIS: tuple[str, ...] = ("❌", "⚙️", "✅", "🛑")  # ❌ ⚙️ ✅ 🛑
_ISSUE_EMOJI_TO_STATUS: dict[str, str] = {
    "❌": "not_started",
    "⚙️": "wip",
    "✅": "completed",
    "🛑": "rejected",
}
_ISSUE_STATUS_TO_COLOR: dict[str, int] = {
    "open":        C_RED,
    "not_started": C_RED,
    "completed":   C_GREEN,
    "wip":         C_GOLD,
    "rejected":    C_RED,
}
# Human-readable label per stored status. "open" is the seeded default; we
# don't render a status footer for it.
_ISSUE_STATUS_LABEL: dict[str, str] = {
    "not_started": "Not started",
    "completed":   "Completed",
    "wip":         "Work in progress",
    "rejected":    "Rejected",
}


def _issue_status_footer(status: str) -> str | None:
    """Render the status-line footer for non-'open' statuses, or None."""
    label = _ISSUE_STATUS_LABEL.get(status)
    return f"**Status:** {label}" if label else None


_ISSUE_MUTED_FOOTER = "**This error has been muted and will not be reported again**"

# Mute reaction for auto-filed error reports. The triage emojis (❌/🚧/✅) apply
# to every kind; 🔇 only does anything on kind='error' issues.
_ISSUE_MUTE_EMOJI = "\U0001F507"  # 🔇

# Match the current `Status: <Label>` footer OR the older per-status sentences
# at the tail of an embed description, so embeds posted before this refactor
# still get cleanly re-rendered when their status changes.
_ISSUE_STATUS_FOOTER_RE = re.compile(
    r"\n\n\*\*Status:\*\*\s*[^\n]+\s*$"
    r"|\n\n\*\*This issue (?:has not been started|has been marked completed|is a work in progress|was rejected)\*\*\s*$"
)
_ISSUE_MUTED_FOOTER_RE = re.compile(
    r"\n\n\*\*This error has been muted and will not be reported again\*\*\s*$"
)


def _strip_issue_footers(desc: str) -> str:
    """Strip any combination of trailing muted + status footers, in either
    order, so a fresh render starts from the canonical description.
    """
    prev = None
    while desc != prev:
        prev = desc
        desc = _ISSUE_STATUS_FOOTER_RE.sub("", desc)
        desc = _ISSUE_MUTED_FOOTER_RE.sub("", desc)
    return desc


def _render_issue_status_embed(
    original: discord.Embed | None,
    status: str,
    *,
    muted: bool = False,
) -> discord.Embed | None:
    """Rebuild the bug-report embed with the current status + mute footers.

    Canonical footer order is `<status>` (if any) then `<muted>` (if any),
    each on its own blank-line-separated bold line. Returning a fresh Embed
    avoids mutating the input.
    """
    if original is None:
        return None
    desc = _strip_issue_footers(original.description or "")
    status_footer = _issue_status_footer(status)
    if status_footer:
        desc = f"{desc}\n\n{status_footer}"
    if muted:
        desc = f"{desc}\n\n{_ISSUE_MUTED_FOOTER}"
    new_embed = emb(
        str(original.title) if original.title else "📒 Issue",
        desc,
        _ISSUE_STATUS_TO_COLOR.get(status, C_RED),
    )
    return new_embed


# ── Feature requests ─────────────────────────────────────────────────────────

# Seed reactions on a fresh !featurerequest embed. ✅ accepts and spawns a
# feature issue; 🛑 rejects. No 'not started' / 'wip' here — those statuses
# live on the spawned feature issue and are mirrored back via
# _render_feature_request_embed when the issue's status changes.
_FEATURE_REQUEST_REACTIONS: tuple[str, ...] = ("✅", "❌")
_FEATURE_REQUEST_EMOJI_TO_DECISION: dict[str, str] = {
    "✅": "accepted",
    "❌": "rejected",
}

_FEATURE_REQUEST_FOOTER_RE = re.compile(
    r"\n\n\*\*Status:\*\*\s*[^\n]+\s*$"
)


def _render_feature_request_embed(
    original: discord.Embed | None,
    decision: str,
    *,
    feature_status: str | None = None,
) -> discord.Embed | None:
    """Re-render the feature-request embed with the current status footer.

    `decision` is 'open' | 'accepted' | 'rejected'. While accepted the embed
    mirrors the linked feature issue's status:

      accepted + not_started → yellow, **Status:** Not started
      accepted + wip         → yellow, **Status:** Work in progress
      accepted + completed   → green,  **Status:** Completed
      accepted + rejected    → red,    **Status:** Rejected
      accepted + (None)      → yellow, **Status:** Accepted
                               (only seen if the spawn step failed)
      rejected               → red,    **Status:** Rejected
      open                   → gold,   no footer
    """
    if original is None:
        return None
    desc = (original.description or "")
    # Strip any prior status footer first so transitions don't stack.
    prev = None
    while desc != prev:
        prev = desc
        desc = _FEATURE_REQUEST_FOOTER_RE.sub("", desc)

    footer: str | None
    color: int
    if decision == "accepted":
        if feature_status == "completed":
            footer = "**Status:** Completed"
            color = C_GREEN
        elif feature_status == "rejected":
            footer = "**Status:** Rejected"
            color = C_RED
        elif feature_status in ("not_started", "wip"):
            label = _ISSUE_STATUS_LABEL.get(feature_status, feature_status)
            footer = f"**Status:** {label}"
            color = C_GOLD
        else:
            # No linked feature yet (spawn failed) — fall back to a generic ack.
            footer = "**Status:** Accepted"
            color = C_GOLD
    elif decision == "rejected":
        footer = "**Status:** Rejected"
        color = C_RED
    else:
        footer = None
        color = C_GOLD

    if footer:
        desc = f"{desc}\n\n{footer}"
    title = str(original.title) if original.title else "📖 Feature Request"
    return emb(title, desc, color)


async def setup(bot):
    await bot.add_cog(UtilityCog(bot))
