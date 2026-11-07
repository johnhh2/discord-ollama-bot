import asyncio
import functools
import time
import re

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_PURPLE, C_GREY,
    send_ephemeral,
    shop_charge, log_bot_permission_error, MemberConverter, resolve_member,
    announce_record,
)

from src.economy import (
    add_balance, get_balance, is_insured, get_insurance_expiry,
    extend_insurance, next_daily_reset_ts, rebase_savings_deposits,
    get_insurance_tier, set_insurance_tier, insurance_switch_cost,
    insurance_tier_cost, insurance_tier_info, insurance_policies,
    _ensure_user, _ct_today,
)
from src.leveling import (
    _ensure_lvl_record, _xp_cost, level_from_xp, display_level, record_levelup,
    xp_for_level, best_level_elsewhere,
)
from src.permissions import (
    _wrong_channel_reply, command_permitted, is_silenced,
)
from src.persistence import (
    save_insurance, save_insurance_subs, save_guild_settings,
    save_bot_roles,
    save_ragebait, save_mock, save_tax, save_curse,
    save_user_artifact, delete_user_artifact, save_economy, try_set_record, save_leveling,
)
from src.artifacts import ARTIFACTS, RETIRED_ARTIFACTS, ALL_ARTIFACTS, owned_qty, owned_artifact_count
from src.confirm_view import confirm_purchase, confirm_choice, confirm_prompt
from src.settings_views import pick_from_list, pick_from_menus
from src.guild_config import get_guild_cfg
from src.features import feature_enabled, disabled_feature_for
from src.shop_hub import open_shop_hub
from src.rankings_view import send_rankings
from src.ai import (
    keep_typing,
    stream_ollama, finalize,
)
from src.config import (
    SHOP_NICKNAME_SELF_COST, SHOP_NICKNAME_REMOVE_COST, SHOP_NICKNAME_OTHER_COST,
    SHOP_ROLE_CREATE_COST, SHOP_ROLE_ASSIGN_COST, SHOP_ROLE_REMOVE_COST, SHOP_ROLE_DELETE_COST, SHOP_ROLE_MOVE_COST,
    SHOP_ROLECOLOR_COST, SHOP_ROLECHANNEL_COST, SHOP_LOCK_COST, SHOP_RENAME_COST, SHOP_CHANNEL_COST, SHOP_CHANNEL_DELETE_COST, SHOP_TAX_COST,
    SHOP_INSURANCE_TIERS, INSURANCE_TYPES, PROPERTY_INSURANCE_LIVE,
    SHOP_MOCK_COST, SHOP_RAGEBAIT_COST, SHOP_CURSE_COST, SHOP_UNOREVERSE_COST,
    SHOP_INSURANCE_DURATION_SECS, SHOP_MOCK_MESSAGES, SHOP_RAGEBAIT_MESSAGES,
    SHOP_CURSE_MESSAGES, SHOP_TAX_PER_MESSAGE,
    SHOP_TAX_DURATION_SECS,
    SHOP_XP_COST_PER_XP, SHOP_XP_CATCHUP_DISCOUNT_PCT, SHOP_XP_CATCHUP_COST_PER_XP,
    BOUNTY_MIN_AMOUNT, SHOP_ANNOUNCE_COST_PER_CHAR, SHOP_ANNOUNCE_MAX_COST, ANNOUNCE_MAX_TOTAL_CHARS, WYOMING_TTS_HOST,
)
from src import state


# ── Per-subcommand boilerplate ────────────────────────────────────────────────
#
# Every !shop subcommand opens with the same two checks: (1) refuse to run in
# the configured lottery channel, and (2) if shop_items[<key>] is False in
# guild settings, send the "🛒 Disabled" embed and return. _shop_subcommand
# absorbs both so the bodies below can focus on the actual logic.
#
# enabled_key:
#   - a string ("nickname", "rolecolor", …) → check shop_items[key] in cfg
#   - "dynamic" → key is ctx.invoked_with (used by roleup/roledown)
#   - None → no enabled-check; only the lottery-channel guard runs

def _shop_subcommand(enabled_key: "str | None"):
    def deco(func):
        # wraps() is load-bearing: discord.py reads __qualname__ (is_inside_class) to
        # decide whether to skip `self`. With the wrapper's local qualname it skips
        # only one param and parses `ctx` as a user arg — MissingRequiredArgument /
        # BadArgument("Converting to Context failed") on every wrapped subcommand.
        @functools.wraps(func)
        async def wrapper(self, ctx: commands.Context, *args, **kwargs):
            if ctx.guild:
                cfg = get_guild_cfg(ctx.guild.id)
                lottery_channel_id = cfg.get("lottery_channel")
                if lottery_channel_id and ctx.channel.id == lottery_channel_id:
                    await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
                    return
            if enabled_key is not None:
                key = ctx.invoked_with if enabled_key == "dynamic" else enabled_key
                shop_items = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}
                if not shop_items.get(key, True):
                    await ctx.send(embed=emb("🛒 Disabled", f"The {key} shop item is disabled in this server.", C_GREY))
                    return
            return await func(self, ctx, *args, **kwargs)
        return wrapper
    return deco


# Top-level commands that mirror a !shop subcommand (same args, same body) —
# one table keeps the pair in lockstep without 30 hand-written wrappers.
# Each entry: (canonical top-level name, handler attr, [legacy aliases]).
# The canonical name mirrors the !shop subcommand's name=; legacy aliases keep
# the pre-rename verb-first names (createrole, createchannel, …) working as
# both top-level commands and !shop subcommand aliases.
_SHOP_TOP_ALIASES: list[tuple[str, str, list[str]]] = [
    ("nickname",       "shop_nickname",      []),
    ("removenickname", "shop_removenickname", []),
    ("rolecreate",     "shop_createrole",    ["createrole"]),
    ("roleassign",     "shop_assignrole",    ["assignrole"]),
    ("roleunassign",   "shop_unassignrole",  ["unassignrole"]),
    ("roledelete",     "shop_deleterole",    ["deleterole"]),
    ("channelcreate",  "shop_createchannel", ["createchannel"]),
    ("channeldelete",  "shop_deletechannel", ["deletechannel"]),
    ("channelrename",  "shop_renamechannel", ["renamechannel"]),
    ("rolerename",     "shop_renamerole",    ["renamerole"]),
    ("rolechannel",    "shop_rolechannel",   []),
    ("channellock",    "shop_lockchannel",   ["lockchannel"]),
    ("channelunlock",  "shop_unlockchannel", ["unlockchannel"]),
    ("rolelock",       "shop_lockrole",      ["lockrole"]),
    ("roleunlock",     "shop_unlockrole",    ["unlockrole"]),
    ("ragebait",       "shop_ragebait",      []),
    ("mock",           "shop_mock",          []),
    ("insurance",      "shop_insurance",     []),
    ("rolecolor",      "shop_rolecolor",     []),
    ("tax",            "shop_tax",           []),
    ("curse",          "shop_curse",         []),
    ("unoreverse",     "shop_unoreverse",    []),
    ("buyxp",          "shop_buyxp",         []),
    ("artifacts",      "shop_artifacts",     ["artifact"]),
    ("announce",       "shop_announce",      []),
    # roleup and roledown both dispatch via shop_roleup (it reads
    # ctx.invoked_with to pick direction).
    ("roleup",         "shop_roleup",        []),
    ("roledown",       "shop_roleup",        []),
]


# ── Once-a-day targeted effects ──────────────────────────────────────────────
# Ragebait, mock, curse and tax make the bot post on every message the target
# sends, so one buyer covering the whole server floods every channel. Each is
# limited to one buy per buyer per gameplay day.

def _once_a_day_embed(item: str) -> discord.Embed:
    return emb("⏳ Once a Day", f"You can buy one {item} a day. Your next one unlocks <t:{next_daily_reset_ts()}:R>.", C_GREY)


def _targeted_buy_used(uid: int, item: str) -> bool:
    days = state.economy["users"].get(str(uid), {}).get("targeted_buy_days") or {}
    return days.get(item) == _ct_today()


async def _claim_targeted_buy(ctx: commands.Context, item: str) -> "tuple[bool, str | None]":
    """Claim today's buy of *item* before the charge (see CLAUDE.md: Concurrency).

    Returns (claimed, prior day); hand the prior day to _release_targeted_buy on failure.
    """
    uid = ctx.author.id
    await _ensure_user(uid)
    days = state.economy["users"][str(uid)].setdefault("targeted_buy_days", {})
    prior = days.get(item)
    if prior == _ct_today():
        await ctx.send(embed=_once_a_day_embed(item))
        return False, prior
    days[item] = _ct_today()
    return True, prior


async def _release_targeted_buy(uid: int, item: str, prior: "str | None"):
    days = state.economy["users"][str(uid)].setdefault("targeted_buy_days", {})
    if prior is None:
        days.pop(item, None)
    else:
        days[item] = prior
    await save_economy(uid)


def _artifact_sell_price(art: dict) -> int:
    """Half the catalog price, rounded down."""
    return art["cost"] // 2


def _artifact_by_id(artifact_id: str) -> dict:
    return next(a for a in ALL_ARTIFACTS if a["id"] == artifact_id)


def _artifact_options(uid: int, lvl: int, action: str) -> list[tuple[str, str]]:
    """Dropdown `(label, id)` pairs: the unowned artifacts `lvl` unlocks for
    `buy`, the owned ones with their half-price payout for `sell`."""
    options = []
    for i, art in enumerate(ARTIFACTS, start=1):
        owned = owned_qty(uid, art["id"])
        if action == "sell":
            if owned > 0:
                options.append((f"{i}. {art['effect'][:70]} — +{_artifact_sell_price(art):,} 🪙", art["id"]))
        elif owned < art["max"] and lvl >= art.get("level", 1):
            options.append((f"{i}. {art['effect'][:70]} — {art['cost']:,} 🪙", art["id"]))
    if action == "sell":
        for art in RETIRED_ARTIFACTS:
            if owned_qty(uid, art["id"]) > 0:
                options.append((f"Retired: {art['effect'][:62]} — +{_artifact_sell_price(art):,} 🪙", art["id"]))
    return options


class ShopCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # (guild_id, user_id) pairs with an XP purchase confirm in flight —
        # the confirm prompt is a long await, so gate re-entry explicitly.
        self._buyxp_active: set = set()
        # Register the top-level aliases. Tests instantiate ShopCog(bot=None)
        # to call handlers directly — nothing to attach to then.
        if bot is not None:
            for top_name, sub_attr, legacy_aliases in _SHOP_TOP_ALIASES:
                sub_cmd = getattr(self, sub_attr)
                # cog=self is load-bearing: the callback's __qualname__ is
                # ShopCog.shop_X (kept by wraps() in _shop_subcommand), so
                # discord.py's _parse_arguments builds ctx.args as
                # [cog, ctx, ...]. Without a cog it builds [ctx, ...]: `self`
                # binds to the Context, `ctx` to the first user arg, and the
                # first `.guild` access dies with 'str' has no attribute 'guild'.
                alias_cmd = commands.Command(sub_cmd.callback, name=top_name, aliases=legacy_aliases,
                                             help=sub_cmd.help, usage=sub_cmd.usage)
                alias_cmd.cog = self
                bot.add_command(alias_cmd)

    def cog_unload(self):
        if self.bot is None:
            return
        for top_name, _, _ in _SHOP_TOP_ALIASES:
            self.bot.remove_command(top_name)

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

    @staticmethod
    def _is_bot_channel(guild: discord.Guild, channel) -> bool:
        """True if the shop created this channel (guild cfg `bot_channels`).

        Every shop command that edits a channel — rename, restrict, lock,
        delete — gates on this. Roles gate on `state.bot_roles` the same way.
        Without the gate, coins bought edits to #general."""
        return channel.id in get_guild_cfg(guild.id).get("bot_channels", [])

    @staticmethod
    async def _rollback_created_role(guild: discord.Guild, role: discord.Role) -> bool:
        """Delete a role !shop rolecreate just made but couldn't assign, and
        drop it from the tracked set. Returns False if Discord refused the
        delete — the role then stays tracked so !shop roledelete can take it."""
        try:
            await role.delete(reason="shop: rolecreate could not assign the role")
        except Exception:
            return False
        state.bot_roles.discard(role.id)
        state.bot_role_ranks.pop((guild.id, role.id), None)
        try:
            await save_bot_roles()
        except Exception:
            pass  # best-effort: the caller still has to refund
        return True

    @staticmethod
    async def _restore_overwrites(channel: discord.TextChannel, applied: list[tuple]) -> None:
        """Undo the permission overwrites a failed !shop rolechannel already
        applied. `applied` is [(target, overwrite_before), …] in the order they
        were set. Best-effort: an empty prior overwrite means there was none,
        so the row is deleted rather than written back empty."""
        for who, before in reversed(applied):
            try:
                await channel.set_permissions(who, overwrite=None if before.is_empty() else before)
            except Exception:
                pass

    def _role_section_lines(self, shop_items: dict, uid: int, gid: int) -> list[str]:
        """Cost-sorted, level-aware, shop_items-gated lines for the Roles menu.

        Shared by the main !shop pointer (presence check only) and the
        dedicated !shop roles sub-help menu. Returns [] when every role item
        is disabled.
        """
        from src.level_unlocks import fmt_line
        def L(cmd, text):
            return fmt_line(cmd, text, uid, gid)
        items = []
        if shop_items.get("unassignrole", True):
            items.append((SHOP_ROLE_REMOVE_COST, L("roleunassign", f"`!shop roleunassign [@user] <name>` — Remove a bot-created role from yourself or another user — **{SHOP_ROLE_REMOVE_COST:,} 🪙**")))
        if shop_items.get("deleterole", True):
            items.append((SHOP_ROLE_DELETE_COST, L("roledelete", f"`!shop roledelete <name>` — Permanently delete a bot-created role — **{SHOP_ROLE_DELETE_COST:,} 🪙**")))
        if shop_items.get("createrole", True):
            items.append((SHOP_ROLE_CREATE_COST, L("rolecreate", f"`!shop rolecreate @user <name>` — Create a role for a user — **{SHOP_ROLE_CREATE_COST:,} 🪙**")))
        if shop_items.get("assignrole", True):
            items.append((SHOP_ROLE_ASSIGN_COST, L("roleassign", f"`!shop roleassign @user <name>` — Assign an existing bot-created role to a user — **{SHOP_ROLE_ASSIGN_COST:,} 🪙**")))
        if shop_items.get("roleup", True):
            items.append((SHOP_ROLE_MOVE_COST, L("roleup", f"`!shop roleup <role name>` — Move a bot-created role up one position — **{SHOP_ROLE_MOVE_COST:,} 🪙**")))
        if shop_items.get("roledown", True):
            items.append((SHOP_ROLE_MOVE_COST, L("roledown", f"`!shop roledown <role name>` — Move a bot-created role down one position — **{SHOP_ROLE_MOVE_COST:,} 🪙**")))
        if shop_items.get("rolecolor", True):
            items.append((SHOP_ROLECOLOR_COST, L("rolecolor", f"`!shop rolecolor @role <color>` — Change a bot-created role's color — **{SHOP_ROLECOLOR_COST:,} 🪙**")))
        if shop_items.get("renamerole", True):
            items.append((SHOP_RENAME_COST, L("rolerename", f"`!shop rolerename @role | <new name>` — Rename a bot-created role — **{SHOP_RENAME_COST:,} 🪙**")))
        if shop_items.get("lockrole", True):
            items.append((SHOP_LOCK_COST, L("rolelock", f"`!shop rolelock <role name>` — Lock a role against changes — **{SHOP_LOCK_COST:,} 🪙**")))
        if not items:
            return []
        items.sort(key=lambda x: x[0])
        lines = [item[1] for item in items]
        if shop_items.get("lockrole", True):
            lines.append(L("roleunlock", "`!shop roleunlock <role name>` — Unlock a role (lock owner only)"))
        return lines

    def _channel_section_lines(self, shop_items: dict, uid: int, gid: int) -> list[str]:
        """Cost-sorted, level-aware, shop_items-gated lines for the Channels menu.

        Shared by the main !shop pointer (presence check only) and the
        dedicated !shop channels sub-help menu. Returns [] when every channel
        item is disabled.
        """
        from src.level_unlocks import fmt_line
        def L(cmd, text):
            return fmt_line(cmd, text, uid, gid)
        items = []
        if shop_items.get("channel", True):
            items.append((SHOP_CHANNEL_COST, L("channelcreate", f"`!shop channelcreate <name>` — Create a new text channel — **{SHOP_CHANNEL_COST:,} 🪙**")))
            items.append((SHOP_CHANNEL_DELETE_COST, L("channeldelete", f"`!shop channeldelete <name>` — Delete a bot-created channel — **{SHOP_CHANNEL_DELETE_COST:,} 🪙**")))
        if shop_items.get("renamechannel", True):
            items.append((SHOP_RENAME_COST, L("channelrename", f"`!shop channelrename <channel> <new name>` — Rename a bot-created channel — **{SHOP_RENAME_COST:,} 🪙**")))
        if shop_items.get("lockchannel", True):
            items.append((SHOP_LOCK_COST, L("channellock", f"`!shop channellock #channel` — Lock a bot-created channel against changes — **{SHOP_LOCK_COST:,} 🪙**")))
        if shop_items.get("rolechannel", True):
            items.append((SHOP_ROLECHANNEL_COST, L("rolechannel", f"`!shop rolechannel @role #channel` — Restrict a bot-created channel to a role — **{SHOP_ROLECHANNEL_COST:,} 🪙**")))
        if not items:
            return []
        items.sort(key=lambda x: x[0])
        lines = [item[1] for item in items]
        if shop_items.get("lockchannel", True):
            lines.append(L("channelunlock", "`!shop channelunlock #channel` — Unlock a channel (lock owner only)"))
        return lines

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Route !<alias> @user to shop_tax when the alias is a guild-configured tax alias."""
        if not isinstance(error, commands.CommandNotFound):
            return
        if not ctx.guild or not feature_enabled(ctx.guild.id, "shop"):
            return
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

    # `store` is the idle RPG's market (`!idle shop` bare), not an alias here.
    @commands.group(name="shop", invoke_without_command=True,
                    help="Open the shop panel; pick a section, then an item")
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

        embed = self._overview_embed(ctx)
        if embed is None:
            await send_ephemeral(ctx, embed=emb("🛒 Shop", "No shop items are currently available.", C_PURPLE))
            return
        if ctx.guild is None:  # the panel's pickers need a server
            await send_ephemeral(ctx, embed=embed)
            return
        await open_shop_hub(ctx, self, overview=self._overview_embed)

    @app_commands.command(name="shop", description="Browse the shop — only you see the panel")
    @app_commands.guild_only()
    async def slash_shop(self, interaction: discord.Interaction):
        """The same panel, ephemeral. A slash command skips `process_commands`,
        so the gates a typed `!shop` meets are applied here: the blocklist,
        the shop feature switch and the lottery-channel rule. Purchases made
        from it post their confirm prompt and result publicly, as typed
        ones do."""
        if is_silenced(interaction.user.id, interaction.guild_id):
            return
        ctx = await self.bot.get_context(interaction)
        ctx.command = self.cmd_shop
        gid = interaction.guild_id
        feature = disabled_feature_for("shop", gid)
        if feature is not None:
            await interaction.response.send_message(embed=emb(
                "🚫 Turned Off", f"The shop needs **{feature.label}**, which is off in this server.", C_GREY,
            ), ephemeral=True)
            return
        if get_guild_cfg(gid).get("lottery_channel") == interaction.channel_id:
            await interaction.response.send_message("Shop commands are not allowed in the lottery channel.", ephemeral=True)
            return
        embed = self._overview_embed(ctx)
        if embed is None:
            await interaction.response.send_message(embed=emb("🛒 Shop", "No shop items are currently available.", C_PURPLE), ephemeral=True)
            return
        await open_shop_hub(ctx, self, overview=self._overview_embed, ephemeral=True)

    async def forward(self, ctx, method: str, *args, invoked_with: "str | None" = None, **kwargs) -> "str | None":
        """Run a shop command in its typed form for the panel. `method` is a
        ShopCog attribute, or `bot:<qualified name>` for another cog's
        command (`assets buy`, `bounties`). The gates `process_commands`
        would have applied — permission tier, feature switch, level lock —
        are `bot.check`s a direct call skips, so they run here; a refusal
        is returned as text for the panel to show privately, None means the
        command ran. `ctx.invoked_with` is set because roleup/roledown and
        the tax aliases read it."""
        if method.startswith("bot:"):
            command = self.bot.get_command(method[4:]) if self.bot is not None else None
        else:
            command = getattr(self, method, None)
        if command is None:
            return "That item isn't available right now."
        ctx.command = command
        ctx.invoked_with = invoked_with or command.name
        if not command_permitted(ctx):
            return "❌ You can't use that command."
        gid = ctx.guild.id if ctx.guild else None
        feature = disabled_feature_for(command.qualified_name, gid)
        if feature is not None:
            return f"🚫 That needs **{feature.label}**, which is off in this server."
        from src.level_unlocks import is_locked_for
        required = is_locked_for(ctx.invoked_with, ctx.author.id, gid) if gid else None
        if required is not None:
            return f"🔒 That unlocks at **level {required}** in this server."
        await command.callback(command.cog or self, ctx, *args, **kwargs)
        return None

    def _overview_embed(self, ctx: commands.Context) -> "discord.Embed | None":
        """The `!shop` overview — every section with its typed forms. None
        when nothing is for sale."""
        cfg = get_guild_cfg(ctx.guild.id) if ctx.guild else {}
        _si = cfg.get("shop_items", {})
        _gid = ctx.guild.id if ctx.guild else 0
        _uid = ctx.author.id
        sections = {}

        from src.level_unlocks import fmt_line

        def L(cmd, text):  # shorthand for level-aware line
            return fmt_line(cmd, text, _uid, _gid)

        # Nicknames (sorted by cost)
        if _si.get("nickname", True):
            nickname_items = [
                (SHOP_NICKNAME_SELF_COST,   L("nickname", f"`!shop nickname <new_name>` — Change your own nickname — **{SHOP_NICKNAME_SELF_COST:,} 🪙**")),
                (SHOP_NICKNAME_REMOVE_COST, L("removenickname", f"`!shop removenickname` — Remove your own nickname — **{SHOP_NICKNAME_REMOVE_COST:,} 🪙**")),
                (SHOP_NICKNAME_OTHER_COST,  L("nickname", f"`!shop nickname @user <new_name>` — Nickname user — **{SHOP_NICKNAME_OTHER_COST:,} 🪙**")),
            ]
            nickname_items.sort(key=lambda x: x[0])
            sections["🎭 Nicknames"] = [item[1] for item in nickname_items]

        # Roles and Channels each get a dedicated sub-help menu (!shop roles /
        # !shop channels) instead of a wall of commands here. Show a one-line
        # pointer to each when any of its items is enabled.
        if self._role_section_lines(_si, _uid, _gid):
            sections["👑 Roles"] = ["`!shop roles` — Role commands (create, assign, rename, color, lock, rank, …)"]
        if self._channel_section_lines(_si, _uid, _gid):
            sections["📢 Channels"] = ["`!shop channels` — Channel commands (create, delete, rename, restrict, lock, …)"]

        # Fun & Social (sorted by cost)
        fun_items = [
            (min(t["cost"] for t in SHOP_INSURANCE_TIERS.values()),
             f"`!shop insurance` — Personal insurance refunds 50–100% of crime losses; social insurance blocks mock/tax/nickname/role effects. Every server — **from {min(t['cost'] for t in SHOP_INSURANCE_TIERS.values()):,} 🪙/day**"),
            (SHOP_TAX_COST,      f"`!shop tax @user` — Apply a per-message tax to a user for 24h, once a day — **{SHOP_TAX_COST:,} 🪙**"),
            (SHOP_MOCK_COST,      f"`!shop mock @user` — Mock someone's next {SHOP_MOCK_MESSAGES} messages, once a day — **{SHOP_MOCK_COST:,} 🪙**"),
        ]
        if _si.get("ragebait", True):
            fun_items.append((SHOP_RAGEBAIT_COST, f"`!shop ragebait @user [topic]` — Ragebait for {SHOP_RAGEBAIT_MESSAGES + 1} messages, once a day — **{SHOP_RAGEBAIT_COST:,} 🪙**"))
        fun_items.append((SHOP_CURSE_COST, f"`!shop curse @user` — Curse someone's messages for {SHOP_CURSE_MESSAGES} messages, once a day — **{SHOP_CURSE_COST:,} 🪙**"))
        fun_items.append((SHOP_UNOREVERSE_COST, L("unoreverse", f"`!shop unoreverse @user` — Redirect active mock/ragebait/curse onto someone else — **{SHOP_UNOREVERSE_COST:,} 🪙**")))
        if WYOMING_TTS_HOST and _si.get("announce", True):  # off without a TTS server, like the command
            fun_items.append((SHOP_ANNOUNCE_COST_PER_CHAR,
                              f"`!shop announce \"<message>\" <voice-channel> [x<N>]` — Read your message aloud in a voice channel "
                              f"— **{SHOP_ANNOUNCE_COST_PER_CHAR} 🪙/character**, {SHOP_ANNOUNCE_MAX_COST:,} 🪙 at most "
                              f"(up to {ANNOUNCE_MAX_TOTAL_CHARS:,} characters, repeats included)"))
        fun_items.sort(key=lambda x: x[0])
        sections["🎉 Fun & Social"] = [item[1] for item in fun_items]

        # Leveling — variable price, quoted (and confirmed) at purchase time.
        if _si.get("buyxp", True):
            sections["✨ Leveling"] = [
                f"`!shop buyxp [lvl <x>]` — Buy your next level's worth of XP, or every level up to lvl x — **{SHOP_XP_COST_PER_XP} 🪙/XP** "
                f"(price scales with level; 🏷️ **{SHOP_XP_CATCHUP_DISCOUNT_PCT}% off** levels below your best in another server)"
            ]

        # Artifacts — permanent per-user upgrades, listed in their own menu.
        if feature_enabled(_gid, "artifacts"):
            sections["🏺 Artifacts"] = [
                "`!artifacts` — Permanent artifacts with passive effects"
            ]

        # Bounties — only surfaced where the feature is enabled (a bounty
        # channel is configured). The reward is escrowed from the poster, so
        # there's no fixed price to list; show the minimum and the channel.
        bounty_channel_id = cfg.get("bounty_channel") if ctx.guild else None
        if bounty_channel_id:
            sections["🎯 Bounties"] = [
                f"`!bounty <coins> [duration] <condition>` — Post an escrowed reward "
                f"another user can claim in <#{bounty_channel_id}> — **{BOUNTY_MIN_AMOUNT:,} 🪙 min**",
                "`!bounties` — List the server's open bounties and their rewards",
            ]

        if feature_enabled(_gid, "assets"):
            sections["🏘️ Assets"] = ["`!assets` — Real-estate deeds that pay daily rent"]

        if not sections:
            return None

        desc = "\n\n".join(f"**{section}**\n" + "\n".join(items) for section, items in sections.items())
        return emb("🛒 Shop", desc, C_PURPLE)

    # ── !shop roles ───────────────────────────────────────────────────────────
    @cmd_shop.command(name="roles", aliases=["role"],
                      help="Open the shop panel on the roles page (create, assign, rename, color, lock, rank)")
    @_shop_subcommand(None)
    async def shop_roles(self, ctx: commands.Context):
        _si = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}
        _gid = ctx.guild.id if ctx.guild else 0
        lines = self._role_section_lines(_si, ctx.author.id, _gid)
        if not lines:
            await send_ephemeral(ctx, embed=emb("👑 Role Shop", "No role shop items are currently available.", C_PURPLE))
            return
        if ctx.guild is None:
            await send_ephemeral(ctx, embed=emb("👑 Role Shop", "\n".join(lines), C_PURPLE))
            return
        await open_shop_hub(ctx, self, page="roles", overview=self._overview_embed)

    # ── !shop channels ────────────────────────────────────────────────────────
    @cmd_shop.command(name="channels", aliases=["channel"],
                      help="Open the shop panel on the channels page (create, delete, rename, restrict, lock)")
    @_shop_subcommand(None)
    async def shop_channels(self, ctx: commands.Context):
        _si = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}
        _gid = ctx.guild.id if ctx.guild else 0
        lines = self._channel_section_lines(_si, ctx.author.id, _gid)
        if not lines:
            await send_ephemeral(ctx, embed=emb("📢 Channel Shop", "No channel shop items are currently available.", C_PURPLE))
            return
        if ctx.guild is None:
            await send_ephemeral(ctx, embed=emb("📢 Channel Shop", "\n".join(lines), C_PURPLE))
            return
        await open_shop_hub(ctx, self, page="channels", overview=self._overview_embed)

    # ── !shop assets ──────────────────────────────────────────────────────────
    @cmd_shop.command(name="assets", aliases=["asset", "properties", "property"],
                      help="Browse the real-estate deeds that pay daily rent, or run an assets subcommand from the shop",
                      usage="[browse|buy|sell|upgrade|unlist|rename] [args]")
    @_shop_subcommand(None)
    async def shop_assets(self, ctx: commands.Context, sub: str = None, *args):
        """Bare (or an unknown word), the `!assets` menu itself;
        `!shop assets <browse|buy|sell|upgrade|unlist|rename> …` forwards to
        the matching `!assets` subcommand."""
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return
        assets = self.bot.get_command("assets") if self.bot is not None else None
        target = assets.get_command(sub.lower()) if assets is not None and sub is not None else None
        if target is None:
            refusal = await self.forward(ctx, "bot:assets")
            if refusal:
                await ctx.send(embed=emb("🏘️ Real Estate", refusal, C_GREY))
            return
        # `buy`, `upgrade` and `unlist` take their name keyword-only.
        if target.name in ("buy", "upgrade", "unlist"):
            refusal = await self.forward(ctx, f"bot:assets {target.name}", name=" ".join(args) or None)
        else:
            refusal = await self.forward(ctx, f"bot:assets {target.name}", *args)
        if refusal:
            await ctx.send(embed=emb("🏘️ Real Estate", refusal, C_GREY))

    # ── !shop artifacts ───────────────────────────────────────────────────────
    @cmd_shop.command(name="artifacts",
                      help="List the permanent artifacts with passive effects; buy one, or sell one back for half its price",
                      usage="[buy [number]|sell [number]]")
    @_shop_subcommand(None)
    async def shop_artifacts(self, ctx: commands.Context, *args):
        from src.level_unlocks import user_global_display_level

        uid = ctx.author.id
        # Artifacts are owned bot-wide, so the gate is the global level too.
        lvl = user_global_display_level(uid)

        if not args:
            lines = []
            for i, art in enumerate(ARTIFACTS, start=1):
                req = art.get("level", 1)
                if owned_qty(uid, art["id"]) >= art["max"]:
                    lines.append(f"**{i}.** {art['effect']} — ✅ **Owned** · sells for {_artifact_sell_price(art):,} 🪙")
                elif lvl < req:
                    lines.append(f"~~**{i}.** {art['effect']} — **{art['cost']:,} 🪙**~~ 🔒 **Lvl {req}**")
                else:
                    lines.append(f"**{i}.** {art['effect']} — **{art['cost']:,} 🪙**")
            for art in RETIRED_ARTIFACTS:
                if owned_qty(uid, art["id"]) > 0:
                    lines.append(f"**Retired:** {art['effect']} — ✅ **Owned** · sells for {_artifact_sell_price(art):,} 🪙")
            lines.append("")
            lines.append("Buy one with `!artifacts buy`, or sell one back for half its price with `!artifacts sell`.")
            await send_ephemeral(ctx, embed=emb("🏺 Artifacts", "\n".join(lines), C_PURPLE))
            # Buy / Sell buttons under the listing; a pick runs the typed branch.
            picked = await pick_from_menus(
                ctx, title="🏺 Buy or sell",
                description="Buy an artifact, or sell one of yours back for half its price.",
                menus=[("buy", "Buy", _artifact_options(uid, lvl, "buy"), "Pick an artifact to buy."),
                       ("sell", "Sell", _artifact_options(uid, lvl, "sell"),
                        "Pick an artifact to sell back for half its price.")],
            )
            if picked:
                action, (art_id,) = picked
                await self._artifact_trade(ctx, action, _artifact_by_id(art_id))
            return

        action = args[0].lower()
        if action not in ("buy", "sell"):
            await ctx.send(embed=emb("🏺 Artifacts", "Usage: `!artifacts` to browse, `!artifacts buy [number]` to buy, `!artifacts sell [number]` to sell.", C_PURPLE))
            return
        if len(args) < 2:
            options = _artifact_options(uid, lvl, action)
            if not options:
                text = ("Nothing to buy right now — you own every artifact your level unlocks."
                        if action == "buy" else "You don't own any artifacts to sell.")
                await ctx.send(embed=emb("🏺 Artifacts", text, C_PURPLE))
                return
            picked = await pick_from_list(
                ctx, title="🏺 Buy Artifact" if action == "buy" else "🏺 Sell Artifact",
                description=("Pick an artifact to buy." if action == "buy"
                             else "Pick an artifact to sell back for half its price."),
                options=options, placeholder="Buy…" if action == "buy" else "Sell…", multi=False,
            )
            if picked:
                await self._artifact_trade(ctx, action, _artifact_by_id(picked[0]))
            return
        try:
            idx = int(args[1])
        except ValueError:
            idx = 0
        if not 1 <= idx <= len(ARTIFACTS):
            await ctx.send(embed=emb("❌ Invalid Artifact", f"Pick a number between 1 and {len(ARTIFACTS)} (see `!artifacts`).", C_RED))
            return
        await self._artifact_trade(ctx, action, ARTIFACTS[idx - 1])

    async def _artifact_trade(self, ctx: commands.Context, action: str, art: dict):
        if action == "sell":
            await self._artifact_sell(ctx, art)
        else:
            await self._artifact_buy(ctx, art)

    async def _artifact_sell(self, ctx: commands.Context, art: dict):
        uid = ctx.author.id
        if owned_qty(uid, art["id"]) <= 0:
            await ctx.send(embed=emb("🏺 Not Owned", "You don't own that artifact.", C_PURPLE))
            return
        # Godmode buys cost nothing, so a godmode sale pays nothing — otherwise
        # buy/sell would mint half the catalog price per cycle.
        refund = 0 if uid in state.godmode_users else _artifact_sell_price(art)
        if not await confirm_prompt(
            ctx, title="🏺 Sell Artifact",
            description=f"{art['effect']} — you lose its effect for good and get **{refund:,} 🪙** back.",
            payer=ctx.author,
        ):
            return

        # Claim synchronously after the confirm wait (see CLAUDE.md on per-user
        # command races): a second concurrent sell sees the copy gone and bails,
        # so one artifact can't be paid out twice.
        owned = state.user_artifacts.setdefault(uid, {})
        prior = owned.get(art["id"], 0)
        if prior <= 0:
            await ctx.send(embed=emb("🏺 Not Owned", "You don't own that artifact.", C_PURPLE))
            return
        # The savings boost is read from current ownership, so bank the
        # boosted interest earned so far while the artifact still counts.
        rebase_savings = bool(art.get("savings_rate_boost"))
        if rebase_savings:
            rebase_savings_deposits(uid)
        if prior > 1:
            owned[art["id"]] = prior - 1
        else:
            owned.pop(art["id"], None)
            state.user_artifact_acquired_at.get(uid, {}).pop(art["id"], None)
        if rebase_savings:
            await save_economy(uid=uid)
        if prior > 1:
            await save_user_artifact(uid, art["id"], prior - 1)
        else:
            await delete_user_artifact(uid, art["id"])
        await add_balance(uid, refund)
        await ctx.send(embed=emb("🏺 Artifact Sold", f"Its power is gone: {art['effect'].lower()}. **+{refund:,} 🪙**", C_GREEN))

    async def _artifact_buy(self, ctx: commands.Context, art: dict):
        from src.level_unlocks import user_global_display_level

        uid = ctx.author.id
        lvl = user_global_display_level(uid)
        req = art.get("level", 1)
        if lvl < req:
            await ctx.send(embed=emb("🔒 Level Locked", f"That artifact unlocks at **global Level {req}** (your highest level in any server) — you're Level {lvl}.", C_RED))
            return
        if owned_qty(uid, art["id"]) >= art["max"]:
            await ctx.send(embed=emb("🏺 Already Owned", "You already own that artifact.", C_PURPLE))
            return

        cost = 0 if uid in state.godmode_users else art["cost"]
        if not await confirm_purchase(
            ctx, title="🏺 Buy Artifact",
            description=f"{art['effect']} — permanent and yours forever.",
            cost=cost, payer=ctx.author,
        ):
            return

        # Gate-and-claim runs synchronously before the charge await so a
        # second concurrent !artifacts buy sees the claim and bails instead
        # of double-charging (see CLAUDE.md on per-user command races). The
        # owned re-check also covers drift during the confirm wait.
        owned = state.user_artifacts.setdefault(uid, {})
        prior = owned.get(art["id"], 0)
        if prior >= art["max"]:
            await ctx.send(embed=emb("🏺 Already Owned", "You already own that artifact.", C_PURPLE))
            return
        owned[art["id"]] = prior + 1
        # A first purchase stamps its acquisition time in the same
        # synchronous block — effects that switch on at purchase (the
        # savings-rate boost) read it as their boundary, so it must never
        # predate the coins leaving the wallet by more than this instant.
        acquired = state.user_artifact_acquired_at.setdefault(uid, {})
        if not prior:
            acquired[art["id"]] = time.time()
        if not await shop_charge(ctx, uid, cost, cost_label=f"{art['cost']:,}"):
            if prior:
                owned[art["id"]] = prior
            else:
                owned.pop(art["id"], None)
                acquired.pop(art["id"], None)
            return
        await save_user_artifact(uid, art["id"], prior + 1, acquired.get(art["id"]))
        await ctx.send(embed=emb("🏺 Artifact Acquired", f"Its power is now yours: {art['effect'].lower()}.", C_GREEN))

        # total_artifacts is a global per-user stat: try_set_record mirrors it
        # into the holder's other guilds, and only this guild announces.
        if ctx.guild is not None:
            total = owned_artifact_count(uid)
            if await try_set_record(
                ctx.guild.id, "total_artifacts", total, uid, ctx.author.display_name,
            ):
                await announce_record(
                    ctx.channel, "total_artifacts", ctx.author.display_name, total,
                    holder_id=uid,
                )

    # ── !shop nickname ────────────────────────────────────────────────────────
    @cmd_shop.command(name="nickname",
                      help="Change your own nickname, or another user's for a higher price; social insurance blocks it",
                      usage="[@user] <new name>")
    @_shop_subcommand("nickname")
    async def shop_nickname(self, ctx: commands.Context, *args):
        uid = ctx.author.id
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
        if len(new_name) > 32:
            await ctx.send(embed=emb("❌ Too Long", "Nicknames must be 32 characters or fewer.", C_RED))
            return
        if target.id != uid and await is_insured(target.id,"nickname"):
            _exp = get_insurance_expiry(target.id, "social")
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has social insurance and can't be renamed (expires <t:{_exp}:R>).", C_GOLD))
            return
        desc = (f"Change **{target.display_name}**'s nickname to **{new_name}**."
                if target.id != uid else f"Change your nickname to **{new_name}**.")
        if not await confirm_purchase(ctx, title="🎭 Nickname", description=desc, cost=cost, payer=ctx.author):
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
    @cmd_shop.command(name="removenickname",
                      help="Reset your nickname to your username")
    @_shop_subcommand("nickname")
    async def shop_removenickname(self, ctx: commands.Context):
        uid = ctx.author.id
        cost = 0 if uid in state.godmode_users else SHOP_NICKNAME_REMOVE_COST
        if not await confirm_purchase(
            ctx, title="🎭 Remove Nickname",
            description="Reset your nickname to your username.",
            cost=cost, payer=ctx.author,
        ):
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

    # ── !shop rolecreate ──────────────────────────────────────────────────────
    @cmd_shop.command(name="rolecreate", aliases=["createrole"],
                      help="Create a new role and assign it to a user; color it afterwards with rolecolor",
                      usage="@user <name>")
    @_shop_subcommand("createrole")
    async def shop_createrole(self, ctx: commands.Context, *args):
        uid = ctx.author.id
        if len(args) < 2:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop rolecreate @user <name>` (e.g. `!shop rolecreate @CoolGuy MyRole`). Set a color afterward with `!shop rolecolor`.", C_PURPLE))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        try:
            target = await resolve_member(ctx, args[0])
        except commands.BadArgument as exc:
            if not getattr(exc, "handled", False):  # a declined picker already answered
                await ctx.send(embed=emb("❌ Invalid User", "First argument must be a @mention, user ID, or name.", C_RED))
            return
        name = " ".join(args[1:])
        if not name:
            await ctx.send(embed=emb("❌ Invalid Name", "Role name cannot be empty.", C_RED))
            return
        if len(name) > 100:
            await ctx.send(embed=emb("❌ Too Long", "Role names must be 100 characters or fewer.", C_RED))
            return
        if "admin" in name.lower():
            await ctx.send(embed=emb("❌ Invalid Name", "Role names cannot contain \"admin\".", C_RED))
            return
        if target.id != uid and await is_insured(target.id,"role"):
            _exp = get_insurance_expiry(target.id, "social")
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has social insurance and can't be given new roles (expires <t:{_exp}:R>).", C_GOLD))
            return
        cost = 0 if uid in state.godmode_users else SHOP_ROLE_CREATE_COST
        if not await confirm_purchase(
            ctx, title="👑 Create Role",
            description=f"Create role **{name}** and assign it to **{target.display_name}**.",
            cost=cost, payer=ctx.author,
        ):
            return
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLE_CREATE_COST:,}"):
            return
        try:
            new_role = await ctx.guild.create_role(name=name, hoist=True)
        except discord.Forbidden:
            if cost > 0:
                await add_balance(uid, cost)
            log_bot_permission_error(ctx, "manage roles")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to manage roles.", C_RED))
            return
        except Exception as e:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
            return
        # Track the role the moment it exists, before the assign. The assign
        # can fail on its own (target left during the confirm prompt, role
        # hierarchy), and a live role the shop never registered is invisible
        # to !shop roledelete for good.
        # New roles go to the bottom of the rank ladder for this guild
        # (max rank + 1 — lowest priority). Operators promote them via
        # !shop roleup.
        existing_ranks = [r for (g, _r), r in state.bot_role_ranks.items() if g == ctx.guild.id]
        new_rank = (max(existing_ranks) + 1) if existing_ranks else 1
        state.bot_roles.add(new_role.id)
        state.bot_role_ranks[(ctx.guild.id, new_role.id)] = new_rank
        try:
            await save_bot_roles()
            await target.add_roles(new_role)
        except Exception as e:
            # Nothing the buyer paid for applied: take the role back down and
            # refund. If Discord refuses the delete the role stays tracked, so
            # !shop roledelete can still remove it.
            removed = await self._rollback_created_role(ctx.guild, new_role)
            if cost > 0:
                await add_balance(uid, cost)
            if isinstance(e, discord.Forbidden):
                log_bot_permission_error(ctx, "manage roles")
                why = "I don't have permission to assign it"
            else:
                why = f"I couldn't assign it to **{target.display_name}** ({e})"
            tail = (
                "The role was removed and you were refunded."
                if removed else
                "I couldn't remove the role either — it's tracked, so `!shop roledelete` can. You were refunded."
            )
            await ctx.send(embed=emb("❌ Role Not Assigned", f"Role **{name}** was created but {why}. {tail}", C_RED))
            return
        await ctx.send(embed=emb("✅ Role Created", f"Role **{name}** created and assigned to **{target.display_name}** — rank **#{new_rank}**. Give it a color with `!shop rolecolor @{name} <hex>`.", C_GREEN))

    # ── !shop assignrole ──────────────────────────────────────────────────────
    @cmd_shop.command(name="roleassign", aliases=["assignrole"],
                      help="Assign an existing bot-created role to a user; social insurance blocks it",
                      usage="@user <@role|role id>")
    @_shop_subcommand("assignrole")
    async def shop_assignrole(self, ctx: commands.Context, *args):
        uid = ctx.author.id
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if len(args) < 2:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop roleassign @user @role`", C_PURPLE))
            return
        try:
            target = await resolve_member(ctx, args[0])
        except commands.BadArgument as exc:
            if not getattr(exc, "handled", False):  # a declined picker already answered
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
        if target.id != uid and await is_insured(target.id,"role"):
            _exp = get_insurance_expiry(target.id, "social")
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has social insurance and can't be given new roles (expires <t:{_exp}:R>).", C_GOLD))
            return
        cost = 0 if uid in state.godmode_users else SHOP_ROLE_ASSIGN_COST
        if not await confirm_purchase(
            ctx, title="👑 Assign Role",
            description=f"Assign **{role.name}** to **{target.display_name}**.",
            cost=cost, payer=ctx.author,
        ):
            return
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

    # ── !shop unassignrole ──────────────────────────────────────────────────────
    @cmd_shop.command(name="roleunassign", aliases=["unassignrole"],
                      help="Remove a bot-created role from yourself or another user; bare lists the removable ones",
                      usage="[@user] [@role|role id]")
    @_shop_subcommand("unassignrole")
    async def shop_unassignrole(self, ctx: commands.Context, *args):
        uid = ctx.author.id
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
                await ctx.send(embed=emb("🛒 Bot Roles", f"{whose} removable roles:\n{lines}\n\nUse `!shop roleunassign [@user] @role` to remove one.", C_PURPLE))
            return
        role = self._resolve_role_strict(ctx.guild, role_args[0])
        if role is None or role.id not in state.bot_roles or role not in member.roles:
            who = member.display_name if member != ctx.author else "you"
            await ctx.send(embed=emb("❌ Not Found", f"**{who}** doesn't have that bot-created role. Use a @mention or role ID.", C_RED))
            return
        if role.id in state.locked_roles and state.locked_roles[role.id] != uid and uid not in state.godmode_users:
            await ctx.send(embed=emb("🔒 Locked", f"**{role.name}** is locked — only its owner can manage membership.", C_RED))
            return
        if member.id != uid and await is_insured(member.id, "role"):
            _exp = get_insurance_expiry(member.id, "social")
            await ctx.send(embed=emb("🛡️ Protected", f"**{member.display_name}** has social insurance and their roles can't be changed (expires <t:{_exp}:R>).", C_GOLD))
            return
        cost = 0 if uid in state.godmode_users else SHOP_ROLE_REMOVE_COST
        who = member.display_name if member != ctx.author else "you"
        if not await confirm_purchase(
            ctx, title="👑 Remove Role",
            description=f"Remove **{role.name}** from **{who}**.",
            cost=cost, payer=ctx.author,
        ):
            return
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLE_REMOVE_COST:,}"):
            return
        try:
            await member.remove_roles(role)
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
    @cmd_shop.command(name="roledelete", aliases=["deleterole"],
                      help="Permanently delete a bot-created role; bare lists the deletable ones",
                      usage="[@role|role id]")
    @_shop_subcommand("deleterole")
    async def shop_deleterole(self, ctx: commands.Context, *args):
        uid = ctx.author.id
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            existing = [r for r in ctx.guild.roles if r.id in state.bot_roles]
            if not existing:
                await ctx.send(embed=emb("🛒 Bot Roles", "No bot-created roles found in this server.", C_PURPLE))
            else:
                lines = "\n".join(f"• **{r.name}** (`{r.id}`)" for r in existing)
                await ctx.send(embed=emb("🛒 Bot Roles", f"Deletable roles:\n{lines}\n\nUse `!shop roledelete @role` to permanently delete one.", C_PURPLE))
            return
        role = self._resolve_role_strict(ctx.guild, args[0])
        if role is None or role.id not in state.bot_roles:
            await ctx.send(embed=emb("❌ Not Found", "Could not find that bot-created role. Use a @mention or role ID.", C_RED))
            return
        if role.id in state.locked_roles and state.locked_roles[role.id] != uid and uid not in state.godmode_users:
            await ctx.send(embed=emb("🔒 Locked", f"**{role.name}** is locked — only its owner can delete it.", C_RED))
            return
        insured_members = []
        for m in role.members:
            if await is_insured(m.id, "role"):
                insured_members.append(m)
        if insured_members:
            names = ", ".join(f"**{m.display_name}**" for m in insured_members)
            _earliest = min(get_insurance_expiry(m.id, "social") for m in insured_members)
            await ctx.send(embed=emb("🛡️ Protected", f"{names} {'has' if len(insured_members) == 1 else 'have'} social insurance — this role can't be deleted (expires <t:{_earliest}:R>).", C_GOLD))
            return
        cost = 0 if uid in state.godmode_users else SHOP_ROLE_DELETE_COST
        if not await confirm_purchase(
            ctx, title="👑 Delete Role",
            description=f"Permanently delete the role **{role.name}**.",
            cost=cost, payer=ctx.author,
        ):
            return
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLE_DELETE_COST:,}"):
            return
        try:
            name = role.name
            await role.delete()
            state.bot_roles.discard(role.id)
            # Drop the rank entry too. The ladder isn't compacted, so deletes
            # leave gaps in the rank numbers — that's fine.
            state.bot_role_ranks.pop((ctx.guild.id, role.id), None)
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
    @cmd_shop.command(name="channelcreate", aliases=["createchannel"],
                      help="Create a new text channel under the bot-channels category",
                      usage="<name>")
    @_shop_subcommand("channel")
    async def shop_createchannel(self, ctx: commands.Context, *args):
        uid = ctx.author.id
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop channelcreate <name>`", C_PURPLE))
            return
        channel_name = " ".join(args).lower()
        channel_name = channel_name.replace(" ", "-")[:100]
        if len(channel_name) < 2:
            await ctx.send(embed=emb("❌ Invalid Name", "Channel name must be at least 2 characters.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_CHANNEL_COST
        if not await confirm_purchase(
            ctx, title="📢 Create Channel",
            description=f"Create a new text channel **#{channel_name}**.",
            cost=cost, payer=ctx.author,
        ):
            return
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
    @cmd_shop.command(name="channeldelete", aliases=["deletechannel"],
                      help="Delete a bot-created channel; bare lists the removable ones",
                      usage="[#channel|channel id]")
    @_shop_subcommand("channel")
    async def shop_deletechannel(self, ctx: commands.Context, *args):
        uid = ctx.author.id
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
                await ctx.send(embed=emb("🛒 Bot Channels", f"Removable channels:\n{lines}\n\nUse `!shop channeldelete #channel` to delete one.", C_PURPLE))
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
        if not await confirm_purchase(
            ctx, title="📢 Delete Channel",
            description=f"Permanently delete {channel.mention}.",
            cost=cost, payer=ctx.author,
        ):
            return
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
    @cmd_shop.command(name="channelrename", aliases=["renamechannel"],
                      help="Rename a bot-created channel",
                      usage="<#channel|channel id> <new name>")
    @_shop_subcommand("renamechannel")
    async def shop_renamechannel(self, ctx: commands.Context, *args):
        uid = ctx.author.id
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if len(args) < 2:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop channelrename <channel> <new name>`", C_PURPLE))
            return
        new_name = " ".join(args[1:]).lower().replace(" ", "-")[:100]
        if len(new_name) < 2:
            await ctx.send(embed=emb("❌ Invalid Name", "Channel name must be at least 2 characters.", C_RED))
            return
        target_channel = self._resolve_channel_strict(ctx.guild, args[0])
        if target_channel is None or not self._is_bot_channel(ctx.guild, target_channel):
            await ctx.send(embed=emb("❌ Not Found", "Could not find that bot-created channel. Use a #mention or channel ID.", C_RED))
            return
        if target_channel.id in state.locked_channels and state.locked_channels[target_channel.id] != uid and uid not in state.godmode_users:
            await ctx.send(embed=emb("🔒 Locked", f"**{target_channel.name}** is locked — only its owner can rename it.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_RENAME_COST
        if not await confirm_purchase(
            ctx, title="📢 Rename Channel",
            description=f"Rename **{target_channel.name}** to **{new_name}**.",
            cost=cost, payer=ctx.author,
        ):
            return
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
    @cmd_shop.command(name="rolerename", aliases=["renamerole"],
                      help="Rename a bot-created role",
                      usage="<@role|role id> | <new name>")
    @_shop_subcommand("renamerole")
    async def shop_renamerole(self, ctx: commands.Context, *args):
        uid = ctx.author.id
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        full_arg = " ".join(args)
        if " | " not in full_arg:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop rolerename @role | <new name>`", C_PURPLE))
            return
        role_arg, new_name = [s.strip() for s in full_arg.split(" | ", 1)]
        if not new_name:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop rolerename @role | <new name>`", C_PURPLE))
            return
        if len(new_name) > 100:
            await ctx.send(embed=emb("❌ Too Long", "Role names must be 100 characters or fewer.", C_RED))
            return
        role = self._resolve_role_strict(ctx.guild, role_arg)
        if role is None or role.id not in state.bot_roles:
            await ctx.send(embed=emb("❌ Not Found", "Could not find that bot-created role. Use a @mention or role ID.", C_RED))
            return
        if role.id in state.locked_roles and state.locked_roles[role.id] != uid and uid not in state.godmode_users:
            await ctx.send(embed=emb("🔒 Locked", f"**{role.name}** is locked — only its owner can rename it.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_RENAME_COST
        if not await confirm_purchase(
            ctx, title="👑 Rename Role",
            description=f"Rename **{role.name}** to **{new_name}**.",
            cost=cost, payer=ctx.author,
        ):
            return
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
    @cmd_shop.command(name="rolechannel",
                      help="Restrict a bot-created channel so only one role can see it",
                      usage="<@role|role id> <#channel|channel id>")
    @_shop_subcommand("rolechannel")
    async def shop_rolechannel(self, ctx: commands.Context, *args):
        uid = ctx.author.id
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
        if target_channel is None or not self._is_bot_channel(ctx.guild, target_channel):
            await ctx.send(embed=emb("❌ Not Found", "Could not find that bot-created channel. Use a #mention or channel ID.", C_RED))
            return
        if target_channel.id in state.locked_channels and state.locked_channels[target_channel.id] != uid and uid not in state.godmode_users:
            await ctx.send(embed=emb("🔒 Locked", f"**{target_channel.name}** is locked — only its owner can change its permissions.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_ROLECHANNEL_COST
        if not await confirm_purchase(
            ctx, title="📢 Restrict Channel",
            description=f"Make {target_channel.mention} visible only to **{role.name}**.",
            cost=cost, payer=ctx.author,
        ):
            return
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLECHANNEL_COST:,}"):
            return
        # Three overwrites, in this order so the bot keeps access before
        # @everyone loses it. Each is its own HTTP call, so remember what each
        # target had: a failure on the second or third must restore the
        # earlier ones before the full price goes back.
        applied: list[tuple] = []
        try:
            for who, perms in (
                (ctx.guild.me, {"read_messages": True, "send_messages": True}),
                (role, {"read_messages": True}),
                (ctx.guild.default_role, {"read_messages": False}),
            ):
                before = target_channel.overwrites_for(who)
                await target_channel.set_permissions(who, **perms)
                applied.append((who, before))
        except Exception as e:
            await self._restore_overwrites(target_channel, applied)
            if cost > 0:
                await add_balance(uid, cost)
            if isinstance(e, discord.Forbidden):
                log_bot_permission_error(ctx, "manage channel permissions")
                await ctx.send(embed=emb("❌ No Permission", "I don't have permission to manage that channel's permissions.", C_RED))
            else:
                await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
            return
        await ctx.send(embed=emb("✅ Channel Restricted", f"{target_channel.mention} is now only visible to **{role.name}**.", C_GREEN))

    # ── !shop lockchannel ─────────────────────────────────────────────────────
    @cmd_shop.command(name="channellock", aliases=["lockchannel"],
                      help="Lock a bot-created channel so only you can rename, restrict or delete it",
                      usage="<#channel|channel id>")
    @_shop_subcommand("lockchannel")
    async def shop_lockchannel(self, ctx: commands.Context, *args):
        uid = ctx.author.id
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop channellock #channel`", C_PURPLE))
            return
        target_channel = self._resolve_channel_strict(ctx.guild, args[0])
        if target_channel is None or not self._is_bot_channel(ctx.guild, target_channel):
            await ctx.send(embed=emb("❌ Not Found", "Could not find that bot-created channel. Use a #mention or channel ID.", C_RED))
            return
        if target_channel.id in state.locked_channels:
            await ctx.send(embed=emb("❌ Already Locked", f"{target_channel.mention} is already locked.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_LOCK_COST
        if not await confirm_purchase(
            ctx, title="🔒 Lock Channel",
            description=f"Lock {target_channel.mention} so only you can modify or delete it.",
            cost=cost, payer=ctx.author,
        ):
            return
        # Re-check and claim synchronously after the confirm wait — a
        # concurrent locker may have taken it during the prompt.
        if target_channel.id in state.locked_channels:
            await ctx.send(embed=emb("❌ Already Locked", f"{target_channel.mention} is already locked.", C_RED))
            return
        state.locked_channels[target_channel.id] = uid
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_LOCK_COST:,}"):
            state.locked_channels.pop(target_channel.id, None)
            return
        cfg = get_guild_cfg(ctx.guild.id)
        cfg.setdefault("locked_channels", {})[str(target_channel.id)] = uid
        await save_guild_settings()
        await ctx.send(embed=emb("🔒 Channel Locked", f"{target_channel.mention} is now locked. Only you can modify or delete it.", C_GREEN))

    # ── !shop unlockchannel ───────────────────────────────────────────────────
    @cmd_shop.command(name="channelunlock", aliases=["unlockchannel"],
                      help="Unlock a channel you locked (lock owner only)",
                      usage="<#channel|channel id>")
    @_shop_subcommand(None)
    async def shop_unlockchannel(self, ctx: commands.Context, *args):
        uid = ctx.author.id

        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop channelunlock #channel`", C_PURPLE))
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
    @cmd_shop.command(name="rolelock", aliases=["lockrole"],
                      help="Lock a role so only you can modify, delete or manage membership of it",
                      usage="<@role|role id>")
    @_shop_subcommand("lockrole")
    async def shop_lockrole(self, ctx: commands.Context, *args):
        uid = ctx.author.id
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop rolelock @role`", C_PURPLE))
            return
        role = self._resolve_role_strict(ctx.guild, args[0])
        if role is None:
            await ctx.send(embed=emb("❌ Not Found", "Could not find that role. Use a @mention or role ID.", C_RED))
            return
        if role.id in state.locked_roles:
            await ctx.send(embed=emb("❌ Already Locked", f"**{role.name}** is already locked.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_LOCK_COST
        if not await confirm_purchase(
            ctx, title="🔒 Lock Role",
            description=f"Lock **{role.name}** so only you can modify, delete, or manage it.",
            cost=cost, payer=ctx.author,
        ):
            return
        # Re-check and claim synchronously after the confirm wait — a
        # concurrent locker may have taken it during the prompt.
        if role.id in state.locked_roles:
            await ctx.send(embed=emb("❌ Already Locked", f"**{role.name}** is already locked.", C_RED))
            return
        state.locked_roles[role.id] = uid
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_LOCK_COST:,}"):
            state.locked_roles.pop(role.id, None)
            return
        cfg = get_guild_cfg(ctx.guild.id)
        cfg.setdefault("locked_roles", {})[str(role.id)] = uid
        await save_guild_settings()
        await ctx.send(embed=emb("🔒 Role Locked", f"**{role.name}** is now locked. Only you can modify, delete, or manage membership of this role.", C_GREEN))

    # ── !shop unlockrole ──────────────────────────────────────────────────────
    @cmd_shop.command(name="roleunlock", aliases=["unlockrole"],
                      help="Unlock a role you locked (lock owner only)",
                      usage="<@role|role id>")
    @_shop_subcommand(None)
    async def shop_unlockrole(self, ctx: commands.Context, *args):
        uid = ctx.author.id

        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop roleunlock @role`", C_PURPLE))
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
    @cmd_shop.command(name="ragebait",
                      help=f"Have the AI ragebait a user for {SHOP_RAGEBAIT_MESSAGES + 1} messages, optionally on a topic; social insurance blocks it",
                      usage="@user [topic]")
    @_shop_subcommand("ragebait")
    async def shop_ragebait(self, ctx: commands.Context, *args):
        uid = ctx.author.id
        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop ragebait @user [topic]`", C_PURPLE))
            return
        try:
            target = await resolve_member(ctx, args[0])
        except commands.BadArgument as exc:
            if not getattr(exc, "handled", False):  # a declined picker already answered
                await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop ragebait @user [topic]`", C_PURPLE))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return
        gid = ctx.guild.id
        if target.id != uid and await is_insured(target.id,"ragebait"):
            _exp = get_insurance_expiry(target.id, "social")
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has social insurance against ragebait (expires <t:{_exp}:R>).", C_GOLD))
            return
        if _targeted_buy_used(uid, "ragebait"):
            await ctx.send(embed=_once_a_day_embed("ragebait"))
            return
        topic = " ".join(args[1:])
        cost = 0 if uid in state.godmode_users else SHOP_RAGEBAIT_COST
        topic_note = f" (topic: {topic})" if topic else ""
        if not await confirm_purchase(
            ctx, title="🎣 Ragebait",
            description=f"Ragebait **{target.display_name}** for {SHOP_RAGEBAIT_MESSAGES + 1} messages{topic_note}.",
            cost=cost, payer=ctx.author,
        ):
            return
        claimed, prior_buy = await _claim_targeted_buy(ctx, "ragebait")
        if not claimed:
            return
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_RAGEBAIT_COST:,}"):
            await _release_targeted_buy(uid, "ragebait", prior_buy)
            return
        await save_economy(uid)
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
                ], placeholder, user_id=uid)
        except Exception as e:
            await _release_targeted_buy(uid, "ragebait", prior_buy)
            if cost > 0:
                await add_balance(uid, cost)
            await placeholder.edit(content=f"⚠️ {e}")
            return
        finally:
            typing_task.cancel()
        if not full_response:
            # AI disabled or token budget denied — stream_ollama already
            # edited the placeholder with the reason. Refund; don't
            # activate the effect or post a bare mention.
            await _release_targeted_buy(uid, "ragebait", prior_buy)
            if cost > 0:
                await add_balance(uid, cost)
            return
        # Activate and save before posting the opener: a failed save must refund
        # with nothing posted and no live effect.
        key = (gid, target.id)
        state.active_ragebaits[key] = {"remaining": SHOP_RAGEBAIT_MESSAGES, "history": [], "channel_id": ctx.channel.id}
        try:
            await save_ragebait()
        except Exception as e:
            state.active_ragebaits.pop(key, None)
            await _release_targeted_buy(uid, "ragebait", prior_buy)
            if cost > 0:
                await add_balance(uid, cost)
            await placeholder.edit(content=f"⚠️ {e}")
            return
        try:
            await finalize(placeholder, ctx.channel, f"{target.mention} {full_response}")
        except Exception:
            # The effect is live and saved — the buyer got what they paid for,
            # and the streamed text already sits in the placeholder. No refund.
            pass

    # ── !shop mock ────────────────────────────────────────────────────────────
    @cmd_shop.command(name="mock",
                      help=f"Mock a user's next {SHOP_MOCK_MESSAGES} messages; social insurance blocks it",
                      usage="@user")
    @_shop_subcommand(None)
    async def shop_mock(self, ctx: commands.Context, *args):
        uid = ctx.author.id

        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop mock @user`", C_PURPLE))
            return
        try:
            target = await MemberConverter().convert(ctx, args[0])
        except commands.BadArgument:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop mock @user`", C_PURPLE))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return
        gid = ctx.guild.id
        if target.id != uid and await is_insured(target.id,"mock"):
            _exp = get_insurance_expiry(target.id, "social")
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has social insurance against mock (expires <t:{_exp}:R>).", C_GOLD))
            return
        if _targeted_buy_used(uid, "mock"):
            await ctx.send(embed=_once_a_day_embed("mock"))
            return
        cost = 0 if uid in state.godmode_users else SHOP_MOCK_COST
        if not await confirm_purchase(
            ctx, title="🎭 Mock",
            description=f"Mock **{target.display_name}**'s next {SHOP_MOCK_MESSAGES} messages.",
            cost=cost, payer=ctx.author,
        ):
            return
        claimed, prior_buy = await _claim_targeted_buy(ctx, "mock")
        if not claimed:
            return
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_MOCK_COST:,}"):
            await _release_targeted_buy(uid, "mock", prior_buy)
            return
        await save_economy(uid)
        state.active_mocks[(gid, target.id)] = {"remaining": SHOP_MOCK_MESSAGES, "started_by": uid, "channel_id": ctx.channel.id}
        await save_mock()
        await ctx.send(embed=emb(
            "🎭 Mock Activated",
            f"**{target.display_name}** will have their next {SHOP_MOCK_MESSAGES} messages mocked!",
            C_PURPLE,
        ))

    # ── !shop bounty ──────────────────────────────────────────────────────────
    @cmd_shop.command(name="bounty",
                      help="Post an escrowed coin reward another user can claim in the bounty channel",
                      usage="<coins> [duration] <condition>")
    @_shop_subcommand(None)
    async def shop_bounty(self, ctx: commands.Context, *args):
        # The whole bounty feature lives in BountyCog (its own persistence,
        # reaction dispatch, and expiry loop). This subcommand and the !bounty
        # top-level alias both funnel into create_bounty so the channel gating,
        # escrow, and embed are defined in one place.
        cog = self.bot.get_cog("BountyCog") if self.bot else None
        if cog is None:
            await ctx.send(embed=emb("🎯 Bounty", "Bounties are unavailable right now.", C_GREY))
            return
        await cog.create_bounty(ctx, args)

    # ── !shop announce ────────────────────────────────────────────────────────
    @cmd_shop.command(name="announce",
                      help=f"Read your message aloud in a voice channel (text-to-speech) for "
                           f"{SHOP_ANNOUNCE_COST_PER_CHAR} 🪙 a character, times the x<N> repeats; `stop` cuts yours short",
                      usage='"<message>" <voice-channel> [x<N>]')
    @_shop_subcommand("announce")
    async def shop_announce(self, ctx: commands.Context, *, args: str = None):
        # Keyword-only so the quotes reach the parser — discord.py's *args
        # would strip them. The voice work lives in AnnounceCog with the free
        # admin command; this subcommand and the !announce alias funnel into
        # its paid path so the charge, confirm and refund are defined once.
        cog = self.bot.get_cog("AnnounceCog") if self.bot else None
        if cog is None:
            await ctx.send(embed=emb("📢 Announce", "Announcements are unavailable right now.", C_GREY))
            return
        await cog.paid_announce(ctx, args)

    # ── !shop insurance ───────────────────────────────────────────────────────
    # Three types (INSURANCE_TYPES): personal refunds crime losses, social
    # blocks the shop effects, property covers repair bills (unsold until
    # PROPERTY_INSURANCE_LIVE). Each type is its own policy and subscription.
    # Bare `!shop insurance` is the menu (src/insurance_view.py): one message,
    # a type dropdown, then that type's tier buttons with a Back. Typed, a
    # subscribe opens the same tier buttons through confirm_choice. Every
    # button label carries the tier's price and cover. A user has ONE tier per type at a time
    # (policy and subscription agree — set_insurance_tier); switching moves
    # the remaining coverage with them, charging the daily difference on it
    # for an upgrade (insurance_switch_cost) and nothing for a downgrade.
    INSURANCE_PROTECTS_STR = "mock, ragebait, curses, nickname changes, role assignments, and tax"

    @staticmethod
    def _insurance_kinds() -> list[str]:
        """The types on sale; property waits for property theft."""
        return [k for k in INSURANCE_TYPES if k != "property" or PROPERTY_INSURANCE_LIVE]

    @staticmethod
    def _policy_name(kind: str, tier: str | None = None) -> str:
        """'Premium personal insurance', or 'Social insurance' for a one-tier type."""
        spec = INSURANCE_TYPES[kind]
        if len(spec["tiers"]) == 1 or tier is None:
            return f"{spec['label']} insurance"
        return f"{tier.title()} {spec['label'].lower()} insurance"

    def _coverage_text(self, kind: str, tier: str) -> str:
        info = insurance_tier_info(tier, kind)
        if kind == "social":
            return f"protection from {self.INSURANCE_PROTECTS_STR}"
        if kind == "property":
            return f"**{info['refund_pct']}%** of property repair bills covered (up to {info['refund_cap']:,} 🪙 per theft)"
        return f"**{info['refund_pct']}%** of crime losses refunded (up to {info['refund_cap']:,} 🪙 per robbery)"

    def _rollback_insurance_days(self, key: int, days: int, kind: str = "personal"):
        """Undo a stamped-but-unpaid insurance extension. A concurrent purchase
        may have extended on top of our stamp, so subtract our duration rather
        than restoring a snapshot (which would clobber the other buyer)."""
        policies = insurance_policies(kind)[0]
        entry = policies.get(key)
        if entry:
            entry["expires_at"] -= days * SHOP_INSURANCE_DURATION_SECS
            if entry["expires_at"] <= time.time():
                policies.pop(key, None)

    @staticmethod
    def _sub_needs_first_day(expiry: int | None) -> bool:
        """True when a new subscriber has to buy their first day right now:
        no active coverage, or coverage that runs out before the next 5am
        sweep — the first premium the subscription itself would charge.
        Without that bridge a mid-day subscriber lapses from their old expiry
        until 5am. When coverage already reaches past the next sweep, the
        sweep's charge lands while it still holds and extends from it."""
        return expiry is None or expiry < next_daily_reset_ts()

    INSURANCE_BLURBS = {
        "personal": "Refunds part of what crime takes from you, into your wallet. The thief keeps their take.",
        "social": f"Blocks {INSURANCE_PROTECTS_STR} aimed at you.",
        "property": "Covers part of a property's repair bill after a theft.",
    }

    @staticmethod
    def _short_coins(n: int) -> str:
        if n >= 1_000_000 and n % 100_000 == 0:
            return f"{n / 1_000_000:g}m"
        if n >= 1_000 and n % 100 == 0:
            return f"{n / 1_000:g}k"
        return f"{n:,}"

    def _insurance_coverage_line(self, uid: int, kind: str) -> str:
        """The buyer's coverage of one type. Every purchase is a subscription,
        so coverage without one is a cancelled subscription running out."""
        exp = get_insurance_expiry(uid, kind)
        tier = get_insurance_tier(uid, kind)
        sub_tier = insurance_policies(kind)[1].get(uid)
        if exp and tier:
            what = "Active" if len(INSURANCE_TYPES[kind]["tiers"]) == 1 else (
                f"**{tier.title()}**, {self._coverage_text(kind, tier)}")
            tail = "renews at the 5am reset" if sub_tier else f"cancelled, ends <t:{exp}:R>"
            return f"**Coverage:** {what} · {tail}"
        if sub_tier:
            # Subscribed but uncovered: the last sweep couldn't charge.
            return "**Coverage:** none · subscribed, it starts at the next 5am charge you can afford"
        return "**Coverage:** none"

    def _insurance_overview(self, uid: int) -> str:
        """Every type on sale with the user's coverage of it."""
        return "\n\n".join(
            f"__{INSURANCE_TYPES[kind]['emoji']} {INSURANCE_TYPES[kind]['label']} insurance__\n"
            + self._insurance_coverage_line(uid, kind)
            for kind in self._insurance_kinds()
        )

    def _insurance_info_embed(self, uid: int) -> discord.Embed:
        return emb(
            "🛡️ Insurance",
            "Insurance is a daily subscription, charged at the 5am reset. Bare `!shop insurance` "
            "opens the menu; typed, `!shop insurance [personal|social|property] [tier]` subscribes and "
            "`!shop insurance [type] unsub` stops. The type defaults to personal, and the prompt "
            "shows every tier, so the tier word is optional. Cancelling keeps the coverage "
            "you've paid for.\n\n"
            "Crime still goes through with personal insurance — the insurer pays the refund into your "
            "wallet and the thief keeps their take. Social insurance stops the effects outright. "
            "Property insurance pays part of the repair bill after a property theft, at the tier you "
            "held when it happened. All of them work in every server.\n\n"
            + self._insurance_overview(uid),
            C_PURPLE,
        )

    def _insurance_prompt(self, uid: int, kind: str) -> str:
        """What a type's tier buttons sit under: what it does, the buyer's
        coverage, and what pressing a button charges now."""
        exp = get_insurance_expiry(uid, kind)
        lines = [
            self.INSURANCE_BLURBS[kind] + " Charged at the 5am reset each day, valid in every server.",
            self._insurance_coverage_line(uid, kind),
        ]
        if exp is None:
            lines.append("The first day is charged now so coverage starts immediately.")
        elif self._sub_needs_first_day(exp):
            lines.append("Your coverage runs out before the next 5am charge, so the first day is charged now.")
        if exp is not None and len(INSURANCE_TYPES[kind]["tiers"]) > 1:
            lines.append("Another tier moves your remaining coverage with you: an upgrade charges the "
                         "difference on it now, a downgrade refunds nothing.")
        return "\n\n".join(lines)

    def _insurance_tier_choices(self, uid: int, kind: str, preferred: str | None) -> list[dict]:
        """One button per tier. A label reads 'Standard — 3,000 🪙/day (3,000
        now) · 75% up to 200k': the premium, what pressing it charges now (the
        starter day plus any upgrade surcharge), and the cover."""
        days = 1 if self._sub_needs_first_day(get_insurance_expiry(uid, kind)) else 0
        choices = []
        for name, info in INSURANCE_TYPES[kind]["tiers"].items():
            if uid in state.godmode_users:
                price = "free"
            else:
                now = info["cost"] * days + insurance_switch_cost(uid, name, kind)
                price = f"{info['cost']:,} 🪙/day" + (f" ({now:,} now)" if now else "")
            cover = "" if kind == "social" else (
                f" · {info['refund_pct']}% up to {self._short_coins(info['refund_cap'])}")
            choices.append({"label": f"{name.title()} — {price}{cover}", "value": name,
                            "default": name == preferred})
        return choices

    @cmd_shop.command(name="insurance",
                      help="Insurance menu: personal refunds crime losses, social blocks mock/tax/nickname/role effects, property pays theft repairs",
                      usage="[personal|social|property] [tier] [unsub]")
    @_shop_subcommand(None)
    async def shop_insurance(self, ctx: commands.Context, *args: str):
        uid = ctx.author.id
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return
        if not args:
            await self._insurance_menu(ctx)
            return
        # Insurance is bot-wide: one policy per user per type, valid in every
        # server (the purchase itself still happens in a server). Args come in
        # any order: a type, a tier of that type, sub/unsub. Every purchase is
        # a subscription, so "sub" is optional. Anything else ("info", a day
        # count, a typo) is an info request.
        words = [raw.lower() for raw in args]
        kind = next((w for w in words if w in INSURANCE_TYPES), "personal")
        if kind not in self._insurance_kinds():
            await ctx.send(embed=emb(
                "🏠 Property Insurance",
                "Property insurance isn't on sale right now.",
                C_GOLD,
            ))
            return
        tier_arg: str | None = None
        unsub = False
        for word in words:
            if word in INSURANCE_TYPES or word in ("sub", "subscribe"):
                continue
            if word in INSURANCE_TYPES[kind]["tiers"]:
                tier_arg = word
            elif word in ("unsub", "unsubscribe"):
                unsub = True
            else:
                await ctx.send(embed=self._insurance_info_embed(uid))
                return

        if unsub:
            await self._insurance_unsubscribe(ctx, kind)
        else:
            await self._insurance_subscribe(ctx, kind, tier_arg)

    async def _insurance_menu(self, ctx: commands.Context):
        """Bare `!shop insurance`: one message that pages from the overview to
        a type's tier buttons and back (src/insurance_view.py)."""
        from src.insurance_view import InsuranceView
        view = InsuranceView(self, ctx)
        view.message = await ctx.send(embed=view.embed(), view=view)

    async def _insurance_unsubscribe(self, ctx: commands.Context, kind: str = "personal"):
        uid = ctx.author.id
        subs = insurance_policies(kind)[1]
        name = self._policy_name(kind)
        if uid not in subs:
            await ctx.send(embed=emb(
                "🛡️ Not Subscribed",
                f"You don't have a {name.lower()} subscription. `!shop insurance` to start one.",
                C_GOLD,
            ))
            return
        subs.pop(uid, None)
        await save_insurance_subs(kind)
        exp = get_insurance_expiry(uid, kind)
        tail = f" Your current coverage still runs out <t:{exp}:R>." if exp else ""
        await ctx.send(embed=emb(
            "🛡️ Insurance Unsubscribed",
            f"You'll no longer be charged the daily {name.lower()} premium.{tail}",
            C_GREEN,
        ))

    async def _insurance_subscribe(self, ctx: commands.Context, kind: str, tier_arg: str | None):
        """Typed `!shop insurance [type] [tier]`: the tier buttons, then
        `_insurance_apply` with the pick."""
        uid = ctx.author.id
        preferred = tier_arg or get_insurance_tier(uid, kind) or INSURANCE_TYPES[kind]["default"]
        chosen = await confirm_choice(
            ctx, title=f"{INSURANCE_TYPES[kind]['emoji']} {self._policy_name(kind)} Subscription",
            description=self._insurance_prompt(uid, kind),
            choices=self._insurance_tier_choices(uid, kind, preferred), payer=ctx.author,
        )
        if chosen:
            await self._insurance_apply(ctx, kind, chosen)

    async def _insurance_apply(self, ctx: commands.Context, kind: str, chosen: str):
        """Subscribe at `chosen`, or move an existing subscription of that
        type, and its coverage, to it."""
        uid = ctx.author.id
        key = uid
        subs = insurance_policies(kind)[1]
        # The picker wait is a long await — a concurrent subscribe may have
        # landed during the prompt.
        if subs.get(key) == chosen:
            await ctx.send(embed=emb(
                "🛡️ Already Subscribed",
                f"You're already subscribed to {self._policy_name(kind, chosen).lower()} — "
                f"**{insurance_tier_cost(chosen, kind):,} 🪙** is charged at the 5am reset each day. "
                "`!shop insurance` to cancel.",
                C_GOLD,
            ))
            return
        # Claim the sub synchronously before any await so two concurrent
        # subscribes can't both buy the starter day; remember what to restore.
        prev_sub_tier = subs.get(key)
        prev_tier = get_insurance_tier(uid, kind)
        subs[key] = chosen
        switch = insurance_switch_cost(uid, chosen, kind)
        cur_exp = get_insurance_expiry(uid, kind)
        bridged = self._sub_needs_first_day(cur_exp)
        expires_at = None
        charge = switch
        if bridged:
            # Charge the first day now, stacked on any remaining coverage —
            # see _sub_needs_first_day.
            expires_at = extend_insurance(uid, 1, tier=chosen, kind=kind)
            charge += insurance_tier_cost(chosen, kind)
        set_insurance_tier(uid, chosen, kind)
        cost = 0 if uid in state.godmode_users else charge
        if not await shop_charge(ctx, uid, cost, cost_label=f"{cost:,}"):
            if prev_sub_tier is None:
                subs.pop(key, None)
            else:
                subs[key] = prev_sub_tier
            if bridged:
                self._rollback_insurance_days(key, 1, kind)
            if prev_tier is not None:
                set_insurance_tier(uid, prev_tier, kind)
            return
        await save_insurance(kind)
        await save_insurance_subs(kind)

        premium = insurance_tier_cost(chosen, kind)
        if bridged:
            if cur_exp is None:
                start_str = (
                    f" First day charged now — {self._coverage_text(kind, chosen)} "
                    f"(expires <t:{expires_at}:R>)."
                )
            else:
                start_str = (
                    f" Your coverage would have run out <t:{cur_exp}:R>, before the first 5am charge, "
                    f"so the first day was charged now — covered until <t:{expires_at}:f>."
                )
        else:
            start_str = ""
        switch_str = ""
        if prev_tier is not None and prev_tier != chosen:
            if switch > 0:
                switch_str = f" **{switch:,} 🪙** charged to move your remaining coverage up to {chosen.title()}."
            else:
                switch_str = f" Your remaining coverage moved down to {chosen.title()} (no refund on the difference)."
        if prev_sub_tier is not None:
            title = "🛡️ Subscription Switched"
            body = (
                f"Your subscription is now {self._policy_name(kind, chosen)} — **{premium:,} 🪙** is charged "
                f"at the 5am reset each day from now on.{switch_str}{start_str}"
            )
        else:
            title = "🛡️ Insurance Subscribed"
            body = (
                f"{self._policy_name(kind, chosen)} — **{premium:,} 🪙** is now charged at the 5am reset "
                f"each day, adding 24h of coverage.{switch_str}{start_str} Cancel anytime from "
                "`!shop insurance`."
            )
        await ctx.send(embed=emb(title, body, C_GREEN))

    # ── !shop rolecolor ───────────────────────────────────────────────────────
    @cmd_shop.command(name="rolecolor",
                      help="Change a bot-created role's color (hex code or color name)",
                      usage="<@role|role id> <color>")
    @_shop_subcommand(None)
    async def shop_rolecolor(self, ctx: commands.Context, *args):
        uid = ctx.author.id

        if len(args) < 2:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop rolecolor @role <color>`", C_PURPLE))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return
        color_str = args[-1]
        role = self._resolve_role_strict(ctx.guild, args[0])
        if role is None or role.id not in state.bot_roles:
            await ctx.send(embed=emb("❌ Not Found", "Could not find that bot-created role. Use a @mention or role ID.", C_RED))
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
        if not await confirm_purchase(
            ctx, title="🎨 Role Color",
            description=f"Set **{role.name}**'s color to `{color_str}`.",
            cost=cost, payer=ctx.author,
        ):
            return
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLECOLOR_COST:,}"):
            return
        try:
            await role.edit(color=color)
            await ctx.send(embed=emb("🎨 Role Color Changed", f"**{role.name}** color set to `{color_str}`.", C_PURPLE))
        except discord.Forbidden:
            if cost > 0:
                await add_balance(uid, cost)
            log_bot_permission_error(ctx, "edit roles")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to edit roles.", C_RED))
        except Exception as e:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Error", f"Failed to change role color: {str(e)}", C_RED))

    # ── !shop tax (+ guild-configured aliases) ───────────────────────────────
    @cmd_shop.command(name="tax",
                      help=f"Tax a user {SHOP_TAX_PER_MESSAGE} coins per message for 24h, paid to you; social insurance blocks it",
                      usage="@user")
    @_shop_subcommand(None)
    async def shop_tax(self, ctx: commands.Context, *args):
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
            target = await resolve_member(ctx, args[0])
        except commands.BadArgument as exc:
            if not getattr(exc, "handled", False):  # a declined picker already answered
                await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop tax @user`", C_PURPLE))
            return
        if target.id == uid:
            await ctx.send(embed=emb("❌ Error", "You can't tax yourself!", C_RED))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return
        gid = ctx.guild.id
        if await is_insured(target.id,"tax"):
            _exp = get_insurance_expiry(target.id, "social")
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has social insurance against tax (expires <t:{_exp}:R>).", C_GOLD))
            return
        if _targeted_buy_used(uid, "tax"):
            await ctx.send(embed=_once_a_day_embed("tax"))
            return
        cost = 0 if uid in state.godmode_users else SHOP_TAX_COST
        if not await confirm_purchase(
            ctx, title=f"{tax_emoji} {tax_type.capitalize()} Tax",
            description=(
                f"Tax **{target.display_name}** — they'll owe you "
                f"**{SHOP_TAX_PER_MESSAGE:,} 🪙** per message for 24h."
            ),
            cost=cost, payer=ctx.author,
        ):
            return
        claimed, prior_buy = await _claim_targeted_buy(ctx, "tax")
        if not claimed:
            return
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_TAX_COST:,}"):
            await _release_targeted_buy(uid, "tax", prior_buy)
            return
        await save_economy(uid)
        activated_at = time.time()
        expires_ts = int(activated_at + SHOP_TAX_DURATION_SECS)
        tax_data = {"master": uid, "type": tax_type, "emoji": tax_emoji, "channel_id": ctx.channel.id, "activated_at": activated_at, "expires_at": expires_ts}
        state.active_taxes[(gid, target.id)] = tax_data
        await save_tax(state.active_taxes)
        label = tax_type.capitalize()
        await ctx.send(embed=emb(
            f"{tax_emoji} {label} Tax Activated",
            f"**{target.display_name}** now owes **{ctx.author.display_name}** **{SHOP_TAX_PER_MESSAGE:,} 🪙** per message! Expires <t:{expires_ts}:R>.",
            C_PURPLE,
        ))

    # ── !shop curse ───────────────────────────────────────────────────────────
    @cmd_shop.command(name="curse",
                      help=f"Curse a user's next {SHOP_CURSE_MESSAGES} messages; social insurance blocks it",
                      usage="@user")
    @_shop_subcommand(None)
    async def shop_curse(self, ctx: commands.Context, *args):
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
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return
        gid = ctx.guild.id
        if self.bot and self.bot.user and target.id == self.bot.user.id:
            await ctx.send(embed=emb("❌ Invalid Target", "You can't curse the bot.", C_RED))
            return
        if await is_insured(target.id, "curse"):
            _exp = get_insurance_expiry(target.id, "social")
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has social insurance against curses (expires <t:{_exp}:R>).", C_GOLD))
            return
        if _targeted_buy_used(uid, "curse"):
            await ctx.send(embed=_once_a_day_embed("curse"))
            return
        cost = 0 if uid in state.godmode_users else SHOP_CURSE_COST
        if not await confirm_purchase(
            ctx, title="🔮 Curse",
            description=f"Curse **{target.display_name}**'s next {SHOP_CURSE_MESSAGES} messages.",
            cost=cost, payer=ctx.author,
        ):
            return
        claimed, prior_buy = await _claim_targeted_buy(ctx, "curse")
        if not claimed:
            return
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_CURSE_COST:,}"):
            await _release_targeted_buy(uid, "curse", prior_buy)
            return
        await save_economy(uid)
        state.active_curses[(gid, target.id)] = {"cursed_by": uid, "remaining": SHOP_CURSE_MESSAGES, "channel_id": ctx.channel.id}
        await save_curse(state.active_curses)
        await ctx.send(embed=emb(
            "🔮 Curse Activated",
            f"**{target.display_name}** is now cursed for the next **{SHOP_CURSE_MESSAGES}** messages!",
            C_PURPLE,
        ))

    # ── !shop unoreverse ──────────────────────────────────────────────────────
    @cmd_shop.command(name="unoreverse",
                      help="Redirect the mock, ragebait or curse active on you onto someone else; social insurance blocks it",
                      usage="@user")
    @_shop_subcommand(None)
    async def shop_unoreverse(self, ctx: commands.Context, *args):
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
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return
        gid = ctx.guild.id
        self_key = (gid, uid)
        target_key = (gid, target.id)
        # Non-destructive presence check for the usage/confirm messages — the
        # actual claim (pop) happens after the confirm prompt.
        active_names = [
            name for name, effects in (
                ("mock", state.active_mocks),
                ("ragebait", state.active_ragebaits),
                ("curse", state.active_curses),
            ) if self_key in effects
        ]
        if not active_names:
            await ctx.send(embed=emb("🔄 Uno Reverse", "You don't have any active mock, ragebait, or curse on you to reverse!", C_GREY))
            return

        if await is_insured(target.id,"mock"):
            _exp = get_insurance_expiry(target.id, "social")
            await ctx.send(embed=emb(
                "🛡️ Protected",
                f"**{target.display_name}** has social insurance and can't be targeted (expires <t:{_exp}:R>).",
                C_GOLD,
            ))
            return
        cost = 0 if uid in state.godmode_users else SHOP_UNOREVERSE_COST
        if not await confirm_purchase(
            ctx, title="🔄 Uno Reverse",
            description=f"Redirect your active {', '.join(active_names)} onto **{target.display_name}**.",
            cost=cost, payer=ctx.author,
        ):
            return

        # Claim the active effects synchronously so a concurrent
        # !shop unoreverse can't both pay the cost AND crash on .pop(KeyError)
        # when it tries to redirect the same effect a second time.
        mock_data = state.active_mocks.pop(self_key, None)
        rage_data = state.active_ragebaits.pop(self_key, None)
        curse_data = state.active_curses.pop(self_key, None)
        if mock_data is None and rage_data is None and curse_data is None:
            # The effects ran out (or were reversed) during the confirm wait.
            await ctx.send(embed=emb("🔄 Uno Reverse", "You don't have any active mock, ragebait, or curse on you to reverse! You were not charged.", C_GREY))
            return

        def _restore_claimed():
            if mock_data is not None:
                state.active_mocks[self_key] = mock_data
            if rage_data is not None:
                state.active_ragebaits[self_key] = rage_data
            if curse_data is not None:
                state.active_curses[self_key] = curse_data

        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_UNOREVERSE_COST:,}"):
            _restore_claimed()
            return
        redirected = []
        if mock_data is not None:
            mock_data["started_by"] = uid
            state.active_mocks[target_key] = mock_data
            await save_mock()
            redirected.append("mock")
        if rage_data is not None:
            rage_data["history"] = []
            state.active_ragebaits[target_key] = rage_data
            await save_ragebait()
            redirected.append("ragebait")
        if curse_data is not None:
            curse_data["cursed_by"] = uid
            state.active_curses[target_key] = curse_data
            await save_curse(state.active_curses)
            redirected.append("curse")
        await ctx.send(embed=emb(
            "🔄 Uno Reverse!",
            f"**{ctx.author.display_name}** reversed {', '.join(redirected)} onto **{target.display_name}**!",
            C_PURPLE,
        ))

    # ── !shop buyxp ───────────────────────────────────────────────────────────
    #
    # Buys FULL level bands (_xp_cost, not the remaining diff), at
    # SHOP_XP_COST_PER_XP coins per XP. Band costs are strictly increasing, so
    # granting whole bands always lands exactly on the target display level
    # with in-band progress preserved. No argument buys one band (next level);
    # `lvl <x>` / `lvl<x>` buys every band up to display level x.
    #
    # Catch-up discount: bands below the user's best level in another server
    # cost SHOP_XP_CATCHUP_COST_PER_XP instead. Only those bands — discounting
    # everything whenever you're behind somewhere would let two servers
    # leapfrog each other at half price forever.
    @cmd_shop.command(name="buyxp", aliases=["xp"],
                      help="Buy your next level's worth of XP, or every level up to lvl x; the price scales with level",
                      usage="[lvl <x>]")
    @_shop_subcommand("buyxp")
    async def shop_buyxp(self, ctx: commands.Context, *args: str):
        uid = ctx.author.id
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "XP is per-server — use this in a server.", C_RED))
            return
        gid = ctx.guild.id

        target_display = None
        if args:
            m = re.fullmatch(r"(?:lvl|level)\s*(\d{1,4})", " ".join(args).strip().lower())
            if not m:
                await ctx.send(embed=emb(
                    "✨ Buy XP",
                    "Usage: `!buyxp` — buy your next level\n`!buyxp lvl <x>` — buy every level up to lvl x",
                    C_GREY,
                ))
                return
            target_display = int(m.group(1))

        key = (gid, uid)
        if key in self._buyxp_active:
            await ctx.send(embed=emb("⏳ Purchase Pending", "Finish your current XP purchase first.", C_GREY))
            return
        self._buyxp_active.add(key)
        try:
            rec = _ensure_lvl_record(gid, uid)
            quoted_level = rec["level"]
            if target_display is None:
                target_internal = quoted_level + 1
            else:
                if target_display <= display_level(quoted_level):
                    await ctx.send(embed=emb(
                        "✨ Buy XP",
                        f"You're already lvl **{display_level(quoted_level)}** — pick a higher target.",
                        C_GREY,
                    ))
                    return
                target_internal = target_display - 1
            levels = target_internal - quoted_level

            def _quote() -> tuple[int, int, int, int]:
                """(xp, discounted xp, cost, best internal level elsewhere)."""
                best = best_level_elsewhere(gid, uid)
                xp = xp_for_level(target_internal) - xp_for_level(quoted_level)
                catchup_top = min(target_internal, best)
                catchup = max(0, xp_for_level(catchup_top) - xp_for_level(quoted_level))
                price = ((xp - catchup) * SHOP_XP_COST_PER_XP
                         + catchup * SHOP_XP_CATCHUP_COST_PER_XP)
                return xp, catchup, price, best

            xp_amount, catchup_xp, cost, best_elsewhere = _quote()
            godmode = uid in state.godmode_users

            if not godmode and await get_balance(uid) < cost:
                what = ("Your next level" if levels == 1
                        else f"Reaching lvl **{display_level(target_internal)}**")
                await ctx.send(embed=emb(
                    "💸 Insufficient Funds",
                    f"{what} costs **{xp_amount:,} XP** = **{cost:,} 🪙**.",
                    C_RED,
                ))
                return

            projected_xp = rec["xp"] + xp_amount
            projected_level = level_from_xp(projected_xp)
            xp_in_level = projected_xp - xp_for_level(projected_level)
            xp_band = _xp_cost(projected_level)
            level_note = f" (**{levels}** levels)" if levels > 1 else ""
            catchup_note = (
                f"🏷️ **{SHOP_XP_CATCHUP_DISCOUNT_PCT}% off** — you're lvl "
                f"**{display_level(best_elsewhere)}** in another server"
            )
            if catchup_xp == xp_amount:
                price_line = (
                    f"Buy **{xp_amount:,} XP** at {SHOP_XP_CATCHUP_COST_PER_XP} 🪙/XP"
                    f"{level_note} ({catchup_note})."
                )
            elif catchup_xp:
                price_line = (
                    f"Buy **{xp_amount:,} XP**{level_note}: "
                    f"**{catchup_xp:,} XP** at {SHOP_XP_CATCHUP_COST_PER_XP} 🪙/XP "
                    f"({catchup_note}) + **{xp_amount - catchup_xp:,} XP** "
                    f"at {SHOP_XP_COST_PER_XP} 🪙/XP."
                )
            else:
                price_line = f"Buy **{xp_amount:,} XP** at {SHOP_XP_COST_PER_XP} 🪙/XP{level_note}."
            confirmed = await confirm_purchase(
                ctx, title="✨ Buy XP",
                description=(
                    f"{price_line}\n"
                    f"After purchase, you will be lvl **{display_level(projected_level)}** "
                    f"with **{xp_in_level:,}/{xp_band:,}** xp."
                ),
                cost=cost, payer=ctx.author,
            )
            if not confirmed:
                return

            # The confirm wait is a long await — the quoted price is stale if
            # the user levelled meanwhile (organic XP) or their best level in
            # another server moved the discount. Re-quote, don't charge.
            if rec["level"] != quoted_level or _quote()[2] != cost:
                await ctx.send(embed=emb(
                    "⚠️ Price Changed",
                    "Your quote changed while confirming — run it again for a fresh one. You were not charged.",
                    C_GREY,
                ))
                return

            if not await shop_charge(ctx, uid, cost):
                return

            old_level = rec["level"]
            rec["xp"] += xp_amount
            new_level = level_from_xp(rec["xp"])
            rec["level"] = new_level
            leveled = new_level - old_level
            if leveled > 0:
                await record_levelup(gid, uid, count=leveled)
                # Reaching display level 10 (internal 9) latches crime
                # eligibility, as grant_xp does for organic XP.
                if old_level < 9 <= new_level:
                    from src.economy import _ensure_user as _eu
                    from src.persistence import save_economy as _save
                    await _eu(uid)
                    user = state.economy["users"][str(uid)]
                    if not user.get("crime_eligible"):
                        user["crime_eligible"] = True
                        await _save(uid=uid)
            await save_leveling(guild_id=gid, uid=uid)

            saved = catchup_xp * (SHOP_XP_COST_PER_XP - SHOP_XP_CATCHUP_COST_PER_XP)
            saved_line = f"\n🏷️ Catch-up discount saved you **{saved:,} 🪙**." if saved else ""
            await ctx.send(embed=emb(
                "✨ XP Purchased",
                f"+**{xp_amount:,} XP** — you are now **Level {display_level(new_level)}**.{saved_line}",
                C_GREEN,
            ))

            # Full level-up treatment (announcement + levelup_coin_reward) —
            # but not for godmode: a free buy plus the coin reward would be a
            # money printer (same rationale as shop_payout's godmode rule).
            if leveled > 0 and not godmode:
                lvl_cog = self.bot.get_cog("LevelingCog") if self.bot else None
                if lvl_cog:
                    await lvl_cog._announce_levelup(ctx.author, gid)
        finally:
            self._buyxp_active.discard(key)

    # ── !shop roleup / roledown ───────────────────────────────────────────────
    #
    # Source of truth for rank is state.bot_role_ranks[(guild_id, role_id)].
    # 1 = highest, larger = lower. The swap finds the bot role with the
    # next-higher (roleup) or next-lower (roledown) rank *within the same
    # guild* and swaps the two ranks atomically in memory, then mirrors to
    # Discord's role.position best-effort.
    #
    # Discord's role.position can't drive the swap: it counts every server
    # role, not just bot roles, so a bot role with no other bot role above it
    # would jump straight to the top of the bot pile in one move ("#3 → #1").
    @cmd_shop.command(name="roleup", aliases=["roledown"],
                      help="Move a bot-created role up (roleup) or down (roledown) one rank",
                      usage="<@role|role id>")
    @_shop_subcommand("dynamic")
    async def shop_roleup(self, ctx: commands.Context, *args):
        uid = ctx.author.id

        direction = ctx.invoked_with  # "roleup" or "roledown"
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

        cost = 0 if uid in state.godmode_users else SHOP_ROLE_MOVE_COST
        if not await confirm_purchase(
            ctx, title="👑 Move Role",
            description=f"Move **{role.name}** {'up' if direction == 'roleup' else 'down'} one position.",
            cost=cost, payer=ctx.author,
        ):
            return
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_ROLE_MOVE_COST:,}"):
            return

        # Compute the ranks AFTER the charge await: a concurrent roleup on the
        # same role could otherwise change them mid-charge, and applying a
        # stale swap means paying twice for one move. Validation failures
        # past this point refund the charge.
        my_rank = state.bot_role_ranks.get((ctx.guild.id, role.id))
        if my_rank is None:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Unranked", f"**{role.name}** has no rank yet. Ask an admin to seed it.", C_RED))
            return

        # All other (rank, role_id) pairs in this guild.
        guild_ranks = [
            (rank, rid) for (g, rid), rank in state.bot_role_ranks.items()
            if g == ctx.guild.id and rid != role.id
        ]
        if direction == "roleup":
            # The role *above* this one has the next-lower rank number.
            candidates = [(r, rid) for r, rid in guild_ranks if r < my_rank]
            if not candidates:
                if cost > 0:
                    await add_balance(uid, cost)
                await ctx.send(embed=emb("❌ Already Highest", f"**{role.name}** is already the highest bot-created role.", C_RED))
                return
            neighbor_rank, neighbor_id = max(candidates, key=lambda x: x[0])
        else:
            candidates = [(r, rid) for r, rid in guild_ranks if r > my_rank]
            if not candidates:
                if cost > 0:
                    await add_balance(uid, cost)
                await ctx.send(embed=emb("❌ Already Lowest", f"**{role.name}** is already the lowest bot-created role.", C_RED))
                return
            neighbor_rank, neighbor_id = min(candidates, key=lambda x: x[0])

        # Swap both ranks synchronously, before any await, so a sibling
        # invocation never sees half a swap (see CLAUDE.md on per-user
        # command races).
        state.bot_role_ranks[(ctx.guild.id, role.id)] = neighbor_rank
        state.bot_role_ranks[(ctx.guild.id, neighbor_id)] = my_rank

        try:
            await save_bot_roles()
        except Exception as e:
            state.bot_role_ranks[(ctx.guild.id, role.id)] = my_rank
            state.bot_role_ranks[(ctx.guild.id, neighbor_id)] = neighbor_rank
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", f"Could not persist rank swap: {e}", C_RED))
            return

        # Best-effort mirror to Discord's role.position so the server's
        # role sidebar matches the ranking. Failure here doesn't roll back
        # the DB swap — the bot-side rank is authoritative.
        neighbor_role = ctx.guild.get_role(neighbor_id)
        if neighbor_role is not None:
            try:
                role_pos = role.position
                neighbor_pos = neighbor_role.position
                await role.edit(position=neighbor_pos)
                await neighbor_role.edit(position=role_pos)
            except (discord.Forbidden, discord.HTTPException):
                pass  # rank in DB is still correct; sidebar may look stale

        # Rank numbers have gaps after deletes — show the position in the
        # sorted list instead.
        guild_all_ranks = sorted(
            r for (g, _rid), r in state.bot_role_ranks.items() if g == ctx.guild.id
        )
        display_rank = guild_all_ranks.index(neighbor_rank) + 1
        total = len(guild_all_ranks)
        label = "up" if direction == "roleup" else "down"
        await ctx.send(embed=emb("✅ Role Moved", f"Role **{role.name}** moved {label} — now **#{display_rank}** of {total}.", C_GREEN))

    @commands.command(name="roles", aliases=["rolelb", "lbroles", "lbr"],
                      help="Rank this server's bot-created roles, highest first, with locked ones marked")
    async def cmd_roles(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        await send_rankings(self, ctx, "roles")

    def roles_embed(self, ctx: commands.Context) -> discord.Embed:
        candidates: list = []
        for r in ctx.guild.roles:
            if r.id not in state.bot_roles:
                continue
            rank = state.bot_role_ranks.get((ctx.guild.id, r.id))
            # Ranked roles first (1 = highest); unranked ones after, higher
            # Discord position first.
            sort_key = (0, rank) if rank is not None else (1, -r.position)
            candidates.append((sort_key, r))
        if not candidates:
            return emb("👑 Role Leaderboard", "No bot-created roles in this server yet.", C_PURPLE)
        candidates.sort(key=lambda x: x[0])
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, (_k, role) in enumerate(candidates):
            prefix = medals[i] if i < 3 else f"{i + 1}."
            lock = " 🔒" if role.id in state.locked_roles else ""
            lines.append(f"{prefix} **{role.name}**{lock}")
        return emb("👑 Role Leaderboard", "\n".join(lines), C_PURPLE)


async def setup(bot):
    await bot.add_cog(ShopCog(bot))
