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
    mocking_font, curse_font, parse_amount, send_ephemeral,
    fetch_member, toggle_member_role, shop_charge, _render_race,
    _delete_after, _edit_board, get_memory_mb, format_uptime, get_version,
    get_system_prompt, _log_audit, log_bot_permission_error, MemberConverter,
)

from src.economy import (
    add_balance, deduct_balance, get_balance, get_guild_house_balance,
    add_guild_house, drain_bot_balance_into_lottery, announce_new_lottery,
    is_insured, get_insurance_expiry, get_guild_ask_model, get_guild_roleplay_model,
    get_guild_coding_model, _ct_now, _ct_today, do_daily_reset, _ensure_user,
)
from src.permissions import (
    is_admin, is_server_admin, can_manage_settings, check_rate_limit,
    check_channel, check_game_channel, check_ai_channel, check_puzzle_channel,
    check_chess_channel, _wrong_channel_reply,
)
from src.persistence import (
    _load_json, save_economy, save_insurance, save_guild_settings,
    save_bot_settings, save_godmode_users, save_bot_roles,
    save_chess_games, save_ragebait, save_mock, save_rigged_slots,
    save_gambler_streak, save_roleplay_state, save_fanfic_histories,
    save_quote_log, save_saved_quotes, save_tax, save_curse, save_lottery,
    load_lottery, load_saved_quotes, get_guild_cfg,
)
from src.ai import (
    enforce_cost, insufficient_funds, check_ollama_connected, keep_typing,
    stream_ollama, finalize, _execute_ollama_stream, respond, respond_roleplay,
    ASK_SYSTEM_PROMPT, FANFIC_SYSTEM_PROMPT, FEATURE_COSTS, _norm_puzzle_answer,
)
from src.config import (
    OLLAMA_MODEL, OLLAMA_BASE_URL, SYSTEM_PROMPT, HISTORY_LIMIT,
    RATE_LIMIT_SECONDS, RACE_TRACK_LEN,
    ACTIVE_CHANNEL_IDS, DISCORD_TOKEN,
    SLOT_REEL, SLOT_JACKPOT_SEED, SLOT_JACKPOT_CONTRIB, SLOT_HOUSE_CHANCE,
    SLOT_MIN_BET, SLOT_MULT_JACKPOT, SLOT_MULT_3BAR, SLOT_MULT_3BELL,
    SLOT_MULT_3LEMON, SLOT_MULT_3CHERRY, SLOT_MULT_2CHERRY, SLOT_MULT_1CHERRY,
    SLOT_JACKPOT_BONUS_MIN_BET, SLOT_JACKPOT_BONUS_MAX_BET, SLOT_JACKPOT_BONUS_MAX_MULT,
    HANGMAN_MAX_WRONG, HANGMAN_BASE_REWARD, HANGMAN_LENGTH_OFFSET,
    HANGMAN_LENGTH_MULT, HANGMAN_UNIQUE_MULT, HANGMAN_RARE_MULT, HANGMAN_ULTRA_RARE_MULT,
    BLACKJACK_NATURAL_MULT, SCRATCH_SYMBOLS, SCRATCHOFF_MAX_DAILY, SCRATCHOFF_PAYOUTS,
    SHOP_NICKNAME_SELF_COST, SHOP_NICKNAME_REMOVE_COST, SHOP_NICKNAME_OTHER_COST,
    SHOP_ROLE_CREATE_COST, SHOP_ROLE_ASSIGN_COST, SHOP_ROLE_REMOVE_COST, SHOP_ROLE_DELETE_COST, SHOP_ROLE_MOVE_COST,
    SHOP_ROLECOLOR_COST, SHOP_ROLECHANNEL_COST, SHOP_LOCK_COST, SHOP_RENAME_COST, SHOP_CHANNEL_COST, SHOP_CHANNEL_DELETE_COST, SHOP_INSURANCE_COST, SHOP_TAX_COST,
    SHOP_MOCK_COST, SHOP_RAGEBAIT_COST, SHOP_MUTE_COST, SHOP_CURSE_COST, SHOP_UNOREVERSE_COST,
    SHOP_INSURANCE_DURATION_SECS, SHOP_MOCK_MESSAGES, SHOP_RAGEBAIT_MESSAGES,
    SHOP_CURSE_MESSAGES, SHOP_MUTE_MINUTES, SHOP_TAX_PER_MESSAGE,
    SHOP_TAX_DURATION_SECS, SOUNDBOARD_WINDOW_SECS, SOUNDBOARD_MAX_SOUNDS,
    DAILY_REWARD, DAILY_RESET_HOUR, INITIAL_BOT_ADMIN_IDS,
)
from src import state



class ShopCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _resolve_role_strict(self, guild: discord.Guild, arg: str) -> "discord.Role | None":
        m = re.match(r"<@&(\d+)>", arg)
        if m:
            return guild.get_role(int(m.group(1)))
        if arg.isdigit():
            return guild.get_role(int(arg))
        return None

    def _resolve_channel_strict(self, guild: discord.Guild, arg: str) -> "discord.TextChannel | None":
        m = re.match(r"<#(\d+)>", arg)
        if m:
            ch = guild.get_channel(int(m.group(1)))
            return ch if isinstance(ch, discord.TextChannel) else None
        if arg.isdigit():
            ch = guild.get_channel(int(arg))
            return ch if isinstance(ch, discord.TextChannel) else None
        return None

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Route !<alias> @user to shop_tax when the alias is a guild-configured tax alias."""
        if not isinstance(error, commands.CommandNotFound):
            return
        if not ctx.guild:
            return
        # Extract the attempted command name from the raw message
        parts = ctx.message.content.strip().split(None, 1)
        if not parts:
            return
        word = parts[0][1:].lower()  # strip "!"
        aliases = get_guild_cfg(ctx.guild.id).get("tax_aliases", {})
        if word not in aliases:
            return
        # Patch ctx so shop_tax sees the alias name as invoked_with
        ctx.invoked_with = word
        rest_args = tuple(parts[1].split()) if len(parts) > 1 else ()
        await self.shop_tax(ctx, *rest_args)

    @commands.group(name="shop", aliases=["store"], invoke_without_command=True)
    async def cmd_shop(self, ctx: commands.Context):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            # Handle !shop <alias> @user for guild-configured tax aliases
            subcommand = ctx.subcommand_passed
            if subcommand:
                aliases = cfg.get("tax_aliases", {})
                if subcommand.lower() in aliases:
                    ctx.invoked_with = subcommand.lower()
                    # Extract @user arg from message: "!shop <alias> <rest>"
                    raw_parts = ctx.message.content.strip().split(None, 2)
                    rest_args = tuple(raw_parts[2].split()) if len(raw_parts) > 2 else ()
                    await self.shop_tax(ctx, *rest_args)
                    return
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return

        _si = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}
        sections = {}

        # Nicknames (sorted by cost)
        if _si.get("nickname", True):
            nickname_items = [
                (SHOP_NICKNAME_SELF_COST,   f"`!shop nickname <new_name>` — Change your own nickname — **{SHOP_NICKNAME_SELF_COST:,} 🪙**"),
                (SHOP_NICKNAME_REMOVE_COST, f"`!shop removenickname` — Remove your own nickname — **{SHOP_NICKNAME_REMOVE_COST:,} 🪙**"),
                (SHOP_NICKNAME_OTHER_COST,  f"`!shop nickname @user <new_name>` — Nickname user — **{SHOP_NICKNAME_OTHER_COST:,} 🪙**"),
            ]
            nickname_items.sort(key=lambda x: x[0])
            sections["🎭 Nicknames"] = [item[1] for item in nickname_items]

        # Roles (sorted by cost)
        role_items = []
        if _si.get("removerole", True):
            role_items.append((SHOP_ROLE_REMOVE_COST, f"`!shop removerole [@user] <name>` — Remove a bot-created role from yourself or another user — **{SHOP_ROLE_REMOVE_COST:,} 🪙**"))
        if _si.get("deleterole", True):
            role_items.append((SHOP_ROLE_DELETE_COST, f"`!shop deleterole <name>` — Permanently delete a bot-created role — **{SHOP_ROLE_DELETE_COST:,} 🪙**"))
        if _si.get("createrole", True):
            role_items.append((SHOP_ROLE_CREATE_COST, f"`!shop createrole @user <name> <hex>` — Create a custom colored role for a user — **{SHOP_ROLE_CREATE_COST:,} 🪙**"))
        if _si.get("assignrole", True):
            role_items.append((SHOP_ROLE_ASSIGN_COST, f"`!shop assignrole @user <name>` — Assign an existing bot-created role to a user — **{SHOP_ROLE_ASSIGN_COST:,} 🪙**"))
        if _si.get("roleup", True):
            role_items.append((SHOP_ROLE_MOVE_COST, f"`!shop roleup <role name>` — Move a bot-created role up one position — **{SHOP_ROLE_MOVE_COST:,} 🪙**"))
        if _si.get("roledown", True):
            role_items.append((SHOP_ROLE_MOVE_COST, f"`!shop roledown <role name>` — Move a bot-created role down one position — **{SHOP_ROLE_MOVE_COST:,} 🪙**"))
        if _si.get("rolecolor", True):
            role_items.append((SHOP_ROLECOLOR_COST, f"`!shop rolecolor @role <color>` — Change a role's color — **{SHOP_ROLECOLOR_COST:,} 🪙**"))
        if _si.get("rolechannel", True):
            role_items.append((SHOP_ROLECHANNEL_COST, f"`!shop rolechannel @role #channel` — Restrict a channel to a role — **{SHOP_ROLECHANNEL_COST:,} 🪙**"))
        if _si.get("renamerole", True):
            role_items.append((SHOP_RENAME_COST, f"`!shop renamerole @role | <new name>` — Rename a bot-created role — **{SHOP_RENAME_COST:,} 🪙**"))
        if _si.get("lockrole", True):
            role_items.append((SHOP_LOCK_COST, f"`!shop lockrole <role name>` — Lock a role against changes — **{SHOP_LOCK_COST:,} 🪙**"))
        if role_items:
            role_items.sort(key=lambda x: x[0])
            items_list = [item[1] for item in role_items]
            if _si.get("lockrole", True):
                items_list.append(f"`!shop unlockrole <role name>` — Unlock a role (lock owner only)")
            sections["👑 Roles"] = items_list

        # Channels (sorted by cost)
        channel_items = []
        if _si.get("channel", True):
            channel_items.append((SHOP_CHANNEL_COST, f"`!shop createchannel <name>` — Create a new text channel — **{SHOP_CHANNEL_COST:,} 🪙**"))
        if _si.get("channel", True):
            channel_items.append((SHOP_CHANNEL_DELETE_COST, f"`!shop deletechannel <name>` — Delete a bot-created channel — **{SHOP_CHANNEL_DELETE_COST:,} 🪙**"))
        if _si.get("renamechannel", True):
            channel_items.append((SHOP_RENAME_COST, f"`!shop renamechannel <channel> <new name>` — Rename a bot-created channel — **{SHOP_RENAME_COST:,} 🪙**"))
        if _si.get("lockchannel", True):
            channel_items.append((SHOP_LOCK_COST, f"`!shop lockchannel #channel` — Lock a channel against changes — **{SHOP_LOCK_COST:,} 🪙**"))
        if channel_items:
            channel_items.sort(key=lambda x: x[0])
            items_list = [item[1] for item in channel_items]
            if _si.get("lockchannel", True):
                items_list.append(f"`!shop unlockchannel #channel` — Unlock a channel (lock owner only)")
            sections["📢 Channels"] = items_list

        # Fun & Social (sorted by cost)
        fun_items = [
            (SHOP_INSURANCE_COST, f"`!shop insurance` — Protect yourself for 24 hours — **{SHOP_INSURANCE_COST:,} 🪙**"),
            (SHOP_TAX_COST,      f"`!shop tax @user` — Apply a per-message tax to a user for 24h — **{SHOP_TAX_COST:,} 🪙**"),
            (SHOP_MOCK_COST,      f"`!shop mock @user` — Mock someone's next {SHOP_MOCK_MESSAGES} messages — **{SHOP_MOCK_COST:,} 🪙**"),
        ]
        if _si.get("ragebait", True):
            fun_items.append((SHOP_RAGEBAIT_COST, f"`!shop ragebait @user [topic]` — Ragebait for {SHOP_RAGEBAIT_MESSAGES + 1} messages — **{SHOP_RAGEBAIT_COST:,} 🪙**"))
        fun_items.append((SHOP_MUTE_COST,  f"`!shop mute @user` — Server mute for {SHOP_MUTE_MINUTES} minutes — **{SHOP_MUTE_COST:,} 🪙**"))
        fun_items.append((SHOP_CURSE_COST, f"`!shop curse @user` — Curse someone's messages for {SHOP_CURSE_MESSAGES} messages — **{SHOP_CURSE_COST:,} 🪙**"))
        fun_items.append((SHOP_UNOREVERSE_COST, f"`!shop unoreverse @user` — Redirect active mock/ragebait/curse onto someone else — **{SHOP_UNOREVERSE_COST:,} 🪙**"))
        fun_items.sort(key=lambda x: x[0])
        sections["🎉 Fun & Social"] = [item[1] for item in fun_items]

        if not sections:
            await send_ephemeral(ctx, embed=emb("🛒 Shop", "No shop items are currently available.", C_PURPLE))
            return

        desc = "\n\n".join(f"**{section}**\n" + "\n".join(items) for section, items in sections.items())
        await send_ephemeral(ctx, embed=emb("🛒 Shop", desc, C_PURPLE))

    # ── !shop nickname ────────────────────────────────────────────────────────
    @cmd_shop.command(name="nickname")
    async def shop_nickname(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id
        _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

        if not _shop_cfg.get("nickname", True):
            await ctx.send(embed=emb("🛒 Disabled", "The nickname shop item is disabled in this server.", C_GREY))
            return
        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop nickname <new_name>` or `!shop nickname @user <new_name>`", C_PURPLE))
            return

        try:
            maybe_member = await MemberConverter().convert(ctx, args[0])
            target = maybe_member
            new_name = " ".join(args[1:])
            cost = 0 if uid in state.godmode_users else SHOP_NICKNAME_OTHER_COST
            cost_label = f"{SHOP_NICKNAME_OTHER_COST:,}"
        except commands.BadArgument:
            target = ctx.author
            new_name = " ".join(args)
            cost = 0 if uid in state.godmode_users else SHOP_NICKNAME_SELF_COST
            cost_label = f"{SHOP_NICKNAME_SELF_COST:,}"

        if not new_name:
            await ctx.send(embed=emb("🛒 Shop", "Please provide a new nickname.", C_PURPLE))
            return
        if target.id != uid and is_insured(target.id, "nickname"):
            _exp = get_insurance_expiry(target.id)
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance and can't be renamed (expires <t:{_exp}:R>).", C_GOLD))
            return
        if not await shop_charge(ctx, uid, cost, cost_label=cost_label):
            return
        try:
            await target.edit(nick=new_name)
            await ctx.send(embed=emb("✅ Nickname Changed", f"**{target.display_name}**'s nickname is now **{new_name}**!", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                await add_balance(uid, cost)
            log_bot_permission_error(ctx, "change nickname")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to change that nickname.", C_RED))
        except discord.HTTPException as e:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))

    # ── !shop removenickname ──────────────────────────────────────────────────
    @cmd_shop.command(name="removenickname")
    async def shop_removenickname(self, ctx: commands.Context):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id
        _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

        if not _shop_cfg.get("nickname", True):
            await ctx.send(embed=emb("🛒 Disabled", "The nickname shop item is disabled in this server.", C_GREY))
            return
        cost = 0 if uid in state.godmode_users else SHOP_NICKNAME_REMOVE_COST
        if is_insured(uid, "nickname"):
            _exp = get_insurance_expiry(uid)
            await ctx.send(embed=emb("🛡️ Protected", f"**{ctx.author.display_name}** has insurance and can't have their nickname changed (expires <t:{_exp}:R>).", C_GOLD))
            return
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_NICKNAME_REMOVE_COST:,}"):
            return
        try:
            await ctx.author.edit(nick=None)
            await ctx.send(embed=emb("✅ Nickname Removed", "Your nickname has been reset to your username.", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to change your nickname.", C_RED))
        except discord.HTTPException as e:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))

    # ── !shop createrole ──────────────────────────────────────────────────────
    @cmd_shop.command(name="createrole")
    async def shop_createrole(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id
        _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

        if not _shop_cfg.get("createrole", True):
            await ctx.send(embed=emb("🛒 Disabled", "The createrole shop item is disabled in this server.", C_GREY))
            return
        if len(args) < 3:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop createrole @user <name> <hex_color>` (e.g. `!shop createrole @CoolGuy MyRole ff00aa`)", C_PURPLE))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        try:
            target = await MemberConverter().convert(ctx, args[0])
        except commands.BadArgument:
            await ctx.send(embed=emb("❌ Invalid User", "First argument must be a @mention, user ID, or name.", C_RED))
            return
        hex_color = args[-1].lstrip("#")
        name = " ".join(args[1:-1])
        if "admin" in name.lower():
            await ctx.send(embed=emb("❌ Invalid Name", "Role names cannot contain \"admin\".", C_RED))
            return
        try:
            color_int = int(hex_color, 16)
            if not (0 <= color_int <= 0xFFFFFF):
                raise ValueError
        except ValueError:
            await ctx.send(embed=emb("❌ Invalid Color", "Example: `ff00aa` or `#ff00aa`", C_RED))
            return
        if target.id != uid and is_insured(target.id, "role"):
            _exp = get_insurance_expiry(target.id)
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance and can't be given new roles (expires <t:{_exp}:R>).", C_GOLD))
            return
        cost = 0 if uid in state.godmode_users else SHOP_ROLE_CREATE_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLE_CREATE_COST:,}"):
            return
        try:
            new_role = await ctx.guild.create_role(name=name, color=discord.Color(color_int), hoist=True)
            await target.add_roles(new_role)
            state.bot_roles.add(new_role.id)
            await save_bot_roles()
            await ctx.send(embed=emb("✅ Role Created", f"Role **{name}** created and assigned to **{target.display_name}**!", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                await add_balance(uid, cost)
            log_bot_permission_error(ctx, "manage roles")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to manage roles.", C_RED))
        except Exception as e:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))

    # ── !shop assignrole ──────────────────────────────────────────────────────
    @cmd_shop.command(name="assignrole")
    async def shop_assignrole(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id
        _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

        if not _shop_cfg.get("assignrole", True):
            await ctx.send(embed=emb("🛒 Disabled", "The assignrole shop item is disabled in this server.", C_GREY))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if len(args) < 2:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop assignrole @user @role`", C_PURPLE))
            return
        try:
            target = await MemberConverter().convert(ctx, args[0])
        except commands.BadArgument:
            await ctx.send(embed=emb("❌ Invalid User", "First argument must be a @mention, user ID, or name.", C_RED))
            return
        role = self._resolve_role_strict(ctx.guild, args[1])
        if role is None or role.id not in state.bot_roles:
            await ctx.send(embed=emb("❌ Not Found", "Could not find that bot-created role. Use a @mention or role ID.", C_RED))
            return
        if role in target.roles:
            await ctx.send(embed=emb("❌ Already Has Role", f"**{target.display_name}** already has the **{role.name}** role.", C_RED))
            return
        if role.id in state.locked_roles and state.locked_roles[role.id] != uid and uid not in state.godmode_users:
            await ctx.send(embed=emb("🔒 Locked", f"**{role.name}** is locked — only its owner can manage membership.", C_RED))
            return
        if target.id != uid and is_insured(target.id, "role"):
            _exp = get_insurance_expiry(target.id)
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance and can't be given new roles (expires <t:{_exp}:R>).", C_GOLD))
            return
        cost = 0 if uid in state.godmode_users else SHOP_ROLE_ASSIGN_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLE_ASSIGN_COST:,}"):
            return
        try:
            await target.add_roles(role)
            await ctx.send(embed=emb("✅ Role Assigned", f"Role **{role.name}** assigned to **{target.display_name}**.", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                await add_balance(uid, cost)
            log_bot_permission_error(ctx, "manage roles")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to manage roles.", C_RED))
        except Exception as e:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))

    # ── !shop removerole ──────────────────────────────────────────────────────
    @cmd_shop.command(name="removerole")
    async def shop_removerole(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id
        _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

        if not _shop_cfg.get("removerole", True):
            await ctx.send(embed=emb("🛒 Disabled", "The removerole shop item is disabled in this server.", C_GREY))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if args:
            try:
                member = await MemberConverter().convert(ctx, args[0])
                role_args = args[1:]
            except commands.BadArgument:
                member = ctx.author
                role_args = args
        else:
            member = ctx.author
            role_args = args
        if not role_args:
            existing = [r for r in member.roles if r.id in state.bot_roles]
            who = "They don't" if member != ctx.author else "You don't"
            whose = f"{member.display_name}'s" if member != ctx.author else "Your"
            if not existing:
                await ctx.send(embed=emb("🛒 Bot Roles", f"{who} have any bot-created roles to remove.", C_PURPLE))
            else:
                lines = "\n".join(f"• **{r.name}** (`{r.id}`)" for r in existing)
                await ctx.send(embed=emb("🛒 Bot Roles", f"{whose} removable roles:\n{lines}\n\nUse `!shop removerole [@user] @role` to remove one.", C_PURPLE))
            return
        role = self._resolve_role_strict(ctx.guild, role_args[0])
        if role is None or role.id not in state.bot_roles or role not in member.roles:
            who = member.display_name if member != ctx.author else "you"
            await ctx.send(embed=emb("❌ Not Found", f"**{who}** doesn't have that bot-created role. Use a @mention or role ID.", C_RED))
            return
        if role.id in state.locked_roles and state.locked_roles[role.id] != uid and uid not in state.godmode_users:
            await ctx.send(embed=emb("🔒 Locked", f"**{role.name}** is locked — only its owner can manage membership.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_ROLE_REMOVE_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLE_REMOVE_COST:,}"):
            return
        try:
            await member.remove_roles(role)
            who = member.display_name if member != ctx.author else "you"
            await ctx.send(embed=emb("✅ Role Removed", f"Role **{role.name}** has been removed from **{who}**.", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                await add_balance(uid, cost)
            log_bot_permission_error(ctx, "remove role")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to remove that role.", C_RED))
        except Exception as e:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))

    # ── !shop deleterole ──────────────────────────────────────────────────────
    @cmd_shop.command(name="deleterole")
    async def shop_deleterole(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id
        _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

        if not _shop_cfg.get("deleterole", True):
            await ctx.send(embed=emb("🛒 Disabled", "The deleterole shop item is disabled in this server.", C_GREY))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            existing = [r for r in ctx.guild.roles if r.id in state.bot_roles]
            if not existing:
                await ctx.send(embed=emb("🛒 Bot Roles", "No bot-created roles found in this server.", C_PURPLE))
            else:
                lines = "\n".join(f"• **{r.name}** (`{r.id}`)" for r in existing)
                await ctx.send(embed=emb("🛒 Bot Roles", f"Deletable roles:\n{lines}\n\nUse `!shop deleterole @role` to permanently delete one.", C_PURPLE))
            return
        role = self._resolve_role_strict(ctx.guild, args[0])
        if role is None or role.id not in state.bot_roles:
            await ctx.send(embed=emb("❌ Not Found", "Could not find that bot-created role. Use a @mention or role ID.", C_RED))
            return
        if role.id in state.locked_roles and state.locked_roles[role.id] != uid and uid not in state.godmode_users:
            await ctx.send(embed=emb("🔒 Locked", f"**{role.name}** is locked — only its owner can delete it.", C_RED))
            return
        insured_members = [m for m in role.members if is_insured(m.id, "role")]
        if insured_members:
            names = ", ".join(f"**{m.display_name}**" for m in insured_members)
            _earliest = min(get_insurance_expiry(m.id) for m in insured_members)
            await ctx.send(embed=emb("🛡️ Protected", f"{names} {'has' if len(insured_members) == 1 else 'have'} insurance — this role can't be deleted (expires <t:{_earliest}:R>).", C_GOLD))
            return
        cost = 0 if uid in state.godmode_users else SHOP_ROLE_DELETE_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLE_DELETE_COST:,}"):
            return
        try:
            name = role.name
            await role.delete()
            state.bot_roles.discard(role.id)
            await save_bot_roles()
            await ctx.send(embed=emb("✅ Role Deleted", f"Role **{name}** has been permanently deleted.", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                await add_balance(uid, cost)
            log_bot_permission_error(ctx, "delete role")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to delete that role.", C_RED))
        except Exception as e:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))

    # ── !shop createchannel ───────────────────────────────────────────────────
    @cmd_shop.command(name="createchannel")
    async def shop_createchannel(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id
        _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

        if not _shop_cfg.get("channel", True):
            await ctx.send(embed=emb("🛒 Disabled", "The channel shop item is disabled in this server.", C_GREY))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop channel <name>`", C_PURPLE))
            return
        channel_name = " ".join(args).lower()
        channel_name = channel_name.replace(" ", "-")[:100]
        if len(channel_name) < 2:
            await ctx.send(embed=emb("❌ Invalid Name", "Channel name must be at least 2 characters.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_CHANNEL_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_CHANNEL_COST:,}"):
            return
        try:
            bot_category = discord.utils.find(lambda c: isinstance(c, discord.CategoryChannel) and c.name.lower() == "bot-channels", ctx.guild.channels)
            if bot_category is None:
                bot_category = await ctx.guild.create_category("bot-channels")
            new_channel = await ctx.guild.create_text_channel(
                channel_name,
                topic=f"Created by {ctx.author.display_name}",
                category=bot_category
            )
            cfg = get_guild_cfg(ctx.guild.id)
            if "bot_channels" not in cfg:
                cfg["bot_channels"] = []
            cfg["bot_channels"].append(new_channel.id)
            await save_guild_settings()
            await ctx.send(embed=emb("✅ Channel Created", f"Channel {new_channel.mention} created in #bot-channels!", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                await add_balance(uid, cost)
            log_bot_permission_error(ctx, "create channels")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to create channels.", C_RED))
        except Exception as e:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))

    # ── !shop deletechannel ───────────────────────────────────────────────────
    @cmd_shop.command(name="deletechannel")
    async def shop_deletechannel(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id
        _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

        if not _shop_cfg.get("channel", True):
            await ctx.send(embed=emb("🛒 Disabled", "The channel shop item is disabled in this server.", C_GREY))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            cfg = get_guild_cfg(ctx.guild.id)
            bot_channel_ids = cfg.get("bot_channels", [])
            existing = [ch for ch in ctx.guild.channels if ch.id in bot_channel_ids]
            if not existing:
                await ctx.send(embed=emb("🛒 Bot Channels", "No bot-created channels found in this server.", C_PURPLE))
            else:
                lines = "\n".join(f"• {ch.mention}" for ch in existing)
                await ctx.send(embed=emb("🛒 Bot Channels", f"Removable channels:\n{lines}\n\nUse `!shop deletechannel #channel` to delete one.", C_PURPLE))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        bot_channel_ids = cfg.get("bot_channels", [])
        channel = self._resolve_channel_strict(ctx.guild, args[0])
        if channel is None or channel.id not in bot_channel_ids:
            await ctx.send(embed=emb("❌ Not Found", "Could not find that bot-created channel. Use a #mention or channel ID.", C_RED))
            return
        if channel.id in state.locked_channels and state.locked_channels[channel.id] != uid and uid not in state.godmode_users:
            await ctx.send(embed=emb("🔒 Locked", f"**{channel.name}** is locked — only its owner can delete it.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_CHANNEL_DELETE_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_CHANNEL_DELETE_COST:,}"):
            return
        try:
            channel_name = channel.name
            await channel.delete()
            cfg["bot_channels"].remove(channel.id)
            await save_guild_settings()
            await ctx.send(embed=emb("✅ Channel Removed", f"Channel **{channel_name}** has been deleted.", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                await add_balance(uid, cost)
            log_bot_permission_error(ctx, "delete channel")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to delete that channel.", C_RED))
        except Exception as e:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))

    # ── !shop renamechannel ───────────────────────────────────────────────────
    @cmd_shop.command(name="renamechannel")
    async def shop_renamechannel(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id
        _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

        if not _shop_cfg.get("renamechannel", True):
            await ctx.send(embed=emb("🛒 Disabled", "The renamechannel shop item is disabled in this server.", C_GREY))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if len(args) < 2:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop renamechannel <channel> <new name>`", C_PURPLE))
            return
        new_name = " ".join(args[1:]).lower().replace(" ", "-")[:100]
        if len(new_name) < 2:
            await ctx.send(embed=emb("❌ Invalid Name", "Channel name must be at least 2 characters.", C_RED))
            return
        target_channel = self._resolve_channel_strict(ctx.guild, args[0])
        if target_channel is None or not isinstance(target_channel, discord.TextChannel):
            await ctx.send(embed=emb("❌ Not Found", "Could not find that bot-created channel. Use a #mention or channel ID.", C_RED))
            return
        if target_channel.id in state.locked_channels and state.locked_channels[target_channel.id] != uid and uid not in state.godmode_users:
            await ctx.send(embed=emb("🔒 Locked", f"**{target_channel.name}** is locked — only its owner can rename it.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_RENAME_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_RENAME_COST:,}"):
            return
        try:
            old_name = target_channel.name
            await target_channel.edit(name=new_name)
            await ctx.send(embed=emb("✅ Channel Renamed", f"**{old_name}** renamed to **{new_name}**.", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                await add_balance(uid, cost)
            log_bot_permission_error(ctx, "edit channel")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to rename that channel.", C_RED))
        except Exception as e:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))

    # ── !shop renamerole ──────────────────────────────────────────────────────
    @cmd_shop.command(name="renamerole")
    async def shop_renamerole(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id
        _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

        if not _shop_cfg.get("renamerole", True):
            await ctx.send(embed=emb("🛒 Disabled", "The renamerole shop item is disabled in this server.", C_GREY))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        full_arg = " ".join(args)
        if " | " not in full_arg:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop renamerole @role | <new name>`", C_PURPLE))
            return
        role_arg, new_name = [s.strip() for s in full_arg.split(" | ", 1)]
        if not new_name:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop renamerole @role | <new name>`", C_PURPLE))
            return
        role = self._resolve_role_strict(ctx.guild, role_arg)
        if role is None or role.id not in state.bot_roles:
            await ctx.send(embed=emb("❌ Not Found", "Could not find that bot-created role. Use a @mention or role ID.", C_RED))
            return
        if role.id in state.locked_roles and state.locked_roles[role.id] != uid and uid not in state.godmode_users:
            await ctx.send(embed=emb("🔒 Locked", f"**{role.name}** is locked — only its owner can rename it.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_RENAME_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_RENAME_COST:,}"):
            return
        try:
            old_name = role.name
            await role.edit(name=new_name)
            await ctx.send(embed=emb("✅ Role Renamed", f"**{old_name}** renamed to **{new_name}**.", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                await add_balance(uid, cost)
            log_bot_permission_error(ctx, "edit role")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to rename that role.", C_RED))
        except Exception as e:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))

    # ── !shop rolechannel ─────────────────────────────────────────────────────
    @cmd_shop.command(name="rolechannel")
    async def shop_rolechannel(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id
        _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

        if not _shop_cfg.get("rolechannel", True):
            await ctx.send(embed=emb("🛒 Disabled", "The rolechannel shop item is disabled in this server.", C_GREY))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if len(args) < 2:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop rolechannel @role #channel`", C_PURPLE))
            return
        role = self._resolve_role_strict(ctx.guild, args[0])
        if role is None:
            await ctx.send(embed=emb("❌ Not Found", "Could not find that role. Use a @mention or role ID.", C_RED))
            return
        target_channel = self._resolve_channel_strict(ctx.guild, args[1])
        if target_channel is None or not isinstance(target_channel, discord.TextChannel):
            await ctx.send(embed=emb("❌ Not Found", "Could not find that channel. Use a #mention or channel ID.", C_RED))
            return
        if target_channel.id in state.locked_channels and state.locked_channels[target_channel.id] != uid and uid not in state.godmode_users:
            await ctx.send(embed=emb("🔒 Locked", f"**{target_channel.name}** is locked — only its owner can change its permissions.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_ROLECHANNEL_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLECHANNEL_COST:,}"):
            return
        try:
            await target_channel.set_permissions(ctx.guild.me, read_messages=True, send_messages=True)
            await target_channel.set_permissions(role, read_messages=True)
            await target_channel.set_permissions(ctx.guild.default_role, read_messages=False)
            await ctx.send(embed=emb("✅ Channel Restricted", f"{target_channel.mention} is now only visible to **{role.name}**.", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                await add_balance(uid, cost)
            log_bot_permission_error(ctx, "manage channel permissions")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to manage that channel's permissions.", C_RED))
        except Exception as e:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))

    # ── !shop lockchannel ─────────────────────────────────────────────────────
    @cmd_shop.command(name="lockchannel")
    async def shop_lockchannel(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id
        _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

        if not _shop_cfg.get("lockchannel", True):
            await ctx.send(embed=emb("🛒 Disabled", "The lockchannel shop item is disabled in this server.", C_GREY))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop lockchannel #channel`", C_PURPLE))
            return
        target_channel = self._resolve_channel_strict(ctx.guild, args[0])
        if target_channel is None:
            await ctx.send(embed=emb("❌ Not Found", "Could not find that channel. Use a #mention or channel ID.", C_RED))
            return
        if target_channel.id in state.locked_channels:
            await ctx.send(embed=emb("❌ Already Locked", f"{target_channel.mention} is already locked.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_LOCK_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_LOCK_COST:,}"):
            return
        state.locked_channels[target_channel.id] = uid
        cfg = get_guild_cfg(ctx.guild.id)
        cfg.setdefault("locked_channels", {})[str(target_channel.id)] = uid
        await save_guild_settings()
        await ctx.send(embed=emb("🔒 Channel Locked", f"{target_channel.mention} is now locked. Only you can modify or delete it.", C_GREEN))

    # ── !shop unlockchannel ───────────────────────────────────────────────────
    @cmd_shop.command(name="unlockchannel")
    async def shop_unlockchannel(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id

        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop unlockchannel #channel`", C_PURPLE))
            return
        target_channel = self._resolve_channel_strict(ctx.guild, args[0])
        if target_channel is None:
            await ctx.send(embed=emb("❌ Not Found", "Could not find that channel. Use a #mention or channel ID.", C_RED))
            return
        owner_id = state.locked_channels.get(target_channel.id)
        if owner_id is None:
            await ctx.send(embed=emb("❌ Not Locked", f"{target_channel.mention} is not locked.", C_RED))
            return
        if uid != owner_id and uid not in state.godmode_users:
            await ctx.send(embed=emb("🔒 Access Denied", "Only the user who locked this channel can unlock it.", C_RED))
            return
        del state.locked_channels[target_channel.id]
        cfg = get_guild_cfg(ctx.guild.id)
        cfg.get("locked_channels", {}).pop(str(target_channel.id), None)
        await save_guild_settings()
        await ctx.send(embed=emb("🔓 Channel Unlocked", f"{target_channel.mention} is now unlocked.", C_GREEN))

    # ── !shop lockrole ────────────────────────────────────────────────────────
    @cmd_shop.command(name="lockrole")
    async def shop_lockrole(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id
        _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

        if not _shop_cfg.get("lockrole", True):
            await ctx.send(embed=emb("🛒 Disabled", "The lockrole shop item is disabled in this server.", C_GREY))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop lockrole @role`", C_PURPLE))
            return
        role = self._resolve_role_strict(ctx.guild, args[0])
        if role is None:
            await ctx.send(embed=emb("❌ Not Found", "Could not find that role. Use a @mention or role ID.", C_RED))
            return
        if role.id in state.locked_roles:
            await ctx.send(embed=emb("❌ Already Locked", f"**{role.name}** is already locked.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_LOCK_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_LOCK_COST:,}"):
            return
        state.locked_roles[role.id] = uid
        cfg = get_guild_cfg(ctx.guild.id)
        cfg.setdefault("locked_roles", {})[str(role.id)] = uid
        await save_guild_settings()
        await ctx.send(embed=emb("🔒 Role Locked", f"**{role.name}** is now locked. Only you can modify, delete, or manage membership of this role.", C_GREEN))

    # ── !shop unlockrole ──────────────────────────────────────────────────────
    @cmd_shop.command(name="unlockrole")
    async def shop_unlockrole(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id

        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop unlockrole @role`", C_PURPLE))
            return
        role = self._resolve_role_strict(ctx.guild, args[0])
        if role is None:
            await ctx.send(embed=emb("❌ Not Found", "Could not find that role. Use a @mention or role ID.", C_RED))
            return
        owner_id = state.locked_roles.get(role.id)
        if owner_id is None:
            await ctx.send(embed=emb("❌ Not Locked", f"**{role.name}** is not locked.", C_RED))
            return
        if uid != owner_id and uid not in state.godmode_users:
            await ctx.send(embed=emb("🔒 Access Denied", "Only the user who locked this role can unlock it.", C_RED))
            return
        del state.locked_roles[role.id]
        cfg = get_guild_cfg(ctx.guild.id)
        cfg.get("locked_roles", {}).pop(str(role.id), None)
        await save_guild_settings()
        await ctx.send(embed=emb("🔓 Role Unlocked", f"**{role.name}** is now unlocked.", C_GREEN))

    # ── !shop ragebait ────────────────────────────────────────────────────────
    @cmd_shop.command(name="ragebait")
    async def shop_ragebait(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id
        _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

        if not _shop_cfg.get("ragebait", True):
            await ctx.send(embed=emb("🛒 Disabled", "The ragebait shop item is disabled in this server.", C_GREY))
            return
        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop ragebait @user [topic]`", C_PURPLE))
            return
        try:
            target = await MemberConverter().convert(ctx, args[0])
        except commands.BadArgument:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop ragebait @user [topic]`", C_PURPLE))
            return
        if target.id != uid and is_insured(target.id, "ragebait"):
            _exp = get_insurance_expiry(target.id)
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance against ragebait (expires <t:{_exp}:R>).", C_GOLD))
            return
        topic = " ".join(args[1:])
        cost = 0 if uid in state.godmode_users else SHOP_RAGEBAIT_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_RAGEBAIT_COST:,}"):
            return
        topic_clause = f" The topic should be specifically about: {topic}." if topic else ""
        ragebait_system = (
            "You are an expert at crafting ragebait — messages specifically engineered to provoke "
            "an emotional reaction. Your goal is to write something that will genuinely irritate, "
            "annoy, or get under the skin of the target. "
            "Rules: be specific to the target by referring to them by name (no @ symbols), be witty and cutting rather than just insulting, "
            "use irony or condescension where effective, keep it under 200 characters, "
            "and make it feel natural — like something a person would actually say. "
            "Output only the ragebait message with no preamble, explanation, or quotation marks."
        )
        prompt = (
            f"Write a ragebait message aimed at {target.display_name}.{topic_clause} "
            "Make it personal, pointed, and likely to provoke a reaction. Do not use @ symbols."
        )
        placeholder = await ctx.send("...")
        typing_task = asyncio.create_task(keep_typing(ctx.channel))
        try:
            async with aiohttp.ClientSession() as session:
                full_response = await stream_ollama(session, [
                    {"role": "system", "content": ragebait_system},
                    {"role": "user", "content": prompt},
                ], placeholder)
            await finalize(placeholder, ctx.channel, f"{target.mention} {full_response}")
            state.active_ragebaits[target.id] = {"remaining": SHOP_RAGEBAIT_MESSAGES, "history": [], "channel_id": ctx.channel.id}
            await save_ragebait()
        except Exception as e:
            if cost > 0:
                await add_balance(uid, cost)
            await placeholder.edit(content=f"⚠️ {e}")
        finally:
            typing_task.cancel()

    # ── !shop mock ────────────────────────────────────────────────────────────
    @cmd_shop.command(name="mock")
    async def shop_mock(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id

        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop mock @user`", C_PURPLE))
            return
        try:
            target = await MemberConverter().convert(ctx, args[0])
        except commands.BadArgument:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop mock @user`", C_PURPLE))
            return
        cost = 0 if uid in state.godmode_users else SHOP_MOCK_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_MOCK_COST:,}"):
            return
        state.active_mocks[target.id] = {"remaining": SHOP_MOCK_MESSAGES, "started_by": uid, "channel_id": ctx.channel.id}
        await save_mock()
        await ctx.send(embed=emb(
            "🎭 Mock Activated",
            f"**{target.display_name}** will have their next {SHOP_MOCK_MESSAGES} messages mocked!",
            C_PURPLE,
        ))

    # ── !shop insurance ───────────────────────────────────────────────────────
    @cmd_shop.command(name="insurance")
    async def shop_insurance(self, ctx: commands.Context):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id

        key = str(uid)
        existing = state.insurance.get(key)
        if existing and uid not in state.godmode_users:
            remaining = existing.get("expires_at", 0) - time.time()
            half = SHOP_INSURANCE_DURATION_SECS / 2
            if remaining > half:
                earliest_ts = int(existing["expires_at"] - half)
                await ctx.send(embed=emb(
                    "🛡️ Insurance Active",
                    f"You can't renew until your coverage is half expired. Come back <t:{earliest_ts}:R>.",
                    C_GOLD,
                ))
                return

        cost = 0 if uid in state.godmode_users else SHOP_INSURANCE_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_INSURANCE_COST:,}"):
            return
        expires_at = int(time.time() + SHOP_INSURANCE_DURATION_SECS)
        state.insurance[key] = {
            "expires_at": expires_at,
            "protected_from": ["ragebait", "mock", "nickname", "role", "steal", "tax"],
        }
        await save_insurance()
        await ctx.send(embed=emb(
            "🛡️ Insurance Purchased",
            f"Protected against ragebait, mock, nickname, role changes, steal, and tax! (expires <t:{expires_at}:R>)",
            C_GREEN,
        ))

    # ── !shop rolecolor ───────────────────────────────────────────────────────
    @cmd_shop.command(name="rolecolor")
    async def shop_rolecolor(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id

        if len(args) < 2:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop rolecolor @role <color>`", C_PURPLE))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return
        color_str = args[-1]
        role = self._resolve_role_strict(ctx.guild, args[0])
        if role is None:
            await ctx.send(embed=emb("❌ Not Found", "Could not find that role. Use a @mention or role ID.", C_RED))
            return
        if role.id in state.locked_roles and state.locked_roles[role.id] != uid and uid not in state.godmode_users:
            await ctx.send(embed=emb("🔒 Locked", f"**{role.name}** is locked — only its owner can change its color.", C_RED))
            return
        try:
            color = discord.Color.from_str(color_str)
        except ValueError:
            await ctx.send(embed=emb("❌ Invalid Color", f"Could not parse color: `{color_str}`. Try hex codes like `#FF0000` or color names.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_ROLECOLOR_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLECOLOR_COST:,}"):
            return
        try:
            await role.edit(color=color)
            await ctx.send(embed=emb("🎨 Role Color Changed", f"**{role.name}** color set to `{color_str}`.", C_PURPLE))
        except discord.Forbidden:
            log_bot_permission_error(ctx, "edit roles")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to edit roles.", C_RED))
        except Exception as e:
            await ctx.send(embed=emb("❌ Error", f"Failed to change role color: {str(e)}", C_RED))

    # ── !shop mute ────────────────────────────────────────────────────────────
    @cmd_shop.command(name="mute")
    async def shop_mute(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id

        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop mute @user`", C_PURPLE))
            return
        try:
            target = await MemberConverter().convert(ctx, args[0])
        except commands.BadArgument:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop mute @user`", C_PURPLE))
            return
        cost = 0 if uid in state.godmode_users else SHOP_MUTE_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_MUTE_COST:,}"):
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return
        try:
            member = await fetch_member(ctx.guild, target.id)
            if not member:
                await ctx.send(embed=emb("❌ User Not Found", f"Could not find **{target.display_name}** in this server.", C_RED))
                return
            await member.edit(timed_out_until=discord.utils.utcnow() + datetime.timedelta(minutes=SHOP_MUTE_MINUTES))
            await ctx.send(embed=emb(
                "🔕 Muted",
                f"**{target.display_name}** has been muted for {SHOP_MUTE_MINUTES} minutes!",
                C_PURPLE,
            ))
        except discord.Forbidden:
            log_bot_permission_error(ctx, "timeout members")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to timeout members.", C_RED))
        except Exception as e:
            await ctx.send(embed=emb("❌ Error", f"Failed to mute: {str(e)}", C_RED))

    # ── !shop tax (+ guild-configured aliases) ───────────────────────────────
    @cmd_shop.command(name="tax")
    async def shop_tax(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id

        # Determine the label and emoji: defaults for plain !tax, alias values otherwise
        invoked = ctx.invoked_with or "tax"
        guild_aliases = {}
        if ctx.guild:
            guild_aliases = get_guild_cfg(ctx.guild.id).get("tax_aliases", {})
        if invoked in guild_aliases:
            tax_type = invoked
            tax_emoji = guild_aliases[invoked]
        else:
            tax_type = "tax"
            tax_emoji = "💰"

        if not args:
            aliases_line = ""
            if guild_aliases:
                aliases_line = "\nAliases: " + ", ".join(f"{e} `!{w}`" for w, e in guild_aliases.items())
            await ctx.send(embed=emb("🛒 Shop", f"Usage: `!shop tax @user`{aliases_line}", C_PURPLE))
            return
        try:
            target = await MemberConverter().convert(ctx, args[0])
        except commands.BadArgument:
            await ctx.send(embed=emb("🛒 Shop", f"Usage: `!shop tax @user`", C_PURPLE))
            return
        if target.id == uid:
            await ctx.send(embed=emb("❌ Error", "You can't tax yourself!", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_TAX_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_TAX_COST:,}"):
            return
        activated_at = time.time()
        expires_ts = int(activated_at + SHOP_TAX_DURATION_SECS)
        tax_data = {"master": uid, "type": tax_type, "emoji": tax_emoji, "channel_id": ctx.channel.id, "activated_at": activated_at}
        state.active_taxes[target.id] = tax_data
        await save_tax(state.active_taxes)
        label = tax_type.capitalize()
        await ctx.send(embed=emb(
            f"{tax_emoji} {label} Tax Activated",
            f"**{target.display_name}** now owes **{ctx.author.display_name}** **{SHOP_TAX_PER_MESSAGE:,} 🪙** per message! Expires <t:{expires_ts}:R>.",
            C_PURPLE,
        ))

    # ── !shop curse ───────────────────────────────────────────────────────────
    @cmd_shop.command(name="curse")
    async def shop_curse(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id

        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop curse @user`", C_PURPLE))
            return
        try:
            target = await MemberConverter().convert(ctx, args[0])
        except commands.BadArgument:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop curse @user`", C_PURPLE))
            return
        if target.id == uid:
            await ctx.send(embed=emb("❌ Self Curse", "You can't curse yourself!", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_CURSE_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_CURSE_COST:,}"):
            return
        state.active_curses[target.id] = {"cursed_by": uid, "remaining": SHOP_CURSE_MESSAGES}
        await save_curse(state.active_curses)
        await ctx.send(embed=emb(
            "🔮 Curse Activated",
            f"**{target.display_name}** is now cursed for the next **{SHOP_CURSE_MESSAGES}** messages!",
            C_PURPLE,
        ))

    # ── !shop unoreverse ──────────────────────────────────────────────────────
    @cmd_shop.command(name="unoreverse")
    async def shop_unoreverse(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id

        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop unoreverse @user`", C_PURPLE))
            return
        try:
            target = await MemberConverter().convert(ctx, args[0])
        except commands.BadArgument:
            await ctx.send(embed=emb("❌ Invalid User", "Please mention a valid user.", C_RED))
            return
        if target.id == uid:
            await ctx.send(embed=emb("❌ Self Reverse", "You can't uno reverse yourself!", C_RED))
            return
        has_mock     = uid in state.active_mocks
        has_ragebait = uid in state.active_ragebaits
        has_curse    = uid in state.active_curses
        if not (has_mock or has_ragebait or has_curse):
            await ctx.send(embed=emb("🔄 Uno Reverse", "You don't have any active mock, ragebait, or curse on you to reverse!", C_GREY))
            return
        if is_insured(target.id, "mock"):
            _exp = get_insurance_expiry(target.id)
            await ctx.send(embed=emb(
                "🛡️ Protected",
                f"**{target.display_name}** has insurance and can't be targeted (expires <t:{_exp}:R>).",
                C_GOLD,
            ))
            return
        cost = 0 if uid in state.godmode_users else SHOP_UNOREVERSE_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_UNOREVERSE_COST:,}"):
            return
        redirected = []
        if has_mock:
            mock_data = state.active_mocks.pop(uid)
            mock_data["started_by"] = uid
            state.active_mocks[target.id] = mock_data
            await save_mock()
            redirected.append("mock")
        if has_ragebait:
            rage_data = state.active_ragebaits.pop(uid)
            rage_data["history"] = []
            state.active_ragebaits[target.id] = rage_data
            await save_ragebait()
            redirected.append("ragebait")
        if has_curse:
            curse_data = state.active_curses.pop(uid)
            curse_data["cursed_by"] = uid
            state.active_curses[target.id] = curse_data
            await save_curse(state.active_curses)
            redirected.append("curse")
        await ctx.send(embed=emb(
            "🔄 Uno Reverse!",
            f"**{ctx.author.display_name}** reversed {', '.join(redirected)} onto **{target.display_name}**!",
            C_PURPLE,
        ))

    # ── !shop roleup / roledown ───────────────────────────────────────────────
    @cmd_shop.command(name="roleup", aliases=["roledown"])
    async def shop_roleup(self, ctx: commands.Context, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id
        _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

        direction = ctx.invoked_with  # "roleup" or "roledown"
        cfg_key = "roleup" if direction == "roleup" else "roledown"
        if not _shop_cfg.get(cfg_key, True):
            await ctx.send(embed=emb("🛒 Disabled", f"The {direction} shop item is disabled in this server.", C_GREY))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            await ctx.send(embed=emb("🛒 Shop", f"Usage: `!shop {direction} @role`", C_PURPLE))
            return
        role = self._resolve_role_strict(ctx.guild, args[0])
        if role is None or role.id not in state.bot_roles:
            await ctx.send(embed=emb("❌ Not Found", "Could not find that bot-created role. Use a @mention or role ID.", C_RED))
            return
        if role.id in state.locked_roles and state.locked_roles[role.id] != uid and uid not in state.godmode_users:
            await ctx.send(embed=emb("🔒 Locked", f"**{role.name}** is locked — only its owner can move it.", C_RED))
            return
        other_bot_roles = sorted(
            (r for r in ctx.guild.roles if r.id in state.bot_roles and r.id != role.id),
            key=lambda r: r.position
        )
        if direction == "roleup" and not any(r.position >= role.position for r in other_bot_roles):
            await ctx.send(embed=emb("❌ Already Highest", f"**{role.name}** is already the highest bot-created role.", C_RED))
            return
        if direction == "roledown" and not any(r.position <= role.position for r in other_bot_roles):
            await ctx.send(embed=emb("❌ Already Lowest", f"**{role.name}** is already the lowest bot-created role.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_ROLE_MOVE_COST
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLE_MOVE_COST:,}"):
            return
        if direction == "roleup":
            higher = [r for r in other_bot_roles if r.position > role.position]
            if higher:
                next_role = min(higher, key=lambda r: r.position)
                new_pos = next_role.position + 1
            else:
                # New roles default to position 1, so multiple bot roles can share a position until moved.
                tied = [r for r in other_bot_roles if r.position == role.position]
                new_pos = max(r.position for r in tied) + 1
        else:
            lower = [r for r in other_bot_roles if r.position < role.position]
            if lower:
                next_role = max(lower, key=lambda r: r.position)
                new_pos = next_role.position - 1
            else:
                tied = [r for r in other_bot_roles if r.position == role.position]
                new_pos = min(r.position for r in tied) - 1
        new_pos = max(1, min(new_pos, ctx.guild.me.top_role.position - 1))
        try:
            await role.edit(position=new_pos)
            label = "up" if direction == "roleup" else "down"
            updated_bot_roles = sorted(
                (r for r in ctx.guild.roles if r.id in state.bot_roles),
                key=lambda r: r.position, reverse=True
            )
            total_bot_roles = len(updated_bot_roles)
            rank = next((i + 1 for i, r in enumerate(updated_bot_roles) if r.id == role.id), total_bot_roles)
            await ctx.send(embed=emb("✅ Role Moved", f"Role **{role.name}** moved {label} — now **#{rank}** of {total_bot_roles}.", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                await add_balance(uid, cost)
            log_bot_permission_error(ctx, "manage roles")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to manage roles.", C_RED))
        except Exception as e:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))

    # ── Top-level aliases for all shop subcommands ────────────────────────────
    @commands.command(name="nickname")
    async def cmd_nickname(self, ctx: commands.Context, *args):
        await self.shop_nickname(ctx, *args)

    @commands.command(name="removenickname")
    async def cmd_removenickname(self, ctx: commands.Context):
        await self.shop_removenickname(ctx)

    @commands.command(name="createrole")
    async def cmd_createrole(self, ctx: commands.Context, *args):
        await self.shop_createrole(ctx, *args)

    @commands.command(name="assignrole")
    async def cmd_assignrole(self, ctx: commands.Context, *args):
        await self.shop_assignrole(ctx, *args)

    @commands.command(name="removerole")
    async def cmd_removerole(self, ctx: commands.Context, *args):
        await self.shop_removerole(ctx, *args)

    @commands.command(name="deleterole")
    async def cmd_deleterole(self, ctx: commands.Context, *args):
        await self.shop_deleterole(ctx, *args)

    @commands.command(name="createchannel")
    async def cmd_createchannel(self, ctx: commands.Context, *args):
        await self.shop_createchannel(ctx, *args)

    @commands.command(name="deletechannel")
    async def cmd_deletechannel(self, ctx: commands.Context, *args):
        await self.shop_deletechannel(ctx, *args)

    @commands.command(name="renamechannel")
    async def cmd_renamechannel(self, ctx: commands.Context, *args):
        await self.shop_renamechannel(ctx, *args)

    @commands.command(name="renamerole")
    async def cmd_renamerole(self, ctx: commands.Context, *args):
        await self.shop_renamerole(ctx, *args)

    @commands.command(name="rolechannel")
    async def cmd_rolechannel(self, ctx: commands.Context, *args):
        await self.shop_rolechannel(ctx, *args)

    @commands.command(name="lockchannel")
    async def cmd_lockchannel(self, ctx: commands.Context, *args):
        await self.shop_lockchannel(ctx, *args)

    @commands.command(name="unlockchannel")
    async def cmd_unlockchannel(self, ctx: commands.Context, *args):
        await self.shop_unlockchannel(ctx, *args)

    @commands.command(name="lockrole")
    async def cmd_lockrole(self, ctx: commands.Context, *args):
        await self.shop_lockrole(ctx, *args)

    @commands.command(name="unlockrole")
    async def cmd_unlockrole(self, ctx: commands.Context, *args):
        await self.shop_unlockrole(ctx, *args)

    @commands.command(name="ragebait")
    async def cmd_ragebait(self, ctx: commands.Context, *args):
        await self.shop_ragebait(ctx, *args)

    @commands.command(name="mock")
    async def cmd_mock(self, ctx: commands.Context, *args):
        await self.shop_mock(ctx, *args)

    @commands.command(name="insurance")
    async def cmd_insurance(self, ctx: commands.Context):
        await self.shop_insurance(ctx)

    @commands.command(name="rolecolor")
    async def cmd_rolecolor(self, ctx: commands.Context, *args):
        await self.shop_rolecolor(ctx, *args)

    @commands.command(name="mute")
    async def cmd_mute(self, ctx: commands.Context, *args):
        await self.shop_mute(ctx, *args)

    @commands.command(name="tax")
    async def cmd_tax(self, ctx: commands.Context, *args):
        await self.shop_tax(ctx, *args)

    @commands.command(name="curse")
    async def cmd_curse(self, ctx: commands.Context, *args):
        await self.shop_curse(ctx, *args)

    @commands.command(name="unoreverse")
    async def cmd_unoreverse(self, ctx: commands.Context, *args):
        await self.shop_unoreverse(ctx, *args)

    @commands.command(name="roleup")
    async def cmd_roleup(self, ctx: commands.Context, *args):
        await self.shop_roleup(ctx, *args)

    @commands.command(name="roledown")
    async def cmd_roledown(self, ctx: commands.Context, *args):
        await self.shop_roleup(ctx, *args)

    @commands.command(name="roles", aliases=["rolelb", "lbroles", "lbr"])
    async def cmd_roles(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        bot_roles = sorted(
            (r for r in ctx.guild.roles if r.id in state.bot_roles),
            key=lambda r: r.position, reverse=True
        )
        if not bot_roles:
            await ctx.send(embed=emb("👑 Role Leaderboard", "No bot-created roles in this server yet.", C_PURPLE))
            return
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, role in enumerate(bot_roles):
            prefix = medals[i] if i < 3 else f"{i + 1}."
            color_hex = f"#{role.color.value:06x}" if role.color.value else "default"
            lines.append(f"{prefix} **{role.name}** ({color_hex})")
        lines.append("\n*Also: `!lb` currency · `!levels` XP*")
        await ctx.send(embed=emb("👑 Role Leaderboard", "\n".join(lines), C_PURPLE))


async def setup(bot):
    await bot.add_cog(ShopCog(bot))
