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
    check_chess_channel, _wrong_channel_reply,
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
    SHOP_ROLE_CREATE_COST, SHOP_ROLE_ASSIGN_COST, SHOP_ROLE_REMOVE_COST, SHOP_ROLE_DELETE_COST, SHOP_ROLE_MOVE_COST,
    SHOP_ROLECOLOR_COST, SHOP_CHANNEL_COST, SHOP_INSURANCE_COST, SHOP_SIMP_COST,
    SHOP_MOCK_COST, SHOP_RAGEBAIT_COST, SHOP_MUTE_COST, SHOP_CURSE_COST,
    SHOP_INSURANCE_DURATION_SECS, SHOP_MOCK_MESSAGES, SHOP_RAGEBAIT_MESSAGES,
    SHOP_CURSE_MESSAGES, SHOP_MUTE_MINUTES, SHOP_SIMP_TAX_PER_MESSAGE,
    SHOP_CONCUBINE_DURATION_SECS, SOUNDBOARD_WINDOW_SECS, SOUNDBOARD_MAX_SOUNDS,
    DAILY_REWARD, DAILY_RESET_HOUR, INITIAL_BOT_ADMIN_ID,
)
from src import state



class ShopCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="shop", aliases=["store"])
    async def cmd_shop(self, ctx: commands.Context, subcommand: str = None, *args):
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                return
        uid = ctx.author.id

        if subcommand is None:
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
            if role_items:
                role_items.sort(key=lambda x: x[0])
                sections["👑 Roles"] = [item[1] for item in role_items]

            # Channels (sorted by cost)
            channel_items = []
            if _si.get("channel", True):
                channel_items.append((SHOP_CHANNEL_COST, f"`!shop channel <name>` — Create a new text channel — **{SHOP_CHANNEL_COST:,} 🪙**"))
            if _si.get("channel", True):
                channel_items.append((SHOP_CHANNEL_COST, f"`!shop removechannel <name>` — Delete a bot-created channel — **{SHOP_CHANNEL_COST:,} 🪙**"))
            if channel_items:
                channel_items.sort(key=lambda x: x[0])
                sections["📢 Channels"] = [item[1] for item in channel_items]

            # Fun & Social (sorted by cost)
            fun_items = [
                (SHOP_INSURANCE_COST, f"`!shop insurance` — Protect yourself for 24 hours — **{SHOP_INSURANCE_COST:,} 🪙**"),
                (SHOP_SIMP_COST,      f"`!shop simp @user` — Make a user simp for you — **{SHOP_SIMP_COST:,} 🪙**"),
                (SHOP_MOCK_COST,      f"`!shop mock @user` — Mock someone's next {SHOP_MOCK_MESSAGES} messages — **{SHOP_MOCK_COST:,} 🪙**"),
                (SHOP_ROLECOLOR_COST, f"`!shop rolecolor <role name> <color>` — Change a role's color — **{SHOP_ROLECOLOR_COST:,} 🪙**"),
            ]
            if _si.get("ragebait", True):
                fun_items.append((SHOP_RAGEBAIT_COST, f"`!shop ragebait @user [topic]` — Ragebait for {SHOP_RAGEBAIT_MESSAGES + 1} messages — **{SHOP_RAGEBAIT_COST:,} 🪙**"))
            fun_items.append((SHOP_MUTE_COST,  f"`!shop mute @user` — Server mute for {SHOP_MUTE_MINUTES} minutes — **{SHOP_MUTE_COST:,} 🪙**"))
            fun_items.append((SHOP_CURSE_COST, f"`!shop curse @user` — Curse someone's messages for {SHOP_CURSE_MESSAGES} messages — **{SHOP_CURSE_COST:,} 🪙**"))
            fun_items.sort(key=lambda x: x[0])
            sections["🎉 Fun & Social"] = [item[1] for item in fun_items]

            if not sections:
                await send_ephemeral(ctx, embed=emb("🛒 Shop", "No shop items are currently available.", C_PURPLE))
                return

            desc = "\n\n".join(f"**{section}**\n" + "\n".join(items) for section, items in sections.items())
            await send_ephemeral(ctx, embed=emb("🛒 Shop", desc, C_PURPLE))
            return

        _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

        # ── !shop nickname ────────────────────────────────────────────────────────
        if subcommand == "nickname":
            if not _shop_cfg.get("nickname", True):
                await ctx.send(embed=emb("🛒 Disabled", "The nickname shop item is disabled in this server.", C_GREY))
                return
            if not args:
                await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop nickname <new_name>` or `!shop nickname @user <new_name>`", C_PURPLE))
                return

            # Determine target: @mention or self
            if ctx.message.mentions and args[0].startswith("<@"):
                target = ctx.message.mentions[0]
                new_name = " ".join(args[1:])
                cost = 0 if uid in state.godmode_users else SHOP_NICKNAME_OTHER_COST
                cost_label = f"{SHOP_NICKNAME_OTHER_COST:,}"
            else:
                target = ctx.author
                new_name = " ".join(args)
                cost = 0 if uid in state.godmode_users else SHOP_NICKNAME_SELF_COST
                cost_label = f"{SHOP_NICKNAME_SELF_COST:,}"

            if not new_name:
                await ctx.send(embed=emb("🛒 Shop", "Please provide a new nickname.", C_PURPLE))
                return
            if is_insured(target.id, "nickname"):
                await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance and can't be renamed.", C_GOLD))
                return
            if not await shop_charge(ctx, uid, cost, cost_label=cost_label):
                return
            try:
                await target.edit(nick=new_name)
                await ctx.send(embed=emb("✅ Nickname Changed", f"**{target.display_name}**'s nickname is now **{new_name}**!", C_GREEN))
            except discord.Forbidden:
                if cost > 0:
                    add_balance(uid, cost)
                log_bot_permission_error(ctx, "change nickname")
                await ctx.send(embed=emb("❌ No Permission", "I don't have permission to change that nickname.", C_RED))
            except discord.HTTPException as e:
                if cost > 0:
                    add_balance(uid, cost)
                await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
            return

        # ── !shop removenickname ──────────────────────────────────────────────────
        if subcommand == "removenickname":
            if not _shop_cfg.get("nickname", True):
                await ctx.send(embed=emb("🛒 Disabled", "The nickname shop item is disabled in this server.", C_GREY))
                return
            cost = 0 if uid in state.godmode_users else SHOP_NICKNAME_REMOVE_COST
            if is_insured(uid, "nickname"):
                await ctx.send(embed=emb("🛡️ Protected", f"**{ctx.author.display_name}** has insurance and can't have their nickname changed.", C_GOLD))
                return
            if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_NICKNAME_REMOVE_COST:,}"):
                return
            try:
                await ctx.author.edit(nick=None)
                await ctx.send(embed=emb("✅ Nickname Removed", "Your nickname has been reset to your username.", C_GREEN))
            except discord.Forbidden:
                if cost > 0:
                    add_balance(uid, cost)
                await ctx.send(embed=emb("❌ No Permission", "I don't have permission to change your nickname.", C_RED))
            except discord.HTTPException as e:
                if cost > 0:
                    add_balance(uid, cost)
                await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
            return

        # ── !shop createrole ──────────────────────────────────────────────────────
        if subcommand == "createrole":
            if not _shop_cfg.get("createrole", True):
                await ctx.send(embed=emb("🛒 Disabled", "The createrole shop item is disabled in this server.", C_GREY))
                return
            if len(args) < 3:
                await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop createrole @user <name> <hex_color>` (e.g. `!shop createrole @CoolGuy MyRole ff00aa`)", C_PURPLE))
                return
            if ctx.guild is None:
                await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
                return
            # Parse target from mention or ID
            target_arg = args[0]
            target_id = None
            if target_arg.startswith("<@") and target_arg.endswith(">"):
                target_id = int(target_arg.strip("<@!>"))
            else:
                try:
                    target_id = int(target_arg)
                except ValueError:
                    pass
            if target_id is None:
                await ctx.send(embed=emb("❌ Invalid User", "First argument must be a @mention or user ID.", C_RED))
                return
            target = await fetch_member(ctx.guild, target_id)
            if target is None:
                await ctx.send(embed=emb("❌ User Not Found", "That user isn't in this server.", C_RED))
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
            if is_insured(target.id, "role"):
                await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance and can't be given new roles.", C_GOLD))
                return
            cost = 0 if uid in state.godmode_users else SHOP_ROLE_CREATE_COST
            if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLE_CREATE_COST:,}"):
                return
            try:
                new_role = await ctx.guild.create_role(name=name, color=discord.Color(color_int), hoist=True)
                await target.add_roles(new_role)
                state.bot_roles.add(new_role.id)
                save_bot_roles()
                await ctx.send(embed=emb("✅ Role Created", f"Role **{name}** created and assigned to **{target.display_name}**!", C_GREEN))
            except discord.Forbidden:
                if cost > 0:
                    add_balance(uid, cost)
                log_bot_permission_error(ctx, "manage roles")
                await ctx.send(embed=emb("❌ No Permission", "I don't have permission to manage roles.", C_RED))
            except Exception as e:
                if cost > 0:
                    add_balance(uid, cost)
                await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
            return

        # ── !shop assignrole ──────────────────────────────────────────────────────
        if subcommand == "assignrole":
            if not _shop_cfg.get("assignrole", True):
                await ctx.send(embed=emb("🛒 Disabled", "The assignrole shop item is disabled in this server.", C_GREY))
                return
            if ctx.guild is None:
                await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
                return
            if len(args) < 2 or not args[0].startswith("<@"):
                await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop assignrole @user <role name>`", C_PURPLE))
                return
            target = ctx.message.mentions[0] if ctx.message.mentions else None
            if target is None:
                await ctx.send(embed=emb("❌ Invalid User", "First argument must be a @mention.", C_RED))
                return
            name = " ".join(args[1:])
            role = discord.utils.find(lambda r: r.name.lower() == name.lower() and r.id in state.bot_roles, ctx.guild.roles)
            if role is None:
                await ctx.send(embed=emb("❌ Not Found", f"No bot-created role named **{name}** exists.", C_RED))
                return
            if role in target.roles:
                await ctx.send(embed=emb("❌ Already Has Role", f"**{target.display_name}** already has the **{role.name}** role.", C_RED))
                return
            if is_insured(target.id, "role"):
                await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance and can't be given new roles.", C_GOLD))
                return
            cost = 0 if uid in state.godmode_users else SHOP_ROLE_ASSIGN_COST
            if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLE_ASSIGN_COST:,}"):
                return
            try:
                await target.add_roles(role)
                await ctx.send(embed=emb("✅ Role Assigned", f"Role **{role.name}** assigned to **{target.display_name}**.", C_GREEN))
            except discord.Forbidden:
                if cost > 0:
                    add_balance(uid, cost)
                log_bot_permission_error(ctx, "manage roles")
                await ctx.send(embed=emb("❌ No Permission", "I don't have permission to manage roles.", C_RED))
            except Exception as e:
                if cost > 0:
                    add_balance(uid, cost)
                await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
            return

        # ── !shop removerole ──────────────────────────────────────────────────────
        if subcommand == "removerole":
            if not _shop_cfg.get("removerole", True):
                await ctx.send(embed=emb("🛒 Disabled", "The removerole shop item is disabled in this server.", C_GREY))
                return
            if ctx.guild is None:
                await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
                return
            if ctx.message.mentions and args and args[0].startswith("<@"):
                member = ctx.message.mentions[0]
                role_args = args[1:]
            else:
                member = ctx.author
                role_args = args
            if not role_args:
                # List bot-created roles the target currently has
                existing = [r for r in member.roles if r.id in state.bot_roles]
                who = "They don't" if member != ctx.author else "You don't"
                whose = f"{member.display_name}'s" if member != ctx.author else "Your"
                if not existing:
                    await ctx.send(embed=emb("🛒 Bot Roles", f"{who} have any bot-created roles to remove.", C_PURPLE))
                else:
                    lines = "\n".join(f"• **{r.name}**" for r in existing)
                    await ctx.send(embed=emb("🛒 Bot Roles", f"{whose} removable roles:\n{lines}\n\nUse `!shop removerole [@user] <name>` to remove one.", C_PURPLE))
                return
            name = " ".join(role_args)
            role = discord.utils.find(lambda r: r.name.lower() == name.lower() and r.id in state.bot_roles, member.roles)
            if role is None:
                who = member.display_name if member != ctx.author else "you"
                await ctx.send(embed=emb("❌ Not Found", f"**{who}** doesn't have a bot-created role named **{name}**.", C_RED))
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
                    add_balance(uid, cost)
                log_bot_permission_error(ctx, "remove role")
                await ctx.send(embed=emb("❌ No Permission", "I don't have permission to remove that role.", C_RED))
            except Exception as e:
                if cost > 0:
                    add_balance(uid, cost)
                await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
            return

        # ── !shop deleterole ──────────────────────────────────────────────────────
        if subcommand == "deleterole":
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
                    lines = "\n".join(f"• **{r.name}**" for r in existing)
                    await ctx.send(embed=emb("🛒 Bot Roles", f"Deletable roles:\n{lines}\n\nUse `!shop deleterole <name>` to permanently delete one.", C_PURPLE))
                return
            name = " ".join(args)
            role = resolve_role(ctx.guild, name) if len(args) == 1 else None
            if role is None:
                role = discord.utils.find(lambda r: r.name.lower() == name.lower() and r.id in state.bot_roles, ctx.guild.roles)
            elif role.id not in state.bot_roles:
                role = None
            if role is None:
                await ctx.send(embed=emb("❌ Not Found", f"No bot-created role named **{name}** exists.", C_RED))
                return
            cost = 0 if uid in state.godmode_users else SHOP_ROLE_DELETE_COST
            if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLE_DELETE_COST:,}"):
                return
            try:
                await role.delete()
                state.bot_roles.discard(role.id)
                save_bot_roles()
                await ctx.send(embed=emb("✅ Role Deleted", f"Role **{name}** has been permanently deleted.", C_GREEN))
            except discord.Forbidden:
                if cost > 0:
                    add_balance(uid, cost)
                log_bot_permission_error(ctx, "delete role")
                await ctx.send(embed=emb("❌ No Permission", "I don't have permission to delete that role.", C_RED))
            except Exception as e:
                if cost > 0:
                    add_balance(uid, cost)
                await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
            return

        # ── !shop channel ─────────────────────────────────────────────────────────
        if subcommand == "channel":
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
            # Validate channel name (Discord requirements: 2-100 chars, no spaces converted to hyphens)
            channel_name = channel_name.replace(" ", "-")[:100]
            if len(channel_name) < 2:
                await ctx.send(embed=emb("❌ Invalid Name", "Channel name must be at least 2 characters.", C_RED))
                return
            cost = 0 if uid in state.godmode_users else SHOP_CHANNEL_COST
            if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_CHANNEL_COST:,}"):
                return
            try:
                # Find or create the "bot-channels" category
                bot_category = discord.utils.find(lambda c: isinstance(c, discord.CategoryChannel) and c.name.lower() == "bot-channels", ctx.guild.channels)
                if bot_category is None:
                    bot_category = await ctx.guild.create_category("bot-channels")

                # Create channel with topic indicating it's a bot-created channel
                new_channel = await ctx.guild.create_text_channel(
                    channel_name,
                    topic=f"Created by {ctx.author.display_name}",
                    category=bot_category
                )
                # Track channel in guild settings
                cfg = get_guild_cfg(ctx.guild.id)
                if "bot_channels" not in cfg:
                    cfg["bot_channels"] = []
                cfg["bot_channels"].append(new_channel.id)
                save_guild_settings()
                await ctx.send(embed=emb("✅ Channel Created", f"Channel {new_channel.mention} created in #bot-channels!", C_GREEN))
            except discord.Forbidden:
                if cost > 0:
                    add_balance(uid, cost)
                log_bot_permission_error(ctx, "create channels")
                await ctx.send(embed=emb("❌ No Permission", "I don't have permission to create channels.", C_RED))
            except Exception as e:
                if cost > 0:
                    add_balance(uid, cost)
                await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
            return

        # ── !shop removechannel ────────────────────────────────────────────────────
        if subcommand == "removechannel":
            if not _shop_cfg.get("channel", True):
                await ctx.send(embed=emb("🛒 Disabled", "The channel shop item is disabled in this server.", C_GREY))
                return
            if ctx.guild is None:
                await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
                return
            if not args:
                # List bot-created channels
                cfg = get_guild_cfg(ctx.guild.id)
                bot_channel_ids = cfg.get("bot_channels", [])
                existing = [ch for ch in ctx.guild.channels if ch.id in bot_channel_ids]
                if not existing:
                    await ctx.send(embed=emb("🛒 Bot Channels", "No bot-created channels found in this server.", C_PURPLE))
                else:
                    lines = "\n".join(f"• {ch.mention}" for ch in existing)
                    await ctx.send(embed=emb("🛒 Bot Channels", f"Removable channels:\n{lines}\n\nUse `!shop removechannel <name>` to delete one.", C_PURPLE))
                return
            channel_name = " ".join(args).lower()
            cfg = get_guild_cfg(ctx.guild.id)
            bot_channel_ids = cfg.get("bot_channels", [])
            # Find channel by name
            channel = discord.utils.find(lambda ch: ch.name.lower() == channel_name and ch.id in bot_channel_ids, ctx.guild.channels)
            if channel is None:
                await ctx.send(embed=emb("❌ Not Found", f"No bot-created channel named **{channel_name}** exists.", C_RED))
                return
            cost = 0 if uid in state.godmode_users else SHOP_CHANNEL_COST
            if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_CHANNEL_COST:,}"):
                return
            try:
                channel_name = channel.name
                await channel.delete()
                cfg["bot_channels"].remove(channel.id)
                save_guild_settings()
                await ctx.send(embed=emb("✅ Channel Removed", f"Channel **{channel_name}** has been deleted.", C_GREEN))
            except discord.Forbidden:
                if cost > 0:
                    add_balance(uid, cost)
                log_bot_permission_error(ctx, "delete channel")
                await ctx.send(embed=emb("❌ No Permission", "I don't have permission to delete that channel.", C_RED))
            except Exception as e:
                if cost > 0:
                    add_balance(uid, cost)
                await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
            return

        # ── !shop ragebait ────────────────────────────────────────────────────────
        if subcommand == "ragebait":
            if not _shop_cfg.get("ragebait", True):
                await ctx.send(embed=emb("🛒 Disabled", "The ragebait shop item is disabled in this server.", C_GREY))
                return
            if not ctx.message.mentions:
                await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop ragebait @user [topic]`", C_PURPLE))
                return
            target = ctx.message.mentions[0]
            if is_insured(target.id, "ragebait"):
                await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance against ragebait.", C_GOLD))
                return
            topic = " ".join(a for a in args if not a.startswith("<@"))
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
                save_ragebait()
            except Exception as e:
                if cost > 0:
                    add_balance(uid, cost)
                await placeholder.edit(content=f"⚠️ {e}")
            finally:
                typing_task.cancel()
            return

        # ── !shop mock ────────────────────────────────────────────────────────────
        if subcommand == "mock":
            if not ctx.message.mentions:
                await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop mock @user`", C_PURPLE))
                return
            target = ctx.message.mentions[0]
            cost = 0 if uid in state.godmode_users else SHOP_MOCK_COST
            if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_MOCK_COST:,}"):
                return
            state.active_mocks[target.id] = {"remaining": SHOP_MOCK_MESSAGES, "started_by": uid, "channel_id": ctx.channel.id}
            save_mock()
            await ctx.send(embed=emb(
                "🎭 Mock Activated",
                f"**{target.display_name}** will have their next {SHOP_MOCK_MESSAGES} messages mocked!",
                C_PURPLE,
            ))
            return

        # ── !shop insurance ─────────────────────────────────────────────────────────────
        if subcommand == "insurance":
            key = str(uid)
            cost = 0 if uid in state.godmode_users else SHOP_INSURANCE_COST
            if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_INSURANCE_COST:,}"):
                return
            expires_at = int(time.time() + SHOP_INSURANCE_DURATION_SECS)
            state.insurance[key] = {
                "expires_at": expires_at,
                "protected_from": ["ragebait", "mock", "nickname", "role"],
            }
            save_insurance()
            await ctx.send(embed=emb(
                "🛡️ Insurance Purchased",
                f"Protected against ragebait, mock, nickname, and role changes! (expires <t:{expires_at}:R>)",
                C_GREEN,
            ))
            return

        # ── !shop rolecolor ───────────────────────────────────────────────────────
        if subcommand == "rolecolor":
            if len(args) < 2:
                await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop rolecolor <role name> <color>`", C_PURPLE))
                return

            if ctx.guild is None:
                await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
                return

            # Last token is the color; everything before is the role name
            color_str = args[-1]
            role_token = " ".join(args[:-1])
            role = resolve_role(ctx.guild, role_token) if len(args) == 2 else None
            if role is None:
                role = discord.utils.find(lambda r: r.name.lower() == role_token.lower(), ctx.guild.roles)
            if role is None:
                await ctx.send(embed=emb("❌ Not Found", f"No role named **{role_token}** exists.", C_RED))
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
            return

        # ── !shop mute ────────────────────────────────────────────────────────────
        if subcommand == "mute":
            if not ctx.message.mentions:
                await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop mute @user`", C_PURPLE))
                return
            target = ctx.message.mentions[0]

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

                # Mute for SHOP_MUTE_MINUTES minutes
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
            return

        # ── !shop simp / concubine ────────────────────────────────────────────────
        if subcommand in ("simp", "concubine"):
            if not ctx.message.mentions:
                await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop simp @user`", C_PURPLE))
                return
            target = ctx.message.mentions[0]

            if target.id == uid:
                await ctx.send(embed=emb("❌ Self Simp", "You can't simp for yourself!", C_RED))
                return

            cost = 0 if uid in state.godmode_users else SHOP_SIMP_COST
            if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_SIMP_COST:,}"):
                return

            tax_type = "concubine" if subcommand == "concubine" else "simp"
            simp_data = {"master": uid, "type": tax_type, "channel_id": ctx.channel.id}
            # Add timestamp for concubine to expire after 24h
            if tax_type == "concubine":
                simp_data["activated_at"] = time.time()
            state.active_simps[target.id] = simp_data
            save_simp(state.active_simps)

            title = "🍆 Concubine Tax Activated" if tax_type == "concubine" else "🍆 Simp Tax Activated"
            await ctx.send(embed=emb(
                title,
                f"**{target.display_name}** now owes **{ctx.author.display_name}** **{SHOP_SIMP_TAX_PER_MESSAGE} 🪙** per message!",
                C_PURPLE,
            ))
            return

        # ── !shop curse ───────────────────────────────────────────────────────────
        if subcommand == "curse":
            if not ctx.message.mentions:
                await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop curse @user`", C_PURPLE))
                return
            target = ctx.message.mentions[0]

            if target.id == uid:
                await ctx.send(embed=emb("❌ Self Curse", "You can't curse yourself!", C_RED))
                return

            cost = 0 if uid in state.godmode_users else SHOP_CURSE_COST
            if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_CURSE_COST:,}"):
                return

            state.active_curses[target.id] = {"cursed_by": uid, "remaining": SHOP_CURSE_MESSAGES}
            save_curse(state.active_curses)

            await ctx.send(embed=emb(
                "🔮 Curse Activated",
                f"**{target.display_name}** is now cursed for the next **{SHOP_CURSE_MESSAGES}** messages!",
                C_PURPLE,
            ))
            return

        # ── !shop roleup / roledown ───────────────────────────────────────────────
        if subcommand in ("roleup", "roledown"):
            direction = subcommand  # "roleup" or "roledown"
            cfg_key = "roleup" if direction == "roleup" else "roledown"
            if not _shop_cfg.get(cfg_key, True):
                await ctx.send(embed=emb("🛒 Disabled", f"The {direction} shop item is disabled in this server.", C_GREY))
                return
            if ctx.guild is None:
                await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
                return
            if not args:
                await ctx.send(embed=emb("🛒 Shop", f"Usage: `!shop {direction} <role name>`", C_PURPLE))
                return
            name = " ".join(args)
            role = resolve_role(ctx.guild, name) if len(args) == 1 else None
            if role is None:
                role = discord.utils.find(lambda r: r.name.lower() == name.lower() and r.id in state.bot_roles, ctx.guild.roles)
            elif role.id not in state.bot_roles:
                role = None
            if role is None:
                await ctx.send(embed=emb("❌ Not Found", f"No bot-created role named **{name}** exists.", C_RED))
                return
            # Sorted ascending by position (lowest = index 0)
            other_bot_roles = sorted(
                (r for r in ctx.guild.roles if r.id in state.bot_roles and r.id != role.id),
                key=lambda r: r.position
            )
            if direction == "roleup" and not any(r.position > role.position for r in other_bot_roles):
                await ctx.send(embed=emb("❌ Already Highest", f"**{role.name}** is already the highest bot-created role.", C_RED))
                return
            if direction == "roledown" and not any(r.position < role.position for r in other_bot_roles):
                await ctx.send(embed=emb("❌ Already Lowest", f"**{role.name}** is already the lowest bot-created role.", C_RED))
                return
            cost = 0 if uid in state.godmode_users else SHOP_ROLE_MOVE_COST
            if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLE_MOVE_COST:,}"):
                return
            # Move to just above the next bot role up, or just below the next bot role down
            if direction == "roleup":
                next_role = min((r for r in other_bot_roles if r.position > role.position), key=lambda r: r.position)
                new_pos = next_role.position + 1
            else:
                next_role = max((r for r in other_bot_roles if r.position < role.position), key=lambda r: r.position)
                new_pos = next_role.position - 1
            new_pos = max(1, min(new_pos, ctx.guild.me.top_role.position - 1))
            try:
                await role.edit(position=new_pos)
                label = "up" if direction == "roleup" else "down"
                # Re-fetch positions after the move since Discord shifts surrounding roles
                updated_bot_roles = sorted(
                    (r for r in ctx.guild.roles if r.id in state.bot_roles),
                    key=lambda r: r.position, reverse=True
                )
                total_bot_roles = len(updated_bot_roles)
                rank = next((i + 1 for i, r in enumerate(updated_bot_roles) if r.id == role.id), total_bot_roles)
                await ctx.send(embed=emb("✅ Role Moved", f"Role **{role.name}** moved {label} — now **#{rank}** of {total_bot_roles}.", C_GREEN))
            except discord.Forbidden:
                if cost > 0:
                    add_balance(uid, cost)
                log_bot_permission_error(ctx, "manage roles")
                await ctx.send(embed=emb("❌ No Permission", "I don't have permission to manage roles.", C_RED))
            except Exception as e:
                if cost > 0:
                    add_balance(uid, cost)
                await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
            return

        await ctx.send(embed=emb("🛒 Unknown Item", "Try `!shop` to see what's available.", C_PURPLE))

    @commands.command(name="roles", aliases=["rolelb", "lbroles", "lbr"])
    async def cmd_roles(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        # Get all bot-created roles present in this guild, sorted highest to lowest
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
        await ctx.send(embed=emb("👑 Role Leaderboard", "\n".join(lines), C_PURPLE))


async def setup(bot):
    await bot.add_cog(ShopCog(bot))
