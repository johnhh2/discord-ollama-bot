import asyncio
import json
import os
import random
import time
import datetime
import logging
import re
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import ui

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_ORANGE, C_BLUE, C_PURPLE, C_GREY,
    mocking_font, curse_font, parse_amount, send_ephemeral, resolve_role,
    fetch_member, toggle_member_role, shop_charge, _render_race,
    _delete_after, _edit_board, get_memory_mb, format_uptime, get_version,
    get_system_prompt, _log_audit, log_bot_permission_error,
)
from src.economy import (
    add_balance, deduct_balance, get_balance, get_guild_house_balance,
    add_guild_house, drain_bot_balance_into_lottery, announce_new_lottery,
    is_insured, get_guild_ask_model, get_guild_roleplay_model,
    get_guild_coding_model, _ct_now, _ct_today, do_daily_reset, _ensure_user,
)
from src.permissions import (
    is_admin, is_server_admin, can_manage_settings, check_rate_limit,
    check_channel, check_game_channel, check_ai_channel, check_puzzle_channel,
    check_chess_channel, _wrong_channel_reply, check_command_permission,
)
from src.persistence import (
    _load_json, _save_json, save_economy, save_insurance, save_guild_settings,
    save_bot_settings, save_bot_admins, save_godmode_users, save_bot_roles,
    save_chess_games, save_ragebait, save_mock, save_rigged_slots,
    save_gambler_streak, save_roleplay_state, save_fanfic_histories,
    save_quote_log, save_saved_quotes, save_tax, save_curse, save_lottery,
    load_lottery, load_saved_quotes, get_guild_cfg,
)
from src.ai import (
    enforce_cost, insufficient_funds, check_ollama_connected, keep_typing,
    stream_ollama, finalize, _execute_ollama_stream, respond, respond_roleplay,
    ASK_SYSTEM_PROMPT, FANFIC_SYSTEM_PROMPT, FEATURE_COSTS, _norm_puzzle_answer,
    ollama_semaphore,
)
from src.config import (
    OLLAMA_MODEL, OLLAMA_BASE_URL, SYSTEM_PROMPT, HISTORY_LIMIT,
    RATE_LIMIT_SECONDS, RULE34_API_KEY, RULE34_USER_ID, RACE_TRACK_LEN,
    ACTIVE_CHANNEL_IDS, DISCORD_TOKEN, RESTART_MSG_FILE, EPHEMERAL_MSG_FILE,
    SLOT_REEL, SLOT_JACKPOT_SEED, SLOT_JACKPOT_CONTRIB, SLOT_HOUSE_CHANCE,
    SLOT_MIN_BET, SLOT_MULT_JACKPOT, SLOT_MULT_3BAR, SLOT_MULT_3BELL,
    SLOT_MULT_3LEMON, SLOT_MULT_3CHERRY, SLOT_MULT_2CHERRY, SLOT_MULT_1CHERRY,
    SLOT_JACKPOT_BONUS_MIN_BET, SLOT_JACKPOT_BONUS_MAX_BET, SLOT_JACKPOT_BONUS_MAX_MULT,
    HANGMAN_MAX_WRONG, HANGMAN_BASE_REWARD, HANGMAN_LENGTH_OFFSET,
    HANGMAN_LENGTH_MULT, HANGMAN_UNIQUE_MULT, HANGMAN_RARE_MULT, HANGMAN_ULTRA_RARE_MULT,
    BLACKJACK_NATURAL_MULT, SCRATCH_SYMBOLS, SCRATCHOFF_MAX_DAILY, SCRATCHOFF_PAYOUTS,
    SHOP_NICKNAME_SELF_COST, SHOP_NICKNAME_REMOVE_COST, SHOP_NICKNAME_OTHER_COST,
    SHOP_ROLE_CREATE_COST, SHOP_ROLE_REMOVE_COST, SHOP_ROLE_MOVE_COST,
    SHOP_ROLECOLOR_COST, SHOP_CHANNEL_COST, SHOP_INSURANCE_COST, SHOP_TAX_COST,
    SHOP_MOCK_COST, SHOP_RAGEBAIT_COST, SHOP_MUTE_COST, SHOP_CURSE_COST,
    SHOP_INSURANCE_DURATION_SECS, SHOP_MOCK_MESSAGES, SHOP_RAGEBAIT_MESSAGES,
    SHOP_CURSE_MESSAGES, SHOP_MUTE_MINUTES, SHOP_TAX_PER_MESSAGE,
    SHOP_TAX_DURATION_SECS, SOUNDBOARD_WINDOW_SECS, SOUNDBOARD_MAX_SOUNDS,
    DAILY_REWARD, DAILY_RESET_HOUR, INITIAL_BOT_ADMIN_ID,
)
from src import state


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


