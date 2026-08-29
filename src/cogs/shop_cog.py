import asyncio
import functools
import time
import datetime
import re

import aiohttp
import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_PURPLE, C_GREY,
    send_ephemeral,
    fetch_member, shop_charge, log_bot_permission_error, MemberConverter,
    announce_record,
)

from src.economy import (
    add_balance, get_balance, is_insured, get_insurance_expiry,
    extend_insurance,
)
from src.leveling import (
    _ensure_lvl_record, _xp_cost, level_from_xp, display_level, record_levelup,
)
from src.permissions import (
    _wrong_channel_reply,
)
from src.persistence import (
    save_insurance, save_insurance_subs, save_guild_settings,
    save_bot_roles,
    save_ragebait, save_mock, save_tax, save_curse, save_spellcheck,
    save_user_artifact, try_set_record, save_leveling,
)
from src.artifacts import ARTIFACTS, owned_qty, owned_artifact_count
from src.confirm_view import confirm_prompt, confirm_purchase
from src.guild_config import get_guild_cfg
from src.ai import (
    keep_typing,
    stream_ollama, finalize,
)
from src.config import (
    SHOP_NICKNAME_SELF_COST, SHOP_NICKNAME_REMOVE_COST, SHOP_NICKNAME_OTHER_COST,
    SHOP_ROLE_CREATE_COST, SHOP_ROLE_ASSIGN_COST, SHOP_ROLE_REMOVE_COST, SHOP_ROLE_DELETE_COST, SHOP_ROLE_MOVE_COST,
    SHOP_ROLECOLOR_COST, SHOP_ROLECHANNEL_COST, SHOP_LOCK_COST, SHOP_RENAME_COST, SHOP_CHANNEL_COST, SHOP_CHANNEL_DELETE_COST, SHOP_INSURANCE_COST, SHOP_TAX_COST,
    SHOP_MOCK_COST, SHOP_RAGEBAIT_COST, SHOP_MUTE_COST, SHOP_CURSE_COST, SHOP_UNOREVERSE_COST,
    SHOP_INSURANCE_DURATION_SECS, SHOP_INSURANCE_MAX_DAYS, SHOP_MOCK_MESSAGES, SHOP_RAGEBAIT_MESSAGES,
    SHOP_CURSE_MESSAGES, SHOP_MUTE_MINUTES, SHOP_TAX_PER_MESSAGE,
    SHOP_TAX_DURATION_SECS,
    SHOP_SPELLCHECK_COST, SHOP_SPELLCHECK_DURATION_SECS,
    SHOP_XP_COST_PER_XP,
    BOUNTY_MIN_AMOUNT,
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
        # functools.wraps copies __qualname__ from `func` (e.g. "ShopCog.shop_insurance"),
        # which is load-bearing: discord.py's get_signature_parameters calls
        # is_inside_class(callback) to decide how many leading params to skip
        # (self + ctx for methods, ctx only for plain functions). If wrapper keeps its
        # local qualname ("_shop_subcommand.<locals>.deco.<locals>.wrapper"),
        # is_inside_class returns False, only one param is skipped, and discord.py
        # treats `ctx` as a user-supplied arg — causing MissingRequiredArgument /
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


# Top-level command names that should map to a !shop subcommand of the same
# shape (same args, same body). Keeps a !shop subcommand and a !alias top-level
# command in lockstep without 30 hand-written wrappers.
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
    ("mute",           "shop_mute",          []),
    ("tax",            "shop_tax",           []),
    ("curse",          "shop_curse",         []),
    ("spellcheck",     "shop_spellcheck",    []),
    ("unoreverse",     "shop_unoreverse",    []),
    ("buyxp",          "shop_buyxp",         []),
    ("artifacts",      "shop_artifacts",     ["artifact"]),
    # roleup and roledown both dispatch via shop_roleup (it reads
    # ctx.invoked_with to pick direction).
    ("roleup",         "shop_roleup",        []),
    ("roledown",       "shop_roleup",        []),
]


class ShopCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # (guild_id, user_id) pairs with an XP purchase confirm in flight —
        # the confirm prompt is a long await, so gate re-entry explicitly.
        self._buyxp_active: set = set()
        # Register declarative top-level aliases for shop subcommands. Tests
        # instantiate ShopCog(bot=None) to exercise subcommand handlers
        # directly, so skip registration when there's no bot to attach to.
        if bot is not None:
            for top_name, sub_attr, legacy_aliases in _SHOP_TOP_ALIASES:
                sub_cmd = getattr(self, sub_attr)
                # cog=self is load-bearing: the callback's __qualname__ is
                # ShopCog.shop_X (preserved by functools.wraps in
                # _shop_subcommand), so discord.py's _parse_arguments builds
                # ctx.args as [cog, ctx, ...]. Without cog set, it builds
                # [ctx, ...] and the wrapper's `self` slot binds to the real
                # Context, shifting every other arg by one — `ctx` then ends
                # up bound to the first user arg (a string), and the first
                # `.guild` access dies with 'str' has no attribute 'guild'.
                alias_cmd = commands.Command(sub_cmd.callback, name=top_name, aliases=legacy_aliases)
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
            items.append((SHOP_ROLECOLOR_COST, L("rolecolor", f"`!shop rolecolor @role <color>` — Change a role's color — **{SHOP_ROLECOLOR_COST:,} 🪙**")))
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
            items.append((SHOP_LOCK_COST, L("channellock", f"`!shop channellock #channel` — Lock a channel against changes — **{SHOP_LOCK_COST:,} 🪙**")))
        if shop_items.get("rolechannel", True):
            items.append((SHOP_ROLECHANNEL_COST, L("rolechannel", f"`!shop rolechannel @role #channel` — Restrict a channel to a role — **{SHOP_ROLECHANNEL_COST:,} 🪙**")))
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
            (SHOP_INSURANCE_COST, f"`!shop insurance [days|sub|unsub]` — Protection, prepaid or subscribed (renews with your daily) — **{SHOP_INSURANCE_COST:,} 🪙/day**"),
            (SHOP_TAX_COST,      f"`!shop tax @user` — Apply a per-message tax to a user for 24h — **{SHOP_TAX_COST:,} 🪙**"),
            (SHOP_MOCK_COST,      f"`!shop mock @user` — Mock someone's next {SHOP_MOCK_MESSAGES} messages — **{SHOP_MOCK_COST:,} 🪙**"),
        ]
        if _si.get("ragebait", True):
            fun_items.append((SHOP_RAGEBAIT_COST, f"`!shop ragebait @user [topic]` — Ragebait for {SHOP_RAGEBAIT_MESSAGES + 1} messages — **{SHOP_RAGEBAIT_COST:,} 🪙**"))
        fun_items.append((SHOP_MUTE_COST,  f"`!shop mute @user` — Server mute for {SHOP_MUTE_MINUTES} minutes — **{SHOP_MUTE_COST:,} 🪙**"))
        fun_items.append((SHOP_CURSE_COST, f"`!shop curse @user` — Curse someone's messages for {SHOP_CURSE_MESSAGES} messages — **{SHOP_CURSE_COST:,} 🪙**"))
        fun_items.append((SHOP_SPELLCHECK_COST, f"`!shop spellcheck @user [days]` — AI corrects their messages — **{SHOP_SPELLCHECK_COST:,} 🪙/day**"))
        fun_items.append((SHOP_UNOREVERSE_COST, L("unoreverse", f"`!shop unoreverse @user` — Redirect active mock/ragebait/curse onto someone else — **{SHOP_UNOREVERSE_COST:,} 🪙**")))
        fun_items.sort(key=lambda x: x[0])
        sections["🎉 Fun & Social"] = [item[1] for item in fun_items]

        # Leveling — variable price, quoted (and confirmed) at purchase time.
        if _si.get("buyxp", True):
            sections["✨ Leveling"] = [
                f"`!shop buyxp` — Buy your next level's worth of XP — **{SHOP_XP_COST_PER_XP} 🪙/XP** (price scales with level)"
            ]

        # Artifacts — permanent per-user upgrades, listed in their own menu.
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

        if not sections:
            await send_ephemeral(ctx, embed=emb("🛒 Shop", "No shop items are currently available.", C_PURPLE))
            return

        desc = "\n\n".join(f"**{section}**\n" + "\n".join(items) for section, items in sections.items())
        await send_ephemeral(ctx, embed=emb("🛒 Shop", desc, C_PURPLE))

    # ── !shop roles ───────────────────────────────────────────────────────────
    @cmd_shop.command(name="roles", aliases=["role"])
    @_shop_subcommand(None)
    async def shop_roles(self, ctx: commands.Context):
        _si = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}
        _gid = ctx.guild.id if ctx.guild else 0
        lines = self._role_section_lines(_si, ctx.author.id, _gid)
        if not lines:
            await send_ephemeral(ctx, embed=emb("👑 Role Shop", "No role shop items are currently available.", C_PURPLE))
            return
        await send_ephemeral(ctx, embed=emb("👑 Role Shop", "\n".join(lines), C_PURPLE))

    # ── !shop channels ────────────────────────────────────────────────────────
    @cmd_shop.command(name="channels", aliases=["channel"])
    @_shop_subcommand(None)
    async def shop_channels(self, ctx: commands.Context):
        _si = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}
        _gid = ctx.guild.id if ctx.guild else 0
        lines = self._channel_section_lines(_si, ctx.author.id, _gid)
        if not lines:
            await send_ephemeral(ctx, embed=emb("📢 Channel Shop", "No channel shop items are currently available.", C_PURPLE))
            return
        await send_ephemeral(ctx, embed=emb("📢 Channel Shop", "\n".join(lines), C_PURPLE))

    # ── !shop artifacts ───────────────────────────────────────────────────────
    @cmd_shop.command(name="artifacts")
    @_shop_subcommand(None)
    async def shop_artifacts(self, ctx: commands.Context, *args):
        from src.level_unlocks import user_display_level

        uid = ctx.author.id
        gid = ctx.guild.id if ctx.guild else 0
        lvl = user_display_level(uid, gid)

        if not args:
            lines = []
            for i, art in enumerate(ARTIFACTS, start=1):
                req = art.get("level", 1)
                if owned_qty(uid, art["id"]) >= art["max"]:
                    lines.append(f"**{i}.** {art['effect']} — ✅ **Owned**")
                elif lvl < req:
                    lines.append(f"~~**{i}.** {art['effect']} — **{art['cost']:,} 🪙**~~ 🔒 **Lvl {req}**")
                else:
                    lines.append(f"**{i}.** {art['effect']} — **{art['cost']:,} 🪙**")
            lines.append("")
            lines.append("Artifacts are permanent and yours forever. Buy one with `!artifacts buy <number>`.")
            await send_ephemeral(ctx, embed=emb("🏺 Artifacts", "\n".join(lines), C_PURPLE))
            return

        if args[0].lower() != "buy" or len(args) < 2:
            await ctx.send(embed=emb("🏺 Artifacts", "Usage: `!artifacts` to browse, `!artifacts buy <number>` to buy.", C_PURPLE))
            return
        try:
            idx = int(args[1])
        except ValueError:
            idx = 0
        if not 1 <= idx <= len(ARTIFACTS):
            await ctx.send(embed=emb("❌ Invalid Artifact", f"Pick a number between 1 and {len(ARTIFACTS)} (see `!artifacts`).", C_RED))
            return
        art = ARTIFACTS[idx - 1]

        req = art.get("level", 1)
        if lvl < req:
            await ctx.send(embed=emb("🔒 Level Locked", f"That artifact unlocks at **Level {req}** — you're Level {lvl}.", C_RED))
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
        if not await shop_charge(ctx, uid, cost, cost_label=f"{art['cost']:,}"):
            if prior:
                owned[art["id"]] = prior
            else:
                owned.pop(art["id"], None)
            return
        await save_user_artifact(uid, art["id"], prior + 1)
        await ctx.send(embed=emb("🏺 Artifact Acquired", f"Its power is now yours: {art['effect'].lower()}.", C_GREEN))

        # Artifacts are global but records are per-guild: the "most artifacts
        # owned" record lands in whichever guild the purchase happened in,
        # same as every other category.
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
    @cmd_shop.command(name="nickname")
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
        if target.id != uid and ctx.guild and await is_insured(ctx.guild.id, target.id, "nickname"):
            _exp = get_insurance_expiry(ctx.guild.id, target.id)
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance and can't be renamed (expires <t:{_exp}:R>).", C_GOLD))
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
    @cmd_shop.command(name="removenickname")
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
    @cmd_shop.command(name="rolecreate", aliases=["createrole"])
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
            target = await MemberConverter().convert(ctx, args[0])
        except commands.BadArgument:
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
        if target.id != uid and ctx.guild and await is_insured(ctx.guild.id, target.id, "role"):
            _exp = get_insurance_expiry(ctx.guild.id, target.id)
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance and can't be given new roles (expires <t:{_exp}:R>).", C_GOLD))
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
            await target.add_roles(new_role)
            state.bot_roles.add(new_role.id)
            # New roles go to the bottom of the rank ladder for this guild
            # (max rank + 1 — lowest priority). Operators promote them via
            # !shop roleup.
            existing_ranks = [r for (g, _r), r in state.bot_role_ranks.items() if g == ctx.guild.id]
            new_rank = (max(existing_ranks) + 1) if existing_ranks else 1
            state.bot_role_ranks[(ctx.guild.id, new_role.id)] = new_rank
            await save_bot_roles()
            await ctx.send(embed=emb("✅ Role Created", f"Role **{name}** created and assigned to **{target.display_name}** — rank **#{new_rank}**. Give it a color with `!shop rolecolor @{name} <hex>`.", C_GREEN))
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
    @cmd_shop.command(name="roleassign", aliases=["assignrole"])
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
        if target.id != uid and ctx.guild and await is_insured(ctx.guild.id, target.id, "role"):
            _exp = get_insurance_expiry(ctx.guild.id, target.id)
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance and can't be given new roles (expires <t:{_exp}:R>).", C_GOLD))
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
    @cmd_shop.command(name="roleunassign", aliases=["unassignrole"])
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
        if member.id != uid and ctx.guild and await is_insured(ctx.guild.id, member.id, "role"):
            _exp = get_insurance_expiry(ctx.guild.id, member.id)
            await ctx.send(embed=emb("🛡️ Protected", f"**{member.display_name}** has insurance and their roles can't be changed (expires <t:{_exp}:R>).", C_GOLD))
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
    @cmd_shop.command(name="roledelete", aliases=["deleterole"])
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
            if ctx.guild and await is_insured(ctx.guild.id, m.id, "role"):
                insured_members.append(m)
        if insured_members:
            names = ", ".join(f"**{m.display_name}**" for m in insured_members)
            _earliest = min(get_insurance_expiry(ctx.guild.id, m.id) for m in insured_members)
            await ctx.send(embed=emb("🛡️ Protected", f"{names} {'has' if len(insured_members) == 1 else 'have'} insurance — this role can't be deleted (expires <t:{_earliest}:R>).", C_GOLD))
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
            # Drop the rank entry too. Gaps are fine — the rank ladder
            # doesn't get compacted, so adjacent ranks may have non-adjacent
            # numbers after deletes.
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
    @cmd_shop.command(name="channelcreate", aliases=["createchannel"])
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
    @cmd_shop.command(name="channeldelete", aliases=["deletechannel"])
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
    @cmd_shop.command(name="channelrename", aliases=["renamechannel"])
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
        if target_channel is None or not isinstance(target_channel, discord.TextChannel):
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
    @cmd_shop.command(name="rolerename", aliases=["renamerole"])
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
    @cmd_shop.command(name="rolechannel")
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
        if target_channel is None or not isinstance(target_channel, discord.TextChannel):
            await ctx.send(embed=emb("❌ Not Found", "Could not find that channel. Use a #mention or channel ID.", C_RED))
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
    @cmd_shop.command(name="channellock", aliases=["lockchannel"])
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
        if target_channel is None:
            await ctx.send(embed=emb("❌ Not Found", "Could not find that channel. Use a #mention or channel ID.", C_RED))
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
    @cmd_shop.command(name="channelunlock", aliases=["unlockchannel"])
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
    @cmd_shop.command(name="rolelock", aliases=["lockrole"])
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
    @cmd_shop.command(name="roleunlock", aliases=["unlockrole"])
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
    @cmd_shop.command(name="ragebait")
    @_shop_subcommand("ragebait")
    async def shop_ragebait(self, ctx: commands.Context, *args):
        uid = ctx.author.id
        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop ragebait @user [topic]`", C_PURPLE))
            return
        try:
            target = await MemberConverter().convert(ctx, args[0])
        except commands.BadArgument:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop ragebait @user [topic]`", C_PURPLE))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return
        gid = ctx.guild.id
        if target.id != uid and await is_insured(gid, target.id, "ragebait"):
            _exp = get_insurance_expiry(gid, target.id)
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance against ragebait (expires <t:{_exp}:R>).", C_GOLD))
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
                ], placeholder, user_id=uid)
            if not full_response:
                # AI disabled or token budget denied — stream_ollama already
                # edited the placeholder with the reason. Refund; don't
                # activate the effect or post a bare mention.
                if cost > 0:
                    await add_balance(uid, cost)
                return
            await finalize(placeholder, ctx.channel, f"{target.mention} {full_response}")
            state.active_ragebaits[(gid, target.id)] = {"remaining": SHOP_RAGEBAIT_MESSAGES, "history": [], "channel_id": ctx.channel.id}
            await save_ragebait()
        except Exception as e:
            if cost > 0:
                await add_balance(uid, cost)
            await placeholder.edit(content=f"⚠️ {e}")
        finally:
            typing_task.cancel()

    # ── !shop mock ────────────────────────────────────────────────────────────
    @cmd_shop.command(name="mock")
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
        if target.id != uid and await is_insured(gid, target.id, "mock"):
            _exp = get_insurance_expiry(gid, target.id)
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance against mock (expires <t:{_exp}:R>).", C_GOLD))
            return
        cost = 0 if uid in state.godmode_users else SHOP_MOCK_COST
        if not await confirm_purchase(
            ctx, title="🎭 Mock",
            description=f"Mock **{target.display_name}**'s next {SHOP_MOCK_MESSAGES} messages.",
            cost=cost, payer=ctx.author,
        ):
            return
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_MOCK_COST:,}"):
            return
        state.active_mocks[(gid, target.id)] = {"remaining": SHOP_MOCK_MESSAGES, "started_by": uid, "channel_id": ctx.channel.id}
        await save_mock()
        await ctx.send(embed=emb(
            "🎭 Mock Activated",
            f"**{target.display_name}** will have their next {SHOP_MOCK_MESSAGES} messages mocked!",
            C_PURPLE,
        ))

    # ── !shop bounty ──────────────────────────────────────────────────────────
    @cmd_shop.command(name="bounty")
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

    # ── !shop insurance ───────────────────────────────────────────────────────
    def _rollback_insurance_days(self, key: tuple, days: int):
        """Undo a stamped-but-unpaid insurance extension. A concurrent purchase
        may have extended on top of our stamp, so subtract our duration rather
        than restoring a snapshot (which would clobber the other buyer)."""
        entry = state.insurance.get(key)
        if entry:
            entry["expires_at"] -= days * SHOP_INSURANCE_DURATION_SECS
            if entry["expires_at"] <= time.time():
                state.insurance.pop(key, None)

    @cmd_shop.command(name="insurance")
    @_shop_subcommand(None)
    async def shop_insurance(self, ctx: commands.Context, arg: str = None):
        uid = ctx.author.id
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return
        key = (ctx.guild.id, uid)
        protects_str = "ragebait, mock, nickname, role assignments, steal, tax, and spellcheck"

        if arg and arg.lower() in ("sub", "subscribe"):
            if key in state.insurance_subs:
                await ctx.send(embed=emb(
                    "🛡️ Already Subscribed",
                    f"You're already subscribed here — **{SHOP_INSURANCE_COST:,} 🪙** is deducted with each daily claim. `!shop insurance unsub` to cancel.",
                    C_GOLD,
                ))
                return
            first_day = get_insurance_expiry(ctx.guild.id, uid) is None
            if not await confirm_prompt(
                ctx, title="🛡️ Insurance Subscription",
                description=(
                    f"Subscribe to insurance — each daily claim deducts **{SHOP_INSURANCE_COST:,} 🪙** "
                    "and adds 24h of coverage."
                    + (" The first day is charged now so coverage starts immediately." if first_day else "")
                ),
                payer=ctx.author,
            ):
                return
            # The confirm wait is a long await — a concurrent subscribe may
            # have landed during the prompt.
            if key in state.insurance_subs:
                await ctx.send(embed=emb(
                    "🛡️ Already Subscribed",
                    f"You're already subscribed here — **{SHOP_INSURANCE_COST:,} 🪙** is deducted with each daily claim. `!shop insurance unsub` to cancel.",
                    C_GOLD,
                ))
                return
            # Claim the sub synchronously before any await so two concurrent
            # subscribes can't both buy the starter day.
            state.insurance_subs.add(key)
            start_str = ""
            if get_insurance_expiry(ctx.guild.id, uid) is None:
                # Not currently covered — charge the first day now so the
                # subscription protects immediately, not at the next daily.
                cost = 0 if uid in state.godmode_users else SHOP_INSURANCE_COST
                expires_at = extend_insurance(ctx.guild.id, uid, 1)
                if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_INSURANCE_COST:,}"):
                    state.insurance_subs.discard(key)
                    self._rollback_insurance_days(key, 1)
                    return
                await save_insurance()
                start_str = f" First day charged now — covered against {protects_str} (expires <t:{expires_at}:R>)."
            await save_insurance_subs()
            await ctx.send(embed=emb(
                "🛡️ Insurance Subscribed",
                f"Each daily claim now deducts **{SHOP_INSURANCE_COST:,} 🪙** and adds 24h of coverage.{start_str} "
                f"Cancel anytime with `!shop insurance unsub`.",
                C_GREEN,
            ))
            return

        if arg and arg.lower() in ("unsub", "unsubscribe"):
            if key not in state.insurance_subs:
                await ctx.send(embed=emb(
                    "🛡️ Not Subscribed",
                    "You don't have an insurance subscription in this server. `!shop insurance sub` to start one.",
                    C_GOLD,
                ))
                return
            state.insurance_subs.discard(key)
            await save_insurance_subs()
            exp = get_insurance_expiry(ctx.guild.id, uid)
            tail = f" Your current coverage still runs out <t:{exp}:R>." if exp else ""
            await ctx.send(embed=emb(
                "🛡️ Insurance Unsubscribed",
                f"Your daily claim will no longer be charged for insurance.{tail}",
                C_GREEN,
            ))
            return

        # Prepay: `!shop insurance [days]` (default 1) at SHOP_INSURANCE_COST/day,
        # stacking on top of any active coverage.
        days = 1
        if arg is not None:
            try:
                days = int(arg)
            except ValueError:
                await ctx.send(embed=emb(
                    "🛡️ Insurance",
                    f"Usage: `!shop insurance [days|sub|unsub]` — prepay coverage at **{SHOP_INSURANCE_COST:,} 🪙/day** "
                    f"(up to {SHOP_INSURANCE_MAX_DAYS} days), or subscribe to auto-renew with your daily claim.",
                    C_PURPLE,
                ))
                return
            if not 1 <= days <= SHOP_INSURANCE_MAX_DAYS:
                await ctx.send(embed=emb(
                    "🛡️ Insurance",
                    f"You can prepay between 1 and {SHOP_INSURANCE_MAX_DAYS} days.",
                    C_RED,
                ))
                return
        now = time.time()
        current_exp = get_insurance_expiry(ctx.guild.id, uid)
        remaining = max(0.0, (current_exp or now) - now)
        if remaining + days * SHOP_INSURANCE_DURATION_SECS > SHOP_INSURANCE_MAX_DAYS * SHOP_INSURANCE_DURATION_SECS:
            buyable = int((SHOP_INSURANCE_MAX_DAYS * SHOP_INSURANCE_DURATION_SECS - remaining) // SHOP_INSURANCE_DURATION_SECS)
            await ctx.send(embed=emb(
                "🛡️ Coverage Capped",
                f"Total coverage can't exceed **{SHOP_INSURANCE_MAX_DAYS} days**. "
                + (f"You can prepay up to **{buyable}** more day{'s' if buyable != 1 else ''} right now." if buyable > 0
                   else "Come back when some of your current coverage has run down."),
                C_GOLD,
            ))
            return

        cost = 0 if uid in state.godmode_users else SHOP_INSURANCE_COST * days
        day_label = f"{days} day{'s' if days != 1 else ''}"
        if not await confirm_purchase(
            ctx, title="🛡️ Insurance",
            description=f"Prepay **{day_label}** of protection against {protects_str}.",
            cost=cost, payer=ctx.author,
        ):
            return
        # The confirm wait is a long await — re-check the coverage cap, since
        # a concurrent purchase (or sub renewal) may have extended coverage
        # during the prompt.
        now = time.time()
        current_exp = get_insurance_expiry(ctx.guild.id, uid)
        remaining = max(0.0, (current_exp or now) - now)
        if remaining + days * SHOP_INSURANCE_DURATION_SECS > SHOP_INSURANCE_MAX_DAYS * SHOP_INSURANCE_DURATION_SECS:
            await ctx.send(embed=emb(
                "🛡️ Coverage Capped",
                f"Total coverage can't exceed **{SHOP_INSURANCE_MAX_DAYS} days** — your coverage "
                "changed while confirming. You were not charged.",
                C_GOLD,
            ))
            return

        # Stamp the extension synchronously before shop_charge (see CLAUDE.md
        # on per-user command races); roll back our days if the charge fails.
        expires_at = extend_insurance(ctx.guild.id, uid, days)
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_INSURANCE_COST * days:,}"):
            self._rollback_insurance_days(key, days)
            return
        await save_insurance()
        sub_str = " Your subscription keeps extending it with each daily claim." if key in state.insurance_subs else ""
        await ctx.send(embed=emb(
            "🛡️ Insurance Purchased",
            f"**{days} day{'s' if days != 1 else ''}** of protection against {protects_str}! "
            f"(expires <t:{expires_at}:R>){sub_str}",
            C_GREEN,
        ))

    # ── !shop rolecolor ───────────────────────────────────────────────────────
    @cmd_shop.command(name="rolecolor")
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

    # ── !shop mute ────────────────────────────────────────────────────────────
    @cmd_shop.command(name="mute")
    @_shop_subcommand(None)
    async def shop_mute(self, ctx: commands.Context, *args):
        uid = ctx.author.id

        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop mute @user`", C_PURPLE))
            return
        try:
            target = await MemberConverter().convert(ctx, args[0])
        except commands.BadArgument:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop mute @user`", C_PURPLE))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return
        cost = 0 if uid in state.godmode_users else SHOP_MUTE_COST
        if not await confirm_purchase(
            ctx, title="🔕 Mute",
            description=f"Server-mute **{target.display_name}** for {SHOP_MUTE_MINUTES} minutes.",
            cost=cost, payer=ctx.author,
        ):
            return
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_MUTE_COST:,}"):
            return
        try:
            member = await fetch_member(ctx.guild, target.id)
            if not member:
                if cost > 0:
                    await add_balance(uid, cost)
                await ctx.send(embed=emb("❌ User Not Found", f"Could not find **{target.display_name}** in this server.", C_RED))
                return
            await member.edit(timed_out_until=discord.utils.utcnow() + datetime.timedelta(minutes=SHOP_MUTE_MINUTES))
            await ctx.send(embed=emb(
                "🔕 Muted",
                f"**{target.display_name}** has been muted for {SHOP_MUTE_MINUTES} minutes!",
                C_PURPLE,
            ))
        except discord.Forbidden:
            if cost > 0:
                await add_balance(uid, cost)
            log_bot_permission_error(ctx, "timeout members")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to timeout members.", C_RED))
        except Exception as e:
            if cost > 0:
                await add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Error", f"Failed to mute: {str(e)}", C_RED))

    # ── !shop tax (+ guild-configured aliases) ───────────────────────────────
    @cmd_shop.command(name="tax")
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
            target = await MemberConverter().convert(ctx, args[0])
        except commands.BadArgument:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop tax @user`", C_PURPLE))
            return
        if target.id == uid:
            await ctx.send(embed=emb("❌ Error", "You can't tax yourself!", C_RED))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return
        gid = ctx.guild.id
        if await is_insured(gid, target.id, "tax"):
            _exp = get_insurance_expiry(gid, target.id)
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance against tax (expires <t:{_exp}:R>).", C_GOLD))
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
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_TAX_COST:,}"):
            return
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
    @cmd_shop.command(name="curse")
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
        cost = 0 if uid in state.godmode_users else SHOP_CURSE_COST
        if not await confirm_purchase(
            ctx, title="🔮 Curse",
            description=f"Curse **{target.display_name}**'s next {SHOP_CURSE_MESSAGES} messages.",
            cost=cost, payer=ctx.author,
        ):
            return
        if not await shop_charge(ctx, uid, cost, cost_label=f"{SHOP_CURSE_COST:,}"):
            return
        state.active_curses[(gid, target.id)] = {"cursed_by": uid, "remaining": SHOP_CURSE_MESSAGES, "channel_id": ctx.channel.id}
        await save_curse(state.active_curses)
        await ctx.send(embed=emb(
            "🔮 Curse Activated",
            f"**{target.display_name}** is now cursed for the next **{SHOP_CURSE_MESSAGES}** messages!",
            C_PURPLE,
        ))

    # ── !shop spellcheck ──────────────────────────────────────────────────────
    @cmd_shop.command(name="spellcheck")
    @_shop_subcommand(None)
    async def shop_spellcheck(self, ctx: commands.Context, *args):
        uid = ctx.author.id

        if not args:
            await ctx.send(embed=emb(
                "🛒 Shop",
                f"Usage: `!shop spellcheck @user [days]` — **{SHOP_SPELLCHECK_COST:,} 🪙** per day (default 1).",
                C_PURPLE,
            ))
            return

        # Optional trailing days arg. If the last token parses as a positive
        # int it's the day count; otherwise treat everything as the target.
        days = 1
        target_args = list(args)
        if len(args) >= 2:
            maybe_days = args[-1]
            if maybe_days.isdigit() and int(maybe_days) > 0:
                days = int(maybe_days)
                target_args = list(args[:-1])

        try:
            target = await MemberConverter().convert(ctx, target_args[0])
        except commands.BadArgument:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop spellcheck @user [days]`", C_PURPLE))
            return

        if target.id == uid:
            await ctx.send(embed=emb("❌ Self Spellcheck", "You can't spellcheck yourself!", C_RED))
            return
        if self.bot and self.bot.user and target.id == self.bot.user.id:
            await ctx.send(embed=emb("❌ Invalid Target", "You can't spellcheck the bot.", C_RED))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return
        gid = ctx.guild.id
        if await is_insured(gid, target.id, "spellcheck"):
            _exp = get_insurance_expiry(gid, target.id)
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance and can't be spellchecked (expires <t:{_exp}:R>).", C_GOLD))
            return

        cost = 0 if uid in state.godmode_users else SHOP_SPELLCHECK_COST * days

        day_label = "1 day" if days == 1 else f"{days} days"
        confirmed = await confirm_purchase(
            ctx,
            title="📝 Spellcheck",
            description=f"Have the AI correct **{target.display_name}**'s messages for **{day_label}**.",
            cost=cost,
            payer=ctx.author,
        )
        if not confirmed:
            return

        if not await shop_charge(ctx, uid, cost, cost_label=f"{cost:,}"):
            return

        activated_at = time.time()
        expires_ts = int(activated_at + days * SHOP_SPELLCHECK_DURATION_SECS)
        state.active_spellchecks[(gid, target.id)] = {
            "started_by": uid,
            "days": days,
            "channel_id": ctx.channel.id,
            "activated_at": activated_at,
            "expires_at": expires_ts,
        }
        await save_spellcheck()
        await ctx.send(embed=emb(
            "📝 Spellcheck Activated",
            f"**{target.display_name}**'s messages will be corrected by the AI for **{day_label}**! Expires <t:{expires_ts}:R>.",
            C_PURPLE,
        ))

    # ── !shop unoreverse ──────────────────────────────────────────────────────
    @cmd_shop.command(name="unoreverse")
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

        if await is_insured(gid, target.id, "mock"):
            _exp = get_insurance_expiry(gid, target.id)
            await ctx.send(embed=emb(
                "🛡️ Protected",
                f"**{target.display_name}** has insurance and can't be targeted (expires <t:{_exp}:R>).",
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
    # Buys the FULL cost of the current level band (_xp_cost, not the
    # remaining diff), at SHOP_XP_COST_PER_XP coins per XP. Band costs are
    # strictly increasing, so granting a full band always lands exactly one
    # display level higher with in-band progress preserved.
    @cmd_shop.command(name="buyxp", aliases=["xp"])
    @_shop_subcommand("buyxp")
    async def shop_buyxp(self, ctx: commands.Context):
        uid = ctx.author.id
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "XP is per-server — use this in a server.", C_RED))
            return
        gid = ctx.guild.id
        key = (gid, uid)
        if key in self._buyxp_active:
            await ctx.send(embed=emb("⏳ Purchase Pending", "Finish your current XP purchase first.", C_GREY))
            return
        self._buyxp_active.add(key)
        try:
            rec = _ensure_lvl_record(gid, uid)
            quoted_level = rec["level"]
            xp_amount = _xp_cost(quoted_level)
            cost = xp_amount * SHOP_XP_COST_PER_XP
            godmode = uid in state.godmode_users

            if not godmode and await get_balance(uid) < cost:
                await ctx.send(embed=emb(
                    "💸 Insufficient Funds",
                    f"Your next level costs **{xp_amount:,} XP** = **{cost:,} 🪙**.",
                    C_RED,
                ))
                return

            confirmed = await confirm_purchase(
                ctx, title="✨ Buy XP",
                description=(
                    f"Buy **{xp_amount:,} XP** at {SHOP_XP_COST_PER_XP} 🪙/XP — "
                    f"takes you to **Level {display_level(quoted_level) + 1}**."
                ),
                cost=cost, payer=ctx.author,
            )
            if not confirmed:
                return

            # The confirm wait is a long await — the quoted price is stale if
            # the user levelled meanwhile (organic XP). Re-quote, don't charge.
            if rec["level"] != quoted_level:
                await ctx.send(embed=emb(
                    "⚠️ Price Changed",
                    "Your level changed while confirming — run it again for a fresh quote. You were not charged.",
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
                if old_level < 9 <= new_level:
                    from src.economy import _ensure_user as _eu
                    from src.persistence import save_economy as _save
                    await _eu(uid)
                    user = state.economy["users"][str(uid)]
                    if not user.get("crime_eligible"):
                        user["crime_eligible"] = True
                        await _save(uid=uid)
            await save_leveling(guild_id=gid, uid=uid)

            await ctx.send(embed=emb(
                "✨ XP Purchased",
                f"+**{xp_amount:,} XP** — you are now **Level {display_level(new_level)}**.",
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
    # Previously the code sorted Discord's role.position (which includes
    # every server role, not just bot roles), so a bot role at Discord
    # position 3 with no other bot roles above it would jump straight to
    # the top of the bot pile in one move — visible as "#3 → #1".
    @cmd_shop.command(name="roleup", aliases=["roledown"])
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

        # Swap the two ranks synchronously before any await — sibling
        # invocations either see the new state and act on it or were
        # already past their own gate (matches the codebase's standard
        # race-avoidance pattern for in-memory mutations).
        state.bot_role_ranks[(ctx.guild.id, role.id)] = neighbor_rank
        state.bot_role_ranks[(ctx.guild.id, neighbor_id)] = my_rank

        try:
            await save_bot_roles()
        except Exception as e:
            # Roll back in-memory swap and refund.
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

        # Compute display rank by counting how many ranks are <= ours.
        guild_all_ranks = sorted(
            r for (g, _rid), r in state.bot_role_ranks.items() if g == ctx.guild.id
        )
        display_rank = guild_all_ranks.index(neighbor_rank) + 1
        total = len(guild_all_ranks)
        label = "up" if direction == "roleup" else "down"
        await ctx.send(embed=emb("✅ Role Moved", f"Role **{role.name}** moved {label} — now **#{display_rank}** of {total}.", C_GREEN))

    @commands.command(name="roles", aliases=["rolelb", "lbroles", "lbr"])
    async def cmd_roles(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        # Sort by stored bot_role_ranks (1 = highest), tie-break and
        # fall-back to Discord position for any unranked role.
        candidates: list = []
        for r in ctx.guild.roles:
            if r.id not in state.bot_roles:
                continue
            rank = state.bot_role_ranks.get((ctx.guild.id, r.id))
            # Unranked roles sort after ranked ones; among unranked,
            # higher Discord position wins.
            sort_key = (0, rank) if rank is not None else (1, -r.position)
            candidates.append((sort_key, r))
        if not candidates:
            await ctx.send(embed=emb("👑 Role Leaderboard", "No bot-created roles in this server yet.", C_PURPLE))
            return
        candidates.sort(key=lambda x: x[0])
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, (_k, role) in enumerate(candidates):
            prefix = medals[i] if i < 3 else f"{i + 1}."
            lock = " 🔒" if role.id in state.locked_roles else ""
            lines.append(f"{prefix} **{role.name}**{lock}")
        lines.append("\n*Also: `!lb` currency · `!levels` XP*")
        await ctx.send(embed=emb("👑 Role Leaderboard", "\n".join(lines), C_PURPLE))


async def setup(bot):
    await bot.add_cog(ShopCog(bot))
