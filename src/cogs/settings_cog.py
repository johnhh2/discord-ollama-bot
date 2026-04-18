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
    get_system_prompt, _log_audit, log_bot_permission_error, MemberConverter,
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
    save_quote_log, save_saved_quotes, save_simp, save_curse, save_lottery,
    load_lottery, load_saved_quotes, get_guild_cfg,
)
from src.ai import (
    enforce_cost, insufficient_funds, check_ollama_connected, keep_typing,
    stream_ollama, finalize, _execute_ollama_stream, respond, respond_roleplay,
    ASK_SYSTEM_PROMPT, FANFIC_SYSTEM_PROMPT, FEATURE_COSTS, _norm_puzzle_answer,
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
    SHOP_ROLECOLOR_COST, SHOP_CHANNEL_COST, SHOP_INSURANCE_COST, SHOP_SIMP_COST,
    SHOP_MOCK_COST, SHOP_RAGEBAIT_COST, SHOP_MUTE_COST, SHOP_CURSE_COST,
    SHOP_INSURANCE_DURATION_SECS, SHOP_MOCK_MESSAGES, SHOP_RAGEBAIT_MESSAGES,
    SHOP_CURSE_MESSAGES, SHOP_MUTE_MINUTES, SHOP_SIMP_TAX_PER_MESSAGE,
    SHOP_CONCUBINE_DURATION_SECS, SOUNDBOARD_WINDOW_SECS, SOUNDBOARD_MAX_SOUNDS,
    DAILY_REWARD, DAILY_RESET_HOUR, INITIAL_BOT_ADMIN_ID,
)
from src import state


class SettingsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(name="settings", aliases=["setting"], invoke_without_command=True)
    async def cmd_settings(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        if not await check_command_permission(ctx):
            return

        cfg = get_guild_cfg(ctx.guild.id)
        ai_channels = cfg.get("ai_channels", [])
        cmd_whitelist = cfg.get("command_whitelist", [])
        cmd_blacklist = cfg.get("command_blacklist", [])
        game_channels = cfg.get("game_channels", [])
        chess_channels = cfg.get("chess_channels", [])
        shop_items = cfg.get("shop_items", {})
        r34_enabled = cfg.get("rule34_enabled", True)
        r34_channels = cfg.get("rule34_channels", [])
        r34_banned = cfg.get("rule34_banned_tags", [])
        lottery_channel_id = cfg.get("lottery_channel")

        ai_val = " ".join(f"<#{c}>" for c in ai_channels) if ai_channels else "all channels"
        whitelist_val = " ".join(f"<#{c}>" for c in cmd_whitelist) if cmd_whitelist else "none (all allowed)"
        blacklist_val = " ".join(f"<#{c}>" for c in cmd_blacklist) if cmd_blacklist else "none"
        game_val = " ".join(f"<#{c}>" for c in game_channels) if game_channels else "all channels"
        chess_val = " ".join(f"<#{c}>" for c in chess_channels) if chess_channels else "game channels (or all)"
        item_names = ["nickname", "role", "removerole", "roleup", "roledown", "ragebait"]
        shop_val = "  ".join(
            f"{n} {'✅' if shop_items.get(n, True) else '❌'}" for n in item_names
        )
        r34_val = ("✅ enabled" if r34_enabled else "❌ disabled")
        r34_ch_val = " ".join(f"<#{c}>" for c in r34_channels) if r34_channels else "all channels"
        r34_val += f"\nChannels: {r34_ch_val}"
        if r34_banned:
            r34_val += f"\nBanned tags: {', '.join(r34_banned)}"
        lottery_val = f"<#{lottery_channel_id}>" if lottery_channel_id else "❌ disabled"
        levelup_channel_id = cfg.get("levelup_channel")
        levelup_val = f"<#{levelup_channel_id}>" if levelup_channel_id else "❌ disabled"
        soundboard_rl = cfg.get("soundboard_ratelimit", [])
        if soundboard_rl:
            rl_names = []
            for uid in soundboard_rl:
                member = ctx.guild.get_member(uid)
                rl_names.append(member.display_name if member else str(uid))
            rl_val = ", ".join(rl_names)
        else:
            rl_val = "none"

        gambler_role_val = "✅ enabled" if cfg.get("gambler_role_enabled", False) else "❌ disabled"

        embed = discord.Embed(title="⚙️ Server Settings", color=C_BLUE)
        embed.add_field(name="🤖 AI channels", value=ai_val, inline=False)
        embed.add_field(name="✅ Channel whitelist", value=whitelist_val, inline=False)
        embed.add_field(name="❌ Channel blacklist", value=blacklist_val, inline=False)
        embed.add_field(name="🎮 Game channels", value=game_val, inline=False)
        embed.add_field(name="♟️ Chess channels", value=chess_val, inline=False)
        embed.add_field(name="🛒 Shop items", value=shop_val, inline=False)
        embed.add_field(name="🔞 rule34", value=r34_val, inline=False)
        embed.add_field(name="🎰 Lottery channel", value=lottery_val, inline=False)
        embed.add_field(name="📊 Level-up channel", value=levelup_val, inline=False)
        embed.add_field(name="🔇 Soundboard rate-limit", value=rl_val, inline=False)
        embed.add_field(name="🎲 Gambler role", value=gambler_role_val, inline=False)
        footer_text = (
            "Subcommands:\n"
            "ai-channels #ch... / clear\n"
            "cmd-whitelist #ch... / clear\n"
            "cmd-blacklist #ch... / clear\n"
            "game-channels #ch... / clear\n"
            "chess-channels #ch... / clear\n"
            "shop <item> on|off\n"
            "rule34 on|off / channels add|remove|list / ban <tag> / unban <tag> / banned\n"
            "lottery-channel #channel / clear\n"
            "soundboard-ratelimit add|remove @user|<userid> / list\n"
            "gambler-role on|off\n"
            "channel-levelup #channel / clear"
        )
        embed.set_footer(text=footer_text)
        await send_ephemeral(ctx, embed=embed)

    # ── !settings ai-channels ─────────────────────────────────────────────────
    @cmd_settings.command(name="ai-channels")
    async def settings_ai_channels(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        if not await check_command_permission(ctx):
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if args and args[0].lower() == "clear":
            cfg["ai_channels"] = []
            save_guild_settings()
            await ctx.send(embed=emb("⚙️ AI Channels", "AI channel restriction removed — all channels allowed.", C_GREEN))
        elif ctx.message.channel_mentions:
            cfg["ai_channels"] = [c.id for c in ctx.message.channel_mentions]
            save_guild_settings()
            names = " ".join(c.mention for c in ctx.message.channel_mentions)
            await ctx.send(embed=emb("⚙️ AI Channels", f"AI commands restricted to: {names}", C_GREEN))
        else:
            await ctx.send(embed=emb("⚙️ AI Channels", "Usage: `!settings ai-channels #channel ...` or `!settings ai-channels clear`", C_GREY))

    # ── !settings cmd-whitelist ───────────────────────────────────────────────
    @cmd_settings.command(name="cmd-whitelist")
    async def settings_cmd_whitelist(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        if not await check_command_permission(ctx):
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if args and args[0].lower() == "clear":
            cfg["command_whitelist"] = []
            save_guild_settings()
            await ctx.send(embed=emb("✅ Channel Whitelist", "Whitelist removed — commands allowed in all channels.", C_GREEN))
        elif ctx.message.channel_mentions:
            cfg["command_whitelist"] = [c.id for c in ctx.message.channel_mentions]
            save_guild_settings()
            names = " ".join(c.mention for c in ctx.message.channel_mentions)
            await ctx.send(embed=emb("✅ Channel Whitelist", f"Commands restricted to: {names}\n(Note: `!settings` always works everywhere)", C_GREEN))
        else:
            await ctx.send(embed=emb("✅ Channel Whitelist", "Usage: `!settings cmd-whitelist #channel ...` or `!settings cmd-whitelist clear`", C_GREY))

    # ── !settings cmd-blacklist ───────────────────────────────────────────────
    @cmd_settings.command(name="cmd-blacklist")
    async def settings_cmd_blacklist(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        if not await check_command_permission(ctx):
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if args and args[0].lower() == "clear":
            cfg["command_blacklist"] = []
            save_guild_settings()
            await ctx.send(embed=emb("❌ Channel Blacklist", "Blacklist cleared — commands allowed in all channels.", C_GREEN))
        elif ctx.message.channel_mentions:
            cfg["command_blacklist"] = [c.id for c in ctx.message.channel_mentions]
            save_guild_settings()
            names = " ".join(c.mention for c in ctx.message.channel_mentions)
            await ctx.send(embed=emb("❌ Channel Blacklist", f"Commands blocked in: {names}", C_GREEN))
        else:
            await ctx.send(embed=emb("❌ Channel Blacklist", "Usage: `!settings cmd-blacklist #channel ...` or `!settings cmd-blacklist clear`", C_GREY))

    # ── !settings chess-channels ──────────────────────────────────────────────
    @cmd_settings.command(name="chess-channels")
    async def settings_chess_channels(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        if not await check_command_permission(ctx):
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if args and args[0].lower() == "clear":
            cfg["chess_channels"] = []
            save_guild_settings()
            await ctx.send(embed=emb("♟️ Chess Channels", "Chess channel restriction removed — all channels allowed.", C_GREEN))
        elif ctx.message.channel_mentions:
            cfg["chess_channels"] = [c.id for c in ctx.message.channel_mentions]
            save_guild_settings()
            names = " ".join(c.mention for c in ctx.message.channel_mentions)
            await ctx.send(embed=emb("♟️ Chess Channels", f"Chess restricted to: {names}", C_GREEN))
        else:
            await ctx.send(embed=emb("♟️ Chess Channels", "Usage: `!settings chess-channels #channel ...` or `!settings chess-channels clear`", C_GREY))

    # ── !settings game-channels ───────────────────────────────────────────────
    @cmd_settings.command(name="game-channels")
    async def settings_game_channels(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        if not await check_command_permission(ctx):
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if args and args[0].lower() == "clear":
            cfg["game_channels"] = []
            save_guild_settings()
            await ctx.send(embed=emb("🎮 Game Channels", "Game channel restriction removed — games and gambling allowed everywhere.", C_GREEN))
        elif ctx.message.channel_mentions:
            cfg["game_channels"] = [c.id for c in ctx.message.channel_mentions]
            save_guild_settings()
            names = " ".join(c.mention for c in ctx.message.channel_mentions)
            await ctx.send(embed=emb("🎮 Game Channels", f"Games and gambling restricted to: {names}", C_GREEN))
        else:
            await ctx.send(embed=emb("🎮 Game Channels", "Usage: `!settings game-channels #channel ...` or `!settings game-channels clear`", C_GREY))

    # ── !settings shop ────────────────────────────────────────────────────────
    @cmd_settings.command(name="shop")
    async def settings_shop(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        if not await check_command_permission(ctx):
            return
        cfg = get_guild_cfg(ctx.guild.id)
        valid_items = {"nickname", "role", "removerole", "roleup", "roledown", "ragebait"}
        if len(args) < 2 or args[0].lower() not in valid_items or args[1].lower() not in ("on", "off"):
            await ctx.send(embed=emb("⚙️ Shop", f"Usage: `!settings shop <item> on|off`\nItems: {', '.join(valid_items)}", C_GREY))
            return
        item = args[0].lower()
        enabled = args[1].lower() == "on"
        if "shop_items" not in cfg:
            cfg["shop_items"] = {}
        cfg["shop_items"][item] = enabled
        save_guild_settings()
        status = "✅ enabled" if enabled else "❌ disabled"
        await ctx.send(embed=emb("⚙️ Shop", f"**{item}** is now {status}.", C_GREEN))

    # ── !settings rule34 ──────────────────────────────────────────────────────
    @cmd_settings.command(name="rule34")
    async def settings_rule34(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        if not await check_command_permission(ctx):
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if not args:
            await ctx.send(embed=emb("⚙️ rule34", "Usage: `!settings rule34 on|off` / `channels <add|remove|list> [#channel]` / `ban <tag>` / `unban <tag>` / `banned`", C_GREY))
            return
        action = args[0].lower()
        if action in ("on", "off"):
            cfg["rule34_enabled"] = (action == "on")
            save_guild_settings()
            status = "✅ enabled" if action == "on" else "❌ disabled"
            await ctx.send(embed=emb("⚙️ rule34", f"rule34 is now {status}.", C_GREEN))
        elif action == "channels":
            if len(args) < 2:
                await ctx.send(embed=emb("⚙️ rule34", "Usage: `!settings rule34 channels <add|remove|list> [#channel]`", C_GREY))
                return
            channel_action = args[1].lower()
            r34_channels = cfg.setdefault("rule34_channels", [])
            if channel_action == "add":
                if not ctx.message.channel_mentions:
                    await ctx.send(embed=emb("⚙️ rule34", "Please mention a channel to add.", C_GREY))
                    return
                for channel in ctx.message.channel_mentions:
                    if channel.id not in r34_channels:
                        r34_channels.append(channel.id)
                save_guild_settings()
                names = " ".join(f"<#{cid}>" for cid in ctx.message.channel_mentions)
                await ctx.send(embed=emb("⚙️ rule34 Channels", f"Added {names} to whitelist.", C_GREEN))
            elif channel_action == "remove":
                if not ctx.message.channel_mentions:
                    await ctx.send(embed=emb("⚙️ rule34", "Please mention a channel to remove.", C_GREY))
                    return
                for channel in ctx.message.channel_mentions:
                    if channel.id in r34_channels:
                        r34_channels.remove(channel.id)
                save_guild_settings()
                names = " ".join(f"<#{cid}>" for cid in ctx.message.channel_mentions)
                await ctx.send(embed=emb("⚙️ rule34 Channels", f"Removed {names} from whitelist.", C_GREEN))
            elif channel_action == "list":
                val = " ".join(f"<#{cid}>" for cid in r34_channels) if r34_channels else "none"
                await ctx.send(embed=emb("⚙️ rule34 Channels", val, C_GREY))
            else:
                await ctx.send(embed=emb("⚙️ rule34", "Usage: `!settings rule34 channels <add|remove|list> [#channel]`", C_GREY))
        elif action == "ban" and len(args) >= 2:
            tag = args[1].lower()
            banned = cfg.setdefault("rule34_banned_tags", [])
            if tag not in banned:
                banned.append(tag)
                save_guild_settings()
            await ctx.send(embed=emb("⚙️ rule34", f"Tag `{tag}` banned.", C_GREEN))
        elif action == "unban" and len(args) >= 2:
            tag = args[1].lower()
            banned = cfg.get("rule34_banned_tags", [])
            if tag in banned:
                banned.remove(tag)
                save_guild_settings()
                await ctx.send(embed=emb("⚙️ rule34", f"Tag `{tag}` unbanned.", C_GREEN))
            else:
                await ctx.send(embed=emb("⚙️ rule34", f"Tag `{tag}` was not banned.", C_GREY))
        elif action == "banned":
            banned = cfg.get("rule34_banned_tags", [])
            val = ", ".join(f"`{t}`" for t in banned) if banned else "none"
            await ctx.send(embed=emb("⚙️ rule34 Banned Tags", val, C_GREY))
        else:
            await ctx.send(embed=emb("⚙️ rule34", "Usage: `!settings rule34 on|off` / `channels <add|remove|list> [#channel]` / `ban <tag>` / `unban <tag>` / `banned`", C_GREY))

    # ── !settings quote ───────────────────────────────────────────────────────
    @cmd_settings.command(name="quote")
    async def settings_quote(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        if not await check_command_permission(ctx):
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if not args:
            await ctx.send(embed=emb("⚙️ quote", "Usage: `!settings quote bypass on|off`", C_GREY))
            return
        action = args[0].lower()
        if action == "bypass":
            if len(args) < 2:
                await ctx.send(embed=emb("⚙️ quote", "Usage: `!settings quote bypass on|off`", C_GREY))
                return
            bypass_action = args[1].lower()
            if bypass_action in ("on", "off"):
                cfg["quote_bypass_restrictions"] = (bypass_action == "on")
                save_guild_settings()
                status = "✅ enabled" if bypass_action == "on" else "❌ disabled"
                await ctx.send(embed=emb("⚙️ quote", f"Quote bypass is now {status} (quote works in any channel).", C_GREEN))
            else:
                await ctx.send(embed=emb("⚙️ quote", "Usage: `!settings quote bypass on|off`", C_GREY))
        else:
            await ctx.send(embed=emb("⚙️ quote", "Usage: `!settings quote bypass on|off`", C_GREY))

    # ── !settings lottery-channel ─────────────────────────────────────────────
    @cmd_settings.command(name="lottery-channel")
    async def settings_lottery_channel(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        if not await check_command_permission(ctx):
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if args and args[0].lower() == "clear":
            cfg["lottery_channel"] = None
            save_guild_settings()
            await ctx.send(embed=emb("🎰 Lottery Channel", "Lottery disabled.", C_GREEN))
        elif ctx.message.channel_mentions:
            channel = ctx.message.channel_mentions[0]
            cfg["lottery_channel"] = channel.id
            save_guild_settings()

            current_week = datetime.datetime.now().isocalendar()[1]
            lottery = load_lottery(ctx.guild.id)
            if lottery.get("last_posted_week", 0) != current_week:
                lottery = {"prize_pool": 2000, "players": {}, "last_posted_week": current_week}
                drain_bot_balance_into_lottery(lottery, ctx.guild.id)
                save_lottery(ctx.guild.id, lottery)
                try:
                    await announce_new_lottery(channel, lottery["prize_pool"])
                except:
                    pass

            await ctx.send(embed=emb("🎰 Lottery Channel", f"Lottery channel set to {channel.mention}\n🎟️ Lottery ready!", C_GREEN))
        else:
            await ctx.send(embed=emb("🎰 Lottery Channel", "Usage: `!settings lottery-channel #channel` or `!settings lottery-channel clear`", C_GREY))

    # ── !settings soundboard-ratelimit ────────────────────────────────────────
    @cmd_settings.command(name="soundboard-ratelimit")
    async def settings_soundboard_ratelimit(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        if not await check_command_permission(ctx):
            return
        cfg = get_guild_cfg(ctx.guild.id)
        action = args[0].lower() if args else ""
        rl_list = cfg.setdefault("soundboard_ratelimit", [])

        if action == "add":
            user_ids = []
            if ctx.message.mentions:
                user_ids.extend([m.id for m in ctx.message.mentions])
            for arg in args[1:]:
                try:
                    user_ids.append(int(arg))
                except ValueError:
                    pass
            if not user_ids:
                await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", "Usage: `!settings soundboard-ratelimit add @user` or `!settings soundboard-ratelimit add <userid>`", C_GREY))
                return
            added = []
            for uid in user_ids:
                if uid not in rl_list:
                    rl_list.append(uid)
                    added.append(f"`{uid}`")
            if added:
                save_guild_settings()
                await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", f"Added: {' '.join(added)}", C_GREEN))
            else:
                await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", "All users already in the list.", C_GREY))

        elif action == "remove":
            user_ids = []
            if ctx.message.mentions:
                user_ids.extend([m.id for m in ctx.message.mentions])
            for arg in args[1:]:
                try:
                    user_ids.append(int(arg))
                except ValueError:
                    pass
            if not user_ids:
                await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", "Usage: `!settings soundboard-ratelimit remove @user` or `!settings soundboard-ratelimit remove <userid>`", C_GREY))
                return
            removed = []
            for uid in user_ids:
                if uid in rl_list:
                    rl_list.remove(uid)
                    removed.append(f"`{uid}`")
            if removed:
                save_guild_settings()
                await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", f"Removed: {' '.join(removed)}", C_RED))
            else:
                await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", "None of those users were in the list.", C_GREY))

        elif action == "list":
            if not rl_list:
                await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", "No users on the list.", C_GREY))
            else:
                await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", f"**{len(rl_list)} user(s):**\n" + " ".join(f"`{uid}`" for uid in rl_list), C_GOLD))

        else:
            await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", "Usage: `!settings soundboard-ratelimit add|remove @user|<userid>` or `list`", C_GREY))

    # ── !settings gambler-role ────────────────────────────────────────────────
    @cmd_settings.command(name="gambler-role")
    async def settings_gambler_role(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        if not await check_command_permission(ctx):
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if not args or args[0].lower() not in ("on", "off"):
            await ctx.send(embed=emb("⚙️ Gambler Role", "Usage: `!settings gambler-role on|off`", C_GREY))
            return
        enabled = args[0].lower() == "on"
        cfg["gambler_role_enabled"] = enabled
        save_guild_settings()
        status = "✅ enabled" if enabled else "❌ disabled"
        detail = "\nThe **Gamblers** role will be auto-created and assigned to users who use all 3 scratchoffs 2 days in a row. They will be pinged when a progressive jackpot is won." if enabled else ""
        await ctx.send(embed=emb("⚙️ Gambler Role", f"Gambler role tracking is now {status}.{detail}", C_GREEN))

    # ── !settings channel-levelup ─────────────────────────────────────────────
    @cmd_settings.command(name="channel-levelup")
    async def settings_channel_levelup(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        if not await check_command_permission(ctx):
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if args and args[0].lower() == "clear":
            cfg["levelup_channel"] = None
            save_guild_settings()
            await ctx.send(embed=emb("📊 Level-Up Channel", "Level-up announcements disabled.", C_GREEN))
        elif ctx.message.channel_mentions:
            channel = ctx.message.channel_mentions[0]
            cfg["levelup_channel"] = channel.id
            save_guild_settings()
            await ctx.send(embed=emb("📊 Level-Up Channel", f"Level-up announcements will be sent to {channel.mention}.", C_GREEN))
        else:
            await ctx.send(embed=emb("📊 Level-Up Channel", "Usage: `!settings channel-levelup #channel` or `!settings channel-levelup clear`", C_GREY))


async def setup(bot):
    await bot.add_cog(SettingsCog(bot))