async def maybe_assign_gambler_role(guild: discord.Guild, member: discord.Member, channel: discord.abc.Messageable):
    """Assign the Gamblers role if the user used all 3 scratchoffs 2 days in a row."""
    cfg = get_guild_cfg(guild.id)
    if not cfg.get("gambler_role_enabled", False):
        return

    uid_key = str(member.id)
    today_ct = _ct_today()
    yesterday = (datetime.date.fromisoformat(today_ct) - datetime.timedelta(days=1)).isoformat()

    last_full_day = state.gambler_streak.get(uid_key)
    if last_full_day == yesterday:
        role = await get_or_create_gamblers_role(guild)
        if role and role not in member.roles:
            if await toggle_member_role(member, role, True, reason="Used all 3 scratchoffs 2 days in a row"):
                await channel.send(
                    f"🎲 {member.mention} You've been automatically added to the **Gamblers** role for using all 3 scratchoffs 2 days in a row! "
                    f"You'll be pinged whenever a progressive jackpot is won. "
                    f"Use `!gambler-role off` to opt out."
                )


PUZZLE_REWARDS = {
    "easy":   10,
    "medium": 20,
    "hard":   35,
    "extreme": 50,
}



PUZZLE_RIDDLE_REWARD = 20

PUZZLE_RIDDLE_PROMPT = (
    "You are a riddle generator. Your ONLY output must be a single raw JSON object — no markdown, no code fences, no prose before or after.\n"
    "\n"
    "Generate a creative riddle that satisfies ALL of these rules:\n"
    "\n"
    "WHAT MAKES A GOOD RIDDLE:\n"
    "  • The riddle should describe the answer indirectly — through what it does, how it behaves, or how it feels — NOT by literally listing its physical properties\n"
    "  • The answer should feel surprising and satisfying in hindsight: 'oh, of course!' — not 'well obviously, it said exactly what it is'\n"
    "  • Use unexpected angles, personification, or contradiction to obscure the answer\n"
    "\n"
    "HARD RULES:\n"
    "  • Do NOT generate math problems, arithmetic, number puzzles, trivia questions, or factual quiz questions\n"
    "  • Do NOT use these banned overused answers: echo, mirror, shadow, silence, time, fire, wind, darkness, light, water, breath, death, balloon\n"
    "  • Do NOT write a riddle that reads like a checklist of the answer's traits (shape + property + action = answer). That is not a riddle, it is a description.\n"
    "  • Every statement in the riddle must be UNIVERSALLY TRUE of the answer — no exceptions. Do not invent false constraints (e.g. 'I have no lock' for a door) to make the answer harder to guess. If a clue is only sometimes true, or sometimes false, remove it.\n"
    "  • A good riddle makes the solver think of something UNRELATED before the answer clicks. If someone could guess the answer from the second sentence alone, rewrite it.\n"
    "  • The answer must be a single common English word (no phrases, no numbers, no abbreviations)\n"
    "  • The answer must be unambiguous — there should be only one reasonable word that fits\n"
    "\n"
    "Output EXACTLY this JSON and nothing else:\n"
    "  {\"riddle\": \"<the riddle text>\", \"answer\": \"<single lowercase word>\"}\n"
    "\n"
    "Output ONLY the JSON object. Any text outside the JSON will break the parser."
)

