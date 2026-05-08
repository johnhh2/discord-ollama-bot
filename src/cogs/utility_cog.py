import asyncio
import json
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
    save_bot_settings, load_saved_quotes
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
            "`!stop` — Stop roleplay / forfeit active game"
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

    @commands.command(name="bug", aliases=["issue", "bugreport"])
    @requires_perm
    async def cmd_bug(self, ctx: commands.Context, *, report: str = None):
        if report is None or not report.strip():
            await ctx.send(embed=emb(
                "🐛 Bug Report",
                "Usage: `!bug <description of the bug>`",
                C_GREY,
            ))
            return

        chan_id = state.bot_settings.get("bug_report_channel")
        if not chan_id:
            await ctx.send(embed=emb(
                "🐛 Bug Report",
                "Bug reporting is not configured on this bot.",
                C_GREY,
            ))
            return

        try:
            channel = self.bot.get_channel(int(chan_id)) or await self.bot.fetch_channel(int(chan_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            channel = None
        if channel is None:
            await ctx.send(embed=emb("🐛 Bug Report", "Could not reach the bug-report channel.", C_RED))
            return

        history_lines = []
        try:
            async for msg in ctx.channel.history(limit=6):
                if msg.id == ctx.message.id:
                    continue
                if len(history_lines) >= 5:
                    break
                history_lines.append(f"[{msg.author.display_name}]: {_msg_text(msg)[:200]}")
            history_lines.reverse()
        except (discord.Forbidden, discord.HTTPException):
            pass

        guild_name = ctx.guild.name if ctx.guild else "DM"
        guild_id_str = str(ctx.guild.id) if ctx.guild else "—"
        chan_ref = ctx.channel.mention if hasattr(ctx.channel, "mention") else str(ctx.channel)
        desc_lines = [
            f"**Time:** <t:{int(time.time())}:f>",
            f"**User:** {ctx.author.display_name} (`{ctx.author.id}`)",
            f"**Guild:** {guild_name} (`{guild_id_str}`)",
            f"**Channel:** {chan_ref}",
            "",
            f"**Report:**\n{report[:1500]}",
        ]
        if history_lines:
            log_block = "\n".join(history_lines)
            if len(log_block) > 1500:
                log_block = log_block[-1500:]
            desc_lines.append(f"\n**Last 5 messages:**\n```\n{log_block}\n```")

        try:
            await channel.send(embed=emb("🐛 Bug Report", "\n".join(desc_lines), C_RED))
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send(embed=emb("🐛 Bug Report", "Could not post the bug report — please try again later.", C_RED))
            return

        await ctx.send(embed=emb("🐛 Bug Report", "Thanks — your report has been sent to the bot admins.", C_GREEN))


async def setup(bot):
    await bot.add_cog(UtilityCog(bot))