PUZZLE_CODING_PROMPT = (
    "You are a coding puzzle generator. Your ONLY output must be a single raw JSON object — no markdown, no code fences, no prose before or after.\n"
    "\n"
    "STEP 1 — Write a self-contained code snippet that satisfies ALL of these:\n"
    "  • Uses only the standard library (no third-party imports)\n"
    "  • No input(), no random, no time-dependent values — output must be fully deterministic\n"
    "  • No unhandled exceptions of ANY kind — no AttributeError, TypeError, ZeroDivisionError, NameError, IndexError, KeyError, RecursionError, or any other exception that would terminate the program without being caught\n"
    "  • No infinite loops or unbounded recursion\n"
    "  • Must produce exactly ONE line of stdout output — the entire output must fit on a single line with no newline characters\n"
    "  • The puzzle's difficulty should come from surprising but VALID behavior — not from errors\n"
    "\n"
    "STEP 2 — Simulate a Python/JS/C interpreter in your head. Execute every line in order:\n"
    "  a) Track the value of every variable after each assignment\n"
    "  b) For every function call, trace what it returns\n"
    "  c) For every exception that could be raised — even inside try/except blocks — verify it is caught and handled\n"
    "  d) List only the lines that call print() (or printf/console.log). Write down exactly what each prints.\n"
    "  e) Ask: 'Is there any line that could raise an UNCAUGHT exception?' If yes → go back to STEP 1 and rewrite.\n"
    "  f) Ask: 'Is the stdout list from step (d) non-empty?' If empty → go back to STEP 1 and rewrite.\n"
    "  g) Ask: 'Does the stdout list from step (d) contain more than one line of output?' If yes → go back to STEP 1 and rewrite the snippet so it only prints once.\n"
    "\n"
    "STEP 3 — Output EXACTLY this JSON and nothing else:\n"
    "  {\"language\": \"<Python|JavaScript|C>\", "
    "\"code\": \"<snippet as a plain string — no backticks, no markdown, no code fences>\", "
    "\"answer\": \"<exact stdout>\"}\n"
    "\n"
    "CRITICAL: The 'code' field must be a valid JSON string value.\n"
    "  • Do NOT wrap it in backticks or markdown fences (no ```python, no ``` at all)\n"
    "  • Embed newlines as \\n and tabs as \\t\n"
    "  • Any double-quote character inside the code MUST be escaped as \\\". Example: s = \\\"hello\\\" not s = \"hello\"\n"
    "  • Prefer single quotes for string literals in the code where the language allows it (Python, JS) to avoid escaping\n"
    "\n"
    "Rules for the answer field:\n"
    "  • Copy character-for-character from your stdout list in STEP 2d\n"
    "  • Multiple printed lines are joined with a literal \\n in the JSON string\n"
    "  • No trailing newline (Python's print() newline is not part of the output string)\n"
    "  • For C: use standard Linux printf/puts behavior\n"
    "\n"
    "Output ONLY the JSON object. Any text outside the JSON will break the parser."
)

PUZZLE_DIFFICULTY_GUIDANCE = {
    "easy":   "Use Python or JavaScript only. Use a trivial snippet (e.g. basic arithmetic, string concat, simple loop). The output should be obvious to a beginner.",
    "medium": "Use Python or JavaScript only. Use a moderately tricky snippet involving type coercion, simple recursion, or list operations.",
    "hard":   "Use Python, JavaScript, or C. Use a tricky snippet involving closures, scoping, reference semantics, or unexpected operator behavior.",
    "extreme": "Use Python, JavaScript, or C. This must be brutally hard. The difficulty MUST come from actual algorithmic complexity or non-trivial computation — NOT from simple floating point quirks, basic type theory, or single-line edge cases. Required: the snippet must involve at least one of (a) a non-trivial algorithm (e.g. recursive descent, dynamic programming, bitwise computation, manual numeric base conversion, custom sort/reduce), (b) complex multi-step string manipulation or construction (e.g. encoding, interleaving, repeated transformations), or (c) a computation that requires tracing several steps of state mutation through data structures. The solver must actually work through the logic — not just recall a language quirk. The code must run to completion and print exactly one line.",
}

class UtilityCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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
        help_embed = discord.Embed(title="📖 Commands", color=0x3498db)
        help_embed.add_field(name="💰 Economy", inline=False, value=(
            "`!daily` — Claim 200 🪙 (24h cooldown)\n"
            "`!balance [@user]` — Check balance\n"
            "`!pay @user <amount>` — Send coins to another user\n"
            "`!savings` — 🐷 Piggy bank with 1% daily interest\n"
            "`!crime` — Steal, mug, and jailbreak commands"
        ))
        help_embed.add_field(name="🎮 Games / Gambling", inline=False, value=(
            "`!games` — View all games and gambling commands"
        ))
        help_embed.add_field(name="🏆 Leaderboards", inline=False, value=(
            "`!leaderboard` — Top 10 richest users\n"
            "`!roles` — View role thresholds and your progress\n"
            "`!levels` — Top 10 users by XP level\n"
            "`!records` — All-time records for economy and games"
        ))
        help_embed.add_field(name="🤖 AI", inline=False, value=(
            "`!ai` — View AI connection status and command info"
        ))
        help_embed.add_field(name="🛒 Shop", inline=False, value=(
            "`!shop` — Browse items"
        ))

        # Only show rule34 if enabled in guild
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            r34_enabled = cfg.get("rule34_enabled", True)
        else:
            r34_enabled = True

        if r34_enabled:
            help_embed.add_field(name="🔞 NSFW", inline=False, value=(
                "`!rule34 [tags]` — Random image from rule34 (alias: `!r34`)"
            ))

        help_embed.add_field(name="🎉 Fun", inline=False, value=(
            "`!dog` — Random dog picture\n"
            "`!cat` — Random cat picture\n"
            "`!quote` — Save a quoted message (reply) or display a random saved quote\n"
            "`!searchquote [#channel] [@user]` — Find spicy/volatile messages to quote"
        ))
        utility_val = (
            "`!stats` — Show bot statistics\n"
            "`!stop` — Stop roleplay / forfeit active game"
        )
        if ctx.guild and get_guild_cfg(ctx.guild.id).get("gambler_role_enabled", False):
            utility_val += "\n`!gambler-role on|off` — Opt in/out of the Gamblers role"
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

        embed.add_field(
            name="📖 !fanfic",
            value=(
                f"Generate a steamy fan fiction story on any topic\n"
                f"Cost: **20 🪙** · `!continue` for next chapter (10 🪙) · `!tldr` to summarize\n"
                f"Model: `{ask_model}`\n"
                f"Usage: `!fanfic <prompt>`"
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

        await send_ephemeral(ctx, embed=embed)

    @cmd_ai.command(name="on", aliases=["online"])
    async def ai_on(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return
        state.bot_settings["ai_enabled"] = True
        save_bot_settings()
        await ctx.send(embed=emb("🤖 AI Enabled", "Passive AI responses are now **online**.", C_GREEN))

    @cmd_ai.command(name="off", aliases=["offline"])
    async def ai_off(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return
        state.bot_settings["ai_enabled"] = False
        save_bot_settings()
        await ctx.send(embed=emb("🤖 AI Disabled", "Passive AI responses are now **offline**.", C_RED))


    @commands.command(name="game", aliases=["games"])
    async def cmd_game(self, ctx: commands.Context):
        embed = discord.Embed(title="🎮 Games & Gambling", color=C_BLUE)

        embed.add_field(
            name="💰 Gambling",
            value=(
                "`!flip <amount>` — 50/50 coinflip\n"
                "`!slots <amount>` — 3-reel slot machine with progressive jackpot\n"
                "`!scratchoff` — Daily lottery (3 attempts/day)\n"
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
                "`!chess @user [amount]` — Correspondence chess (use `!move <e2e4>`)\n"
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

        embed.add_field(
            name="🏁 Utility",
            value=(
                "`!stop` — Forfeit/stop active game"
            ),
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
            _last = state.user_last_puzzle.get(uid, 0)
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
            state.user_last_puzzle[uid] = time.time()
        import re as _re

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
                            payload = {"model": coding_model, "messages": messages, "stream": False}
                            async with session.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload) as resp:
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

            if cid not in state.active_puzzles:
                await thinking_msg.edit(embed=emb("🚫 Cancelled", "Puzzle generation was cancelled.", C_RED))
                return

            json_match = _re.search(r'\{.*\}', raw, _re.DOTALL)
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
        guidance = PUZZLE_DIFFICULTY_GUIDANCE[difficulty]
        system_prompt = PUZZLE_CODING_PROMPT + f"\n\nDIFFICULTY: {difficulty}. {guidance}"
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
                        payload = {"model": coding_model, "messages": messages, "stream": False}
                        async with session.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload) as resp:
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

        if cid not in state.active_puzzles:
            # Cancelled after generation completed but before we parsed
            await thinking_msg.edit(embed=emb("🚫 Cancelled", "Puzzle generation was cancelled.", C_RED))
            return

        # Parse JSON from the response
        def _extract_puzzle_fields(text: str):
            """Try json.loads first, then fall back to per-field regex extraction."""
            json_match = _re.search(r'\{.*\}', text, _re.DOTALL)
            if not json_match:
                return None
            blob = json_match.group()
            try:
                return json.loads(blob)
            except json.JSONDecodeError:
                pass
            # Fallback: extract each field individually via regex.
            # language and answer are simple quoted strings; code is everything between
            # "code": " and the last closing quote before "answer" or end of object.
            lang_m = _re.search(r'"language"\s*:\s*"([^"]+)"', blob)
            ans_m  = _re.search(r'"answer"\s*:\s*"([^"]*)"', blob)
            # code spans from after `"code": "` to just before `", "answer"` or `"}`
            code_m = _re.search(r'"code"\s*:\s*"(.*?)"\s*(?:,\s*"answer"|,\s*"language"|\})', blob, _re.DOTALL)
            if lang_m and ans_m and code_m:
                return {
                    "language": lang_m.group(1),
                    "code":     code_m.group(1),
                    "answer":   ans_m.group(1),
                }
            return None

        puzzle_data = _extract_puzzle_fields(raw)
        if puzzle_data is None:
            state.active_puzzles.pop(cid, None)
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"AI malformed response (no JSON): {raw[:200]}")
            await thinking_msg.edit(embed=emb("❌ Parse Error", "The AI returned an unexpected format. Try again.", C_RED))
            return
        try:
            code_raw = puzzle_data["code"]
            # Strip markdown code fences if the model ignored the prompt instructions
            code_raw = _re.sub(r'^```[a-zA-Z]*\n?', '', code_raw.strip())
            code_raw = _re.sub(r'\n?```$', '', code_raw)
            code_snippet = code_raw.replace("\\n", "\n").replace("\\t", "\t")
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
    async def cmd_adminhelp(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return
        admin_embed = discord.Embed(title="⚙️ Admin Commands", color=C_GOLD)
        admin_embed.add_field(name="🔧 Server Settings", inline=False, value=(
            "`!settings` — View current server settings\n"
            "`!settings ai-channels #ch... / clear` — Restrict AI commands to channels\n"
            "`!settings cmd-whitelist #ch... / clear` — Allow commands only in channels\n"
            "`!settings cmd-blacklist #ch... / clear` — Disallow commands in channels\n"
            "`!settings chess-channels #ch... / clear` — Restrict chess to channels\n"
            "`!settings shop <item> on|off` — Toggle shop items\n"
            "`!settings quote bypass on|off` — Allow searchquote in any channel (bypass restrictions)\n"
            "`!settings rule34 on|off / channels add|remove|list / ban <tag> / unban <tag> / banned` — rule34 config\n"
            "`!settings soundboard-ratelimit add|remove @user|<userid> / list` — Soundboard rate-limit list"
        ))
        admin_embed.add_field(name="🔍 Moderation", inline=False, value=(
            "`!audit` — Last 5 failed command attempts\n"
            "`!clearbot [n]` — Delete last n bot messages (default 50)\n"
            "`!clearall <n>` — Delete last n messages (any author)\n"
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
                "`!vramtext [text]` — View or set the vRAM display text in !stats"
            ))
            admin_embed.add_field(name="📢 Bot Control", inline=False, value=(
                "`!say <text>` — Make the bot repeat text in channel\n"
                "`!botinvitelink` — Display bot invite link\n"
                "`!invitelink` — Display server invite link\n"
                "`!restart` — Restart the bot process"
            ))
        await send_ephemeral(ctx, embed=admin_embed)



async def setup(bot):
    await bot.add_cog(UtilityCog(bot))
