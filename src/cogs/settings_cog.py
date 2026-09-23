import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_BLUE, C_GREY,
    send_ephemeral,
)
from src.economy import (
    drain_bot_balance_into_lottery, announce_new_lottery,
    _ct_now, lottery_month_key,
)
from src.permissions import (
    requires_perm, is_admin,
)
from src.persistence import (
    save_guild_settings, save_bot_settings, save_channel_prompts,
    save_lottery,
    load_lottery
)
from src.guild_config import get_guild_cfg
from src.custom_names import name_conflict
from src.settings_views import pick_action, pick_channels, pick_from_list, pick_users, toggle_panel
from src.confirm_view import confirm_choice, confirm_prompt
from src.config import OLLAMA_MODEL, LOTTERY_SEED_POOL
from src.features import FEATURES, set_feature, features_overview
from src.setup_wizard import run_setup_wizard
from src import state


# (menu label, command method, args) — what the `!settings` action dropdowns
# and the bare `!settings channel` menu can open. Each method runs with no
# typed value, which is what makes it show its own prompt.
_SETTINGS_PANELS = (
    ("🧩 Features on/off", "settings_features", ()),
    ("🧭 Setup questions", "settings_setup", ()),
    ("🛒 Shop items", "settings_shop", ()),
    ("🔞 NSFW", "settings_nsfw", ()),
    ("🪙 Leaderboard scope", "settings_leaderboard", ()),
    ("🎲 Gambler role", "settings_gambler_role", ()),
    ("💬 Quote bypass", "settings_quote", ()),
    ("⚔️ Idle RPG pace", "settings_idle_pace", ()),
    ("⚔️ Idle RPG auto-enroll", "settings_idle_enroll", ()),
    ("🔞 Remove NSFW aliases", "settings_nsfw_alias", ("remove",)),
    ("📖 Remove story aliases", "settings_story_alias", ("remove",)),
    ("🏷️ Remove tax aliases", "settings_tax_aliases", ("remove",)),
    ("🔇 Soundboard rate-limit: add", "settings_soundboard_ratelimit", ("add",)),
    ("🔇 Soundboard rate-limit: remove", "settings_soundboard_ratelimit", ("remove",)),
)
_CHANNEL_PANELS = (
    ("✅ Command whitelist", "settings_channel_whitelist", ()),
    ("❌ Command blacklist", "settings_channel_blacklist", ()),
    ("🤖 AI channels", "settings_channel_ai", ()),
    ("🎮 Game channels", "settings_channel_game", ()),
    ("♟️ Chess channels", "settings_channel_chess", ()),
    ("🎰 Lottery channel", "settings_channel_lottery", ()),
    ("📊 Level-up channel", "settings_channel_levelup", ()),
    ("🏆 Records channel", "settings_channel_records", ()),
    ("📖 Feature request channel", "settings_channel_feature_request", ()),
    ("🎯 Bounty channel", "settings_bounty_channel", ()),
    ("⛏️ Minecraft channel", "settings_minecraft_channel", ()),
    ("🪙 Dailies channel", "settings_dailies_channel", ()),
    ("⚔️ Idle RPG channel", "settings_channel_idle", ()),
)
_GLOBAL_CHANNEL_PANELS = (
    ("🛡️ Admin log channel (global)", "settings_channel_admin_log", ()),
    ("⚠️ Error log channel (global)", "settings_channel_error_log", ()),
    ("🐛 Internal issue channel (global)", "settings_channel_internal_issue", ()),
)

# (label, cfg key, text when unset, note) — the per-guild channel settings as
# both overviews print them. The subcommand name is the label's word in
# lower case (`!settings channel lottery`), except the first two.
_CHANNEL_ROWS = (
    ("✅ Whitelist", "command_whitelist", "none (all allowed)", "commands only work in these channels; `!settings` works everywhere"),
    ("❌ Blacklist", "command_blacklist", "none", "commands never work in these channels"),
    ("🤖 AI", "ai_channels", "all channels", "where @mentions and AI commands answer"),
    ("🎮 Games", "game_channels", "all channels", "games and gambling"),
    ("♟️ Chess", "chess_channels", "game channels (or all)", None),
    ("🎰 Lottery", "lottery_channel", "❌ off", "the monthly lottery runs here"),
    ("📊 Level-ups", "levelup_channel", "❌ off", "level-up announcements"),
    ("🏆 Records", "records_channel", "❌ off", "this server's new records, plus every new global-top record from any server"),
    ("📖 Feature requests", "feature_request_channel", "❌ off", "`!featurerequest` posts here"),
    ("🎯 Bounties", "bounty_channel", "❌ off", "`!bounty` posts here"),
    ("⛏️ Minecraft", "minecraft_channel", "❌ off", "server up/down alerts and player-count notices"),
    ("🪙 Dailies", "dailies_channel", "❌ off", "self-cleaning channel with the react-to-claim dailies embed"),
    ("⚔️ Idle RPG", "idle_channel", "❌ off", "idle RPG news and feed threads — the game is off without one"),
)
_GLOBAL_CHANNEL_ROWS = (
    ("🛡️ Admin log (global)", "admin_log_channel", "❌ off", "admin command use and errors from every server"),
    ("⚠️ Error log (global)", "error_log_channel", "❌ off", "command errors from every server"),
    ("🐛 Internal issues (global)", "internal_issue_channel", "❌ off", "bug reports and internal issues from every server"),
)


def _mentions(value, empty: str) -> str:
    ids = value if isinstance(value, list) else ([value] if value else [])
    return " ".join(f"<#{c}>" for c in ids) if ids else empty


def _channel_rows(cfg: dict, *, with_global: bool) -> list[tuple[str, str, "str | None"]]:
    rows = [(label, _mentions(cfg.get(key), empty), note) for label, key, empty, note in _CHANNEL_ROWS]
    if with_global:
        rows += [(label, _mentions(state.bot_settings.get(key), empty), note) for label, key, empty, note in _GLOBAL_CHANNEL_ROWS]
    return rows


class SettingsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(name="settings", aliases=["setting"], invoke_without_command=True)
    @requires_perm
    async def cmd_settings(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return

        cfg = get_guild_cfg(ctx.guild.id)
        embed = discord.Embed(title="⚙️ Server Settings", color=C_BLUE)
        embed.add_field(
            name="🧩 Features",
            value=features_overview(ctx.guild.id) + "\n*`!settings features` · the first-run questions: `!settings setup`*",
            inline=False,
        )
        rows = _channel_rows(cfg, with_global=is_admin(ctx))
        embed.add_field(
            name="📁 Channels",
            value="\n".join(f"{label}: {value}" for label, value, _ in rows)
            + "\n*`!settings channel <name> #channel` / `clear`*",
            inline=False,
        )

        shop_items = cfg.get("shop_items", {})
        item_names = ["nickname", "role", "unassignrole", "roleup", "roledown", "ragebait", "buyxp"]
        embed.add_field(
            name="🛒 Shop items",
            value="  ".join(f"{n} {'✅' if shop_items.get(n, True) else '❌'}" for n in item_names),
            inline=False,
        )

        nsfw = "✅ on" if cfg.get("nsfw_enabled", False) else "❌ off"
        nsfw += " · channels: " + _mentions(cfg.get("nsfw_channels"), "all")
        if cfg.get("nsfw_banned_tags"):
            nsfw += " · banned tags: " + ", ".join(cfg["nsfw_banned_tags"])
        embed.add_field(
            name="🎛️ Other",
            value=(
                f"🎲 Gambler role: {'✅ on' if cfg.get('gambler_role_enabled', False) else '❌ off'}\n"
                f"💬 Quote bypass: {'✅ on' if cfg.get('quote_bypass_restrictions', False) else '❌ off'}\n"
                f"🪙 Leaderboard default: **{cfg.get('leaderboard_default_scope', 'global')}**\n"
                f"⚔️ Idle RPG: pace **{cfg.get('idle_pace') or 'lively'}** · auto-enroll **{'on' if cfg.get('idle_enroll') else 'off'}**\n"
                f"🔞 NSFW: {nsfw}"
            ),
            inline=False,
        )

        tax_aliases = cfg.get("tax_aliases", {})
        story_aliases = cfg.get("story_aliases", {})
        nsfw_aliases = cfg.get("nsfw_aliases", {})
        rl_names = []
        for uid in cfg.get("soundboard_ratelimit", []):
            member = ctx.guild.get_member(uid)
            rl_names.append(member.display_name if member else str(uid))
        embed.add_field(
            name="🔤 Aliases & limits",
            value=(
                "🏷️ Tax: " + (", ".join(f"{v} `!{k}`" for k, v in tax_aliases.items()) if tax_aliases else "none") + "\n"
                "📖 Story: " + (", ".join(f"`!{k}`" for k in story_aliases) if story_aliases else "none") + "\n"
                "🔞 NSFW: " + (", ".join(f"`!{k}`" for k in nsfw_aliases) if nsfw_aliases else "none") + "\n"
                "🔇 Soundboard rate-limit: " + (", ".join(rl_names) if rl_names else "none")
            ),
            inline=False,
        )
        embed.set_footer(text="Pick an action below, or run any setting bare (e.g. !settings nsfw) for its prompt.")

        await send_ephemeral(ctx, embed=embed)
        await self._open_actions(ctx)

    # ── !settings features ────────────────────────────────────────────────────
    @cmd_settings.command(name="features", aliases=["feature"])
    @requires_perm
    async def settings_features(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        gid = ctx.guild.id
        usage = "`!settings features <" + "|".join(FEATURES) + "> on|off`"
        if not args:
            # Buttons show the stored switch, not the effective state: a child
            # of a disabled parent keeps its own setting for when the parent
            # comes back, and a click must flip what it shows.
            stored = get_guild_cfg(gid).get("features", {})

            async def _set(key: str, enabled: bool):
                set_feature(gid, key, enabled)
                await save_guild_settings()
            items = {
                key: (f.label + (f" · needs {FEATURES[f.parent].label}" if f.parent else ""), stored.get(key, True))
                for key, f in FEATURES.items()
            }
            await toggle_panel(
                ctx,
                title="🧩 Features",
                description=(
                    "Click a feature to turn it on or off — each click saves. Disabled features "
                    "disappear from the menus and their commands refuse to run.\n"
                    "A feature marked *needs X* only runs while X is on.\n"
                    + "\n".join(f"{f.label} — {f.summary}" for f in FEATURES.values())
                    + f"\nUsage: {usage}"
                ),
                items=items,
                on_toggle=_set,
            )
            return
        if len(args) < 2 or args[0].lower() not in FEATURES or args[1].lower() not in ("on", "off"):
            await ctx.send(embed=emb("🧩 Features", f"Usage: {usage}", C_GREY))
            return
        feature = FEATURES[args[0].lower()]
        enabled = args[1].lower() == "on"
        set_feature(gid, feature.key, enabled)
        await save_guild_settings()
        note = ""
        if enabled and feature.parent:
            from src.features import feature_enabled
            if not feature_enabled(gid, feature.parent):
                note = f" It runs once **{FEATURES[feature.parent].label}** is on too."
        await ctx.send(embed=emb("🧩 Features", f"**{feature.label}** is now {'✅ enabled' if enabled else '❌ disabled'}.{note}", C_GREEN))

    # ── !settings setup ───────────────────────────────────────────────────────
    @cmd_settings.command(name="setup", aliases=["wizard"])
    @requires_perm
    async def settings_setup(self, ctx: commands.Context):
        """The questions the bot asks on joining, for a server that wants them again."""
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        await run_setup_wizard(ctx.guild, ctx.channel)

    # ══════════════════════════════════════════════════════════════════════════
    # !settings channel — every channel setting
    # ══════════════════════════════════════════════════════════════════════════

    @cmd_settings.group(name="channel", aliases=["channels"], invoke_without_command=True)
    @requires_perm
    async def cmd_settings_channel(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        embed = discord.Embed(title="📁 Channel Settings", color=C_BLUE)
        for label, value, note in _channel_rows(cfg, with_global=is_admin(ctx)):
            embed.add_field(name=label, value=value + (f"\n*{note}*" if note else ""), inline=False)
        embed.set_footer(text="!settings channel <name> #channel (or clear) — or pick one below.")
        await send_ephemeral(ctx, embed=embed)
        await self._open_panel(ctx, _CHANNEL_PANELS + (_GLOBAL_CHANNEL_PANELS if is_admin(ctx) else ()))

    @commands.command(name="settings-channel", hidden=True)
    @requires_perm
    async def cmd_settings_channel_legacy(self, ctx: commands.Context, sub: str = None, *args):
        """The old spelling, before the channel settings moved under
        `!settings channel`. Forwards so muscle memory keeps working."""
        target = self.cmd_settings_channel if sub is None else self.cmd_settings_channel.get_command(sub.lower())
        if target is None:
            await ctx.send(embed=emb("📁 Channel Settings", f"No channel setting `{sub}` — see `!settings channel`.", C_GREY))
            return
        await self._forward(ctx, target, *args)

    async def _forward(self, ctx, command, *args) -> None:
        """Run another settings command as if it had been typed: its own
        @requires_perm reads ctx.command, so the hidden bot-admin ones stay
        gated on the forwarded path too."""
        ctx.command = command
        await command.callback(self, ctx, *args)

    # ── !settings shop ────────────────────────────────────────────────────────
    @cmd_settings.command(name="shop")
    @requires_perm
    async def settings_shop(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        valid_items = {"nickname", "role", "unassignrole", "roleup", "roledown", "ragebait", "buyxp"}
        if not args:
            shop_items = cfg.setdefault("shop_items", {})

            async def _set(item: str, enabled: bool):
                shop_items[item] = enabled
                await save_guild_settings()
            await toggle_panel(
                ctx,
                title="⚙️ Shop Items",
                description="Click an item to turn it on or off — each click saves.\nUsage: `!settings shop <item> on|off`",
                items={item: (item, shop_items.get(item, True)) for item in sorted(valid_items)},
                on_toggle=_set,
            )
            return
        if len(args) < 2 or args[0].lower() not in valid_items or args[1].lower() not in ("on", "off"):
            await ctx.send(embed=emb("⚙️ Shop", f"Usage: `!settings shop <item> on|off`\nItems: {', '.join(valid_items)}", C_GREY))
            return
        item = args[0].lower()
        enabled = args[1].lower() == "on"
        if "shop_items" not in cfg:
            cfg["shop_items"] = {}
        cfg["shop_items"][item] = enabled
        await save_guild_settings()
        status = "✅ enabled" if enabled else "❌ disabled"
        await ctx.send(embed=emb("⚙️ Shop", f"**{item}** is now {status}.", C_GREEN))

    # ── !settings leaderboard ─────────────────────────────────────────────────
    @cmd_settings.command(name="leaderboard")
    @requires_perm
    async def settings_leaderboard(self, ctx: commands.Context, scope: str = None):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if scope is None:
            current = cfg.get("leaderboard_default_scope", "global")
            scope = await confirm_choice(
                ctx,
                title="🪙 Leaderboard",
                description=f"Which scope should a bare `!lb` show?\n**Currently:** {current}\n\nUsage: `!settings leaderboard server|global`",
                choices=[{"label": "Server", "value": "server"}, {"label": "Global", "value": "global"}],
                payer=ctx.author,
                not_yours="Not your prompt.",
            )
            if scope is None:
                return
        if scope.lower() not in ("server", "global"):
            current = cfg.get("leaderboard_default_scope", "global")
            await ctx.send(embed=emb(
                "🪙 Leaderboard",
                f"Usage: `!settings leaderboard server|global`\nCurrent default: **{current}**",
                C_GREY,
            ))
            return
        cfg["leaderboard_default_scope"] = scope.lower()
        await save_guild_settings()
        await ctx.send(embed=emb(
            "🪙 Leaderboard",
            f"`!lb` now defaults to **{scope.lower()}** scope. Users can still override with `!lb server` or `!lb global`.",
            C_GREEN,
        ))

    # ── !settings idle-enroll ─────────────────────────────────────────────────
    @cmd_settings.command(name="idle-enroll")
    @requires_perm
    async def settings_idle_enroll(self, ctx: commands.Context, choice: str = None):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        usage = "`!settings idle-enroll on|off`"
        what = (
            "Give every member who holds one of my roles (a shop role, the gambler role…) an idle RPG character, "
            "a few at a time. They are never pinged or messaged about it: an enrolled character has no feed thread "
            "until its player runs `!idle join <class>`, which also sets its class for free. "
            "Anyone who `!idle leave`s is left alone."
        )
        if choice is None:
            choice = await self._on_off_choice(ctx, title="⚔️ Idle RPG Auto-Enroll", what=what, current=bool(cfg.get("idle_enroll")), usage=usage)
            if choice is None:
                return
        if choice.lower() not in ("on", "off"):
            await ctx.send(embed=emb("⚔️ Idle RPG Auto-Enroll", f"Usage: {usage}\n\n{what}", C_GREY))
            return
        cfg["idle_enroll"] = choice.lower() == "on"
        await save_guild_settings()
        if cfg["idle_enroll"] and not cfg.get("idle_channel"):
            note = " It starts once an idle channel is set (`!settings channel idle #channel`)."
        else:
            note = " Characters already made stay; only new enrollment stops." if not cfg["idle_enroll"] else ""
        await ctx.send(embed=emb("⚔️ Idle RPG Auto-Enroll", f"Auto-enroll is now **{choice.lower()}**.{note}", C_GREEN))

    # ── !settings idle-pace ───────────────────────────────────────────────────
    @cmd_settings.command(name="idle-pace")
    @requires_perm
    async def settings_idle_pace(self, ctx: commands.Context, pace: str = None):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        usage = "`!settings idle-pace lively|classic`"
        what = (
            "**Lively** (default) — a godsend and a calamity about once a day per player, a monster every quarter of an hour in the wilds, "
            "team battles from four players. Built for a handful of players.\n"
            "**Classic** — the IRC odds: an event every week or so. Built for dozens."
        )
        if pace is None:
            pace = await confirm_choice(
                ctx,
                title="⚔️ Idle RPG Pace",
                description=f"{what}\n**Currently:** {cfg.get('idle_pace') or 'lively'}\n\nUsage: {usage}",
                choices=[{"label": "Lively", "value": "lively"}, {"label": "Classic", "value": "classic"}],
                payer=ctx.author,
                not_yours="Not your prompt.",
            )
            if pace is None:
                return
        if pace.lower() not in ("lively", "classic"):
            await ctx.send(embed=emb("⚔️ Idle RPG Pace", f"Usage: {usage}\n\n{what}", C_GREY))
            return
        cfg["idle_pace"] = pace.lower()
        await save_guild_settings()
        await ctx.send(embed=emb("⚔️ Idle RPG Pace", f"The idle RPG now runs at the **{pace.lower()}** pace.", C_GREEN))

    # ── !settings channel bounty ──────────────────────────────────────────────
    @cmd_settings_channel.command(name="bounty")
    @requires_perm
    async def settings_bounty_channel(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        chosen = await self._channel_choice(ctx, args, title="🎯 Bounty Channel", current=cfg.get("bounty_channel"), multi=False)
        if chosen is None:
            return
        if not chosen:
            cfg["bounty_channel"] = None
            await save_guild_settings()
            await ctx.send(embed=emb("🎯 Bounty Channel", "Bounties disabled.", C_GREEN))
        else:
            channel = chosen[0]
            cfg["bounty_channel"] = channel.id
            await save_guild_settings()
            await ctx.send(embed=emb(
                "🎯 Bounty Channel",
                f"Bounties can now be posted in {channel.mention} with `!bounty <coins> <condition>`.",
                C_GREEN,
            ))

    # ── !settings channel minecraft ───────────────────────────────────────────
    @cmd_settings_channel.command(name="minecraft")
    @requires_perm
    async def settings_minecraft_channel(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        chosen = await self._channel_choice(ctx, args, title="⛏️ Minecraft Channel", current=cfg.get("minecraft_channel"), multi=False)
        if chosen is None:
            return
        if not chosen:
            cfg["minecraft_channel"] = None
            await save_guild_settings()
            await ctx.send(embed=emb("⛏️ Minecraft Channel", "Minecraft server notifications disabled.", C_GREEN))
        else:
            channel = chosen[0]
            cfg["minecraft_channel"] = channel.id
            await save_guild_settings()
            await ctx.send(embed=emb(
                "⛏️ Minecraft Channel",
                f"Minecraft server up/down alerts and player-count notices will post in {channel.mention}.",
                C_GREEN,
            ))

    # ── !settings channel dailies ─────────────────────────────────────────────
    @cmd_settings_channel.command(name="dailies")
    @requires_perm
    async def settings_dailies_channel(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        chosen = await self._channel_choice(ctx, args, title="🪙 Dailies Channel", current=cfg.get("dailies_channel"), multi=False)
        if chosen is None:
            return
        if not chosen:
            old_ch_id = cfg.get("dailies_channel")
            old_msg_id = cfg.get("dailies_message_id")
            cfg["dailies_channel"] = None
            cfg.pop("dailies_message_id", None)
            cfg.pop("dailies_reset_day", None)
            cfg.pop("dailies_keep_ids", None)
            await save_guild_settings()
            # Best-effort: remove the claim embed we left behind.
            if old_ch_id and old_msg_id:
                try:
                    channel = self.bot.get_channel(old_ch_id) or await self.bot.fetch_channel(old_ch_id)
                    msg = await channel.fetch_message(old_msg_id)
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            await ctx.send(embed=emb("🪙 Dailies Channel", "Dailies channel disabled.", C_GREEN))
        else:
            from src.cogs.dailies_cog import refresh_dailies_channel
            channel = chosen[0]
            cfg["dailies_channel"] = channel.id
            # New channel — the old claim message (if any) no longer counts.
            cfg.pop("dailies_message_id", None)
            cfg.pop("dailies_reset_day", None)
            cfg.pop("dailies_keep_ids", None)
            await save_guild_settings()
            await refresh_dailies_channel(self.bot, ctx.guild.id)
            await ctx.send(embed=emb(
                "🪙 Dailies Channel",
                f"{channel.mention} is now the dailies channel. I'll keep it cleared except for the "
                f"**Claim your dailies** embed — reacting 🗓️ there instantly claims the daily reward and "
                f"uses all daily scratchoffs (🪙 also coin-flips the winnings, 🎰 bets them on slots). "
                f"Claims reset at the 5am CT daily reset; everything else posted there is deleted "
                f"after 5 minutes.",
                C_GREEN,
            ))

    # The three channel settings that used to live directly under !settings.
    @cmd_settings.command(name="bounty-channel", hidden=True)
    @requires_perm
    async def settings_bounty_channel_legacy(self, ctx: commands.Context, *args):
        await self._forward(ctx, self.settings_bounty_channel, *args)

    @cmd_settings.command(name="minecraft-channel", hidden=True)
    @requires_perm
    async def settings_minecraft_channel_legacy(self, ctx: commands.Context, *args):
        await self._forward(ctx, self.settings_minecraft_channel, *args)

    @cmd_settings.command(name="dailies-channel", hidden=True)
    @requires_perm
    async def settings_dailies_channel_legacy(self, ctx: commands.Context, *args):
        await self._forward(ctx, self.settings_dailies_channel, *args)

    # ── !settings nsfw ────────────────────────────────────────────────────────
    @cmd_settings.command(name="nsfw")
    @requires_perm
    async def settings_nsfw(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if not args:
            enabled = cfg.get("nsfw_enabled", False)
            channels = " ".join(f"<#{cid}>" for cid in cfg.get("nsfw_channels", [])) or "none"
            banned = ", ".join(f"`{t}`" for t in cfg.get("nsfw_banned_tags", [])) or "none"
            picked = await confirm_choice(
                ctx,
                title="⚙️ NSFW",
                description=(
                    f"**NSFW commands:** {'✅ on' if enabled else '❌ off'}\n"
                    f"**Channels:** {channels}\n**Banned tags:** {banned}\n\n"
                    "Ban a tag with `!settings nsfw ban <tag>`."
                ),
                choices=[
                    {"label": "Turn off" if enabled else "Turn on", "value": "off" if enabled else "on", "default": True},
                    {"label": "Channels", "value": "channels"},
                    {"label": "Unban tags", "value": "unban"},
                ],
                payer=ctx.author,
                not_yours="Not your prompt.",
            )
            if picked is None:
                return
            args = (picked,)
        action = args[0].lower()
        if action in ("on", "off"):
            cfg["nsfw_enabled"] = (action == "on")
            await save_guild_settings()
            status = "✅ enabled" if action == "on" else "❌ disabled"
            await ctx.send(embed=emb("⚙️ NSFW", f"NSFW commands are now {status}.", C_GREEN))
        elif action == "channels":
            nsfw_channels = cfg.setdefault("nsfw_channels", [])
            if len(args) < 2:
                chosen = await pick_channels(
                    ctx, title="⚙️ NSFW Channels", current_ids=nsfw_channels, multi=True,
                    typed_usage="`!settings nsfw channels <add|remove|list> [#channel]`",
                )
                if chosen is None:
                    return
                nsfw_channels[:] = [c.id for c in chosen]
                await save_guild_settings()
                val = " ".join(f"<#{cid}>" for cid in nsfw_channels) or "none"
                await ctx.send(embed=emb("⚙️ NSFW Channels", f"Whitelist is now: {val}", C_GREEN))
                return
            channel_action = args[1].lower()
            if channel_action == "add":
                if not ctx.message.channel_mentions:
                    await ctx.send(embed=emb("⚙️ NSFW", "Please mention a channel to add.", C_GREY))
                    return
                for channel in ctx.message.channel_mentions:
                    if channel.id not in nsfw_channels:
                        nsfw_channels.append(channel.id)
                await save_guild_settings()
                # channel_mentions holds channel objects, not IDs — f"<#{cid}>"
                # rendered a literal "<#channel-name>" instead of a mention.
                names = " ".join(ch.mention for ch in ctx.message.channel_mentions)
                await ctx.send(embed=emb("⚙️ NSFW Channels", f"Added {names} to whitelist.", C_GREEN))
            elif channel_action == "remove":
                if not ctx.message.channel_mentions:
                    await ctx.send(embed=emb("⚙️ NSFW", "Please mention a channel to remove.", C_GREY))
                    return
                for channel in ctx.message.channel_mentions:
                    if channel.id in nsfw_channels:
                        nsfw_channels.remove(channel.id)
                await save_guild_settings()
                names = " ".join(ch.mention for ch in ctx.message.channel_mentions)
                await ctx.send(embed=emb("⚙️ NSFW Channels", f"Removed {names} from whitelist.", C_GREEN))
            elif channel_action == "list":
                val = " ".join(f"<#{cid}>" for cid in nsfw_channels) if nsfw_channels else "none"
                await ctx.send(embed=emb("⚙️ NSFW Channels", val, C_GREY))
            else:
                await ctx.send(embed=emb("⚙️ NSFW", "Usage: `!settings nsfw channels <add|remove|list> [#channel]`", C_GREY))
        elif action == "ban" and len(args) >= 2:
            tag = args[1].lower()
            banned = cfg.setdefault("nsfw_banned_tags", [])
            if tag not in banned:
                banned.append(tag)
                await save_guild_settings()
            await ctx.send(embed=emb("⚙️ NSFW", f"Tag `{tag}` banned.", C_GREEN))
        elif action == "unban" and len(args) < 2:
            banned = cfg.get("nsfw_banned_tags", [])
            if not banned:
                await ctx.send(embed=emb("⚙️ NSFW", "No tags are banned.", C_GREY))
                return
            picked = await pick_from_list(
                ctx, title="⚙️ NSFW Banned Tags", description="Pick the tags to unban.",
                options=[(t, t) for t in banned], placeholder="Tags to unban…",
            )
            if not picked:
                return
            for tag in picked:
                if tag in banned:
                    banned.remove(tag)
            await save_guild_settings()
            await ctx.send(embed=emb("⚙️ NSFW", "Unbanned " + ", ".join(f"`{t}`" for t in picked) + ".", C_GREEN))
        elif action == "unban" and len(args) >= 2:
            tag = args[1].lower()
            banned = cfg.get("nsfw_banned_tags", [])
            if tag in banned:
                banned.remove(tag)
                await save_guild_settings()
                await ctx.send(embed=emb("⚙️ NSFW", f"Tag `{tag}` unbanned.", C_GREEN))
            else:
                await ctx.send(embed=emb("⚙️ NSFW", f"Tag `{tag}` was not banned.", C_GREY))
        elif action == "banned":
            banned = cfg.get("nsfw_banned_tags", [])
            val = ", ".join(f"`{t}`" for t in banned) if banned else "none"
            await ctx.send(embed=emb("⚙️ NSFW Banned Tags", val, C_GREY))
        else:
            await ctx.send(embed=emb("⚙️ NSFW", "Usage: `!settings nsfw on|off` / `channels <add|remove|list> [#channel]` / `ban <tag>` / `unban <tag>` / `banned`", C_GREY))

    async def _open_actions(self, ctx) -> None:
        """The two dropdowns under `!settings`: one for settings, one for
        channels (more than 25 together — a select's cap)."""
        channel_panels = _CHANNEL_PANELS + (_GLOBAL_CHANNEL_PANELS if is_admin(ctx) else ())
        picked = await pick_action(
            ctx, title="⚙️ Actions", description="Pick a setting or a channel to change it here.",
            menus=[
                ("⚙️ Settings…", [(label, f"s{i}") for i, (label, _, _) in enumerate(_SETTINGS_PANELS)]),
                ("📁 Channels…", [(label, f"c{i}") for i, (label, _, _) in enumerate(channel_panels)]),
            ],
        )
        if not picked:
            return
        panels = _SETTINGS_PANELS if picked[0] == "s" else channel_panels
        await self._run_panel(ctx, panels[int(picked[1:])])

    async def _open_panel(self, ctx, panels) -> None:
        """Dropdown under a settings overview that opens one setting's prompt."""
        picked = await pick_from_list(
            ctx, title="⚙️ Change a Setting", description="Pick a setting to change it here.",
            options=[(label, str(i)) for i, (label, _, _) in enumerate(panels)],
            placeholder="Settings…", multi=False,
        )
        if not picked:
            return
        await self._run_panel(ctx, panels[int(picked[0])])

    async def _run_panel(self, ctx, panel) -> None:
        _, method, args = panel
        # The subcommand's @requires_perm and its usage text both read ctx.command.
        await self._forward(ctx, getattr(self, method), *args)

    async def _remove_aliases(self, ctx, args, aliases: dict, *, title: str) -> None:
        """`remove <word>`, or a dropdown of the existing aliases when no word is typed."""
        if len(args) >= 2:
            words = [args[1].lower()]
            if words[0] not in aliases:
                await ctx.send(embed=emb(title, f"`{words[0]}` is not in the alias list.", C_GREY))
                return
        else:
            if not aliases:
                await ctx.send(embed=emb(title, "There are no aliases to remove.", C_GREY))
                return
            words = await pick_from_list(
                ctx, title=title, description="Pick the aliases to remove.",
                options=[(f"!{w}", w) for w in aliases], placeholder="Aliases to remove…",
            )
            if not words:
                return
        for word in words:
            aliases.pop(word, None)
        await save_guild_settings()
        await ctx.send(embed=emb(title, "Removed " + ", ".join(f"`{w}`" for w in words) + ".", C_GREEN))

    async def _confirm_clear(self, ctx, aliases: dict, *, title: str) -> bool:
        if not aliases:
            await ctx.send(embed=emb(title, "There are no aliases to clear.", C_GREY))
            return False
        return bool(await confirm_prompt(
            ctx, title=f"{title} — Clear All",
            description="This removes: " + ", ".join(f"`!{w}`" for w in aliases),
            payer=ctx.author, not_yours="Not your prompt.",
        ))

    async def _on_off_choice(self, ctx, *, title: str, what: str, current: bool, usage: str) -> "str | None":
        """Buttons for a bare on/off command: "on", "off", or None if dismissed."""
        return await confirm_choice(
            ctx,
            title=title,
            description=f"{what}\n**Currently:** {'✅ on' if current else '❌ off'}\n\nUsage: {usage}",
            choices=[{"label": "On", "value": "on"}, {"label": "Off", "value": "off"}],
            payer=ctx.author,
            not_yours="Not your prompt.",
        )

    async def _channel_choice(self, ctx, args, *, title: str, current, multi: bool) -> "list | None":
        """What a channel setting should become: `[]` to clear it, the
        channels to set, or None when nothing was chosen. Typed `clear` /
        #mentions win; a bare command opens the picker."""
        if args and args[0].lower() == "clear":
            return []
        if ctx.message.channel_mentions:
            return list(ctx.message.channel_mentions)
        name = ctx.command.qualified_name
        usage = f"`!{name} #channel{' ...' if multi else ''}` or `!{name} clear`"
        if ctx.guild is None:  # a channel select can't render in a DM
            await ctx.send(embed=emb(title, f"Usage: {usage}", C_GREY))
            return None
        current = current if isinstance(current, list) else [current]
        return await pick_channels(ctx, title=title, current_ids=current, multi=multi, typed_usage=usage)

    async def _refuse_taken_alias(self, ctx, word: str, *, own: str, also: "str | None" = None) -> bool:
        """Send the refusal and return True if `!<word>` is already taken
        (see src/custom_names.py). `also` is a second command path to check."""
        holder = name_conflict(self.bot, ctx.guild.id, word, own=own)
        if holder is None and also and self.bot is not None and self.bot.get_command(also) is not None:
            holder = "a bot command"
            word = also
        if holder is None:
            return False
        await ctx.send(embed=emb("❌ Name Taken", f"`!{word}` is already {holder}. Pick another word.", C_RED))
        return True

    # ── !settings nsfw-alias ──────────────────────────────────────────────────
    @cmd_settings.command(name="nsfw-alias")
    @requires_perm
    async def settings_nsfw_alias(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        aliases: dict = cfg.setdefault("nsfw_aliases", {})

        if not args:
            await ctx.send(embed=emb(
                "⚙️ NSFW Aliases",
                "Usage: `!settings nsfw-alias add|remove <word>` / `list` / `clear`\n"
                "Aliases let users type `!<alias>` as a shortcut for `!nsfw`. "
                "The alias name becomes a custom command.",
                C_GREY,
            ))
            return

        action = args[0].lower()

        if action == "list":
            if aliases:
                lines = []
                for k, v in aliases.items():
                    t = v.get("tags", "") if isinstance(v, dict) else ""
                    lines.append(f"`!{k}`" + (f" — tags: `{t}`" if t else ""))
                val = "\n".join(lines)
            else:
                val = "none"
            await ctx.send(embed=emb("🔞 NSFW Aliases", val, C_GOLD))

        elif action == "clear":
            if not await self._confirm_clear(ctx, aliases, title="🔞 NSFW Aliases"):
                return
            cfg["nsfw_aliases"] = {}
            await save_guild_settings()
            await ctx.send(embed=emb("🔞 NSFW Aliases", "All aliases cleared.", C_GREEN))

        elif action == "add":
            if len(args) < 2:
                await ctx.send(embed=emb("⚙️ NSFW Aliases", "Usage: `!settings nsfw-alias add <word>`", C_GREY))
                return
            word = args[1].lower()
            if not word.isalnum():
                await ctx.send(embed=emb("❌ Invalid Alias", "Alias must be a single word (letters and numbers only).", C_RED))
                return
            if word in aliases:
                await ctx.send(embed=emb("🔞 NSFW Aliases", f"`{word}` is already an alias.", C_GREY))
                return
            if await self._refuse_taken_alias(ctx, word, own="nsfw_aliases"):
                return
            tags = " ".join(args[2:]) if len(args) > 2 else ""
            aliases[word] = {"tags": tags}
            await save_guild_settings()
            tag_info = f" (pre-fills tags: `{tags}`)" if tags else ""
            await ctx.send(embed=emb("🔞 NSFW Aliases", f"Added `!{word}`{tag_info}.", C_GREEN))

        elif action == "remove":
            await self._remove_aliases(ctx, args, aliases, title="🔞 NSFW Aliases")

        else:
            await ctx.send(embed=emb("⚙️ NSFW Aliases", "Usage: `!settings nsfw-alias add|remove <word>` / `list` / `clear`", C_GREY))

    # ── !settings story-alias ─────────────────────────────────────────────────
    @cmd_settings.command(name="story-alias")
    @requires_perm
    async def settings_story_alias(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        aliases: dict = cfg.setdefault("story_aliases", {})

        usage_short = (
            "Usage: `!settings story-alias add <word> <system prompt>` / `remove <word>` / `list` / `clear`"
        )

        if not args:
            await ctx.send(embed=emb(
                "⚙️ Story Aliases",
                f"{usage_short}\n"
                "Aliases let users type `!<word>` as a shortcut for `!story` with a custom "
                "system prompt — e.g. `!settings story-alias add scifi You write hard science fiction…`",
                C_GREY,
            ))
            return

        action = args[0].lower()

        if action == "list":
            if aliases:
                lines = []
                for k, v in aliases.items():
                    preview = (v[:80] + "…") if isinstance(v, str) and len(v) > 80 else v
                    lines.append(f"`!{k}` — {preview}")
                val = "\n".join(lines)
            else:
                val = "none"
            await ctx.send(embed=emb("📖 Story Aliases", val, C_GOLD))

        elif action == "clear":
            if not await self._confirm_clear(ctx, aliases, title="📖 Story Aliases"):
                return
            cfg["story_aliases"] = {}
            await save_guild_settings()
            await ctx.send(embed=emb("📖 Story Aliases", "All aliases cleared.", C_GREEN))

        elif action == "add":
            if len(args) < 3:
                await ctx.send(embed=emb("⚙️ Story Aliases", "Usage: `!settings story-alias add <word> <system prompt>`", C_GREY))
                return
            word = args[1].lower()
            if not word.isalnum():
                await ctx.send(embed=emb("❌ Invalid Alias", "Alias must be a single word (letters and numbers only).", C_RED))
                return
            if await self._refuse_taken_alias(ctx, word, own="story_aliases"):
                return
            prompt = " ".join(args[2:]).strip()
            if not prompt:
                await ctx.send(embed=emb("❌ Empty Prompt", "Provide a non-empty system prompt.", C_RED))
                return
            if len(prompt) > 2000:
                await ctx.send(embed=emb("❌ Prompt Too Long", "System prompt must be ≤ 2000 characters.", C_RED))
                return
            aliases[word] = prompt
            await save_guild_settings()
            await ctx.send(embed=emb("📖 Story Aliases", f"Added `!{word}`.", C_GREEN))

        elif action == "remove":
            await self._remove_aliases(ctx, args, aliases, title="📖 Story Aliases")

        else:
            await ctx.send(embed=emb("⚙️ Story Aliases", usage_short, C_GREY))

    # ── !settings quote ───────────────────────────────────────────────────────
    @cmd_settings.command(name="quote")
    @requires_perm
    async def settings_quote(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        action = args[0].lower() if args else "bypass"
        if action == "bypass":
            if len(args) < 2:
                bypass_action = await self._on_off_choice(
                    ctx, title="⚙️ quote", what="Let `!quote` work in any channel, ignoring channel restrictions?",
                    current=cfg.get("quote_bypass_restrictions", False), usage="`!settings quote bypass on|off`",
                )
                if bypass_action is None:
                    return
            else:
                bypass_action = args[1].lower()
            if bypass_action in ("on", "off"):
                cfg["quote_bypass_restrictions"] = (bypass_action == "on")
                await save_guild_settings()
                status = "✅ enabled" if bypass_action == "on" else "❌ disabled"
                await ctx.send(embed=emb("⚙️ quote", f"Quote bypass is now {status} (quote works in any channel).", C_GREEN))
            else:
                await ctx.send(embed=emb("⚙️ quote", "Usage: `!settings quote bypass on|off`", C_GREY))
        else:
            await ctx.send(embed=emb("⚙️ quote", "Usage: `!settings quote bypass on|off`", C_GREY))

    # ── !settings soundboard-ratelimit ────────────────────────────────────────
    @cmd_settings.command(name="soundboard-ratelimit")
    @requires_perm
    async def settings_soundboard_ratelimit(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
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
                    pass  # not a raw id — mention tokens were collected above
            if not user_ids:
                picked = await pick_users(
                    ctx, title="⚙️ Soundboard Rate-Limit",
                    description="Pick the users to rate-limit.\nUsage: `!settings soundboard-ratelimit add @user|<userid>`",
                )
                if not picked:
                    return
                user_ids = [u.id for u in picked]
            added = []
            for uid in user_ids:
                if uid not in rl_list:
                    rl_list.append(uid)
                    added.append(f"`{uid}`")
            if added:
                await save_guild_settings()
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
                    pass  # not a raw id — mention tokens were collected above
            if not user_ids:
                if not rl_list:
                    await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", "No users on the list.", C_GREY))
                    return
                options = []
                for uid in rl_list:
                    member = ctx.guild.get_member(uid)
                    options.append((member.display_name if member else str(uid), str(uid)))
                picked = await pick_from_list(
                    ctx, title="⚙️ Soundboard Rate-Limit", description="Pick the users to take off the list.",
                    options=options, placeholder="Users to remove…",
                )
                if not picked:
                    return
                user_ids = [int(uid) for uid in picked]
            removed = []
            for uid in user_ids:
                if uid in rl_list:
                    rl_list.remove(uid)
                    removed.append(f"`{uid}`")
            if removed:
                await save_guild_settings()
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
    @requires_perm
    async def settings_gambler_role(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if not args:
            picked = await self._on_off_choice(
                ctx, title="⚙️ Gambler Role", what="Track scratchoff streaks and auto-assign the **Gamblers** role?",
                current=cfg.get("gambler_role_enabled", False), usage="`!settings gambler-role on|off`",
            )
            if picked is None:
                return
            args = (picked,)
        if args[0].lower() not in ("on", "off"):
            await ctx.send(embed=emb("⚙️ Gambler Role", "Usage: `!settings gambler-role on|off`", C_GREY))
            return
        enabled = args[0].lower() == "on"
        cfg["gambler_role_enabled"] = enabled
        await save_guild_settings()
        status = "✅ enabled" if enabled else "❌ disabled"
        detail = ""
        if enabled:
            from src.gambling.scratchoff import get_or_create_gamblers_role, GAMBLER_ROLE_STREAK_REQUIRED
            role = await get_or_create_gamblers_role(ctx.guild)
            if role:
                detail = f"\nThe **Gamblers** role is ready. Users who use all 3 scratchoffs **{GAMBLER_ROLE_STREAK_REQUIRED} days in a row** will be auto-assigned. They'll be pinged when a slots jackpot or lottery is won."
            else:
                detail = "\n⚠️ Could not create the **Gamblers** role — check the bot's `Manage Roles` permission."
        await ctx.send(embed=emb("⚙️ Gambler Role", f"Gambler role tracking is now {status}.{detail}", C_GREEN))

    # ── !settings tax-aliases ─────────────────────────────────────────────────
    @cmd_settings.command(name="tax-aliases")
    @requires_perm
    async def settings_tax_aliases(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        aliases: dict = cfg.setdefault("tax_aliases", {})

        if not args:
            await ctx.send(embed=emb(
                "⚙️ Tax Aliases",
                "Usage: `!settings tax-aliases add <word> [emoji]` / `remove <word>` / `list` / `clear`\n"
                "Aliases let users type `!shop <alias> @user` or `!<alias> @user` to apply a tax "
                "announced as the **<alias> tax**. An optional emoji is shown in the tax message.",
                C_GREY,
            ))
            return

        action = args[0].lower()

        if action == "list":
            val = "\n".join(f"{v} `!{k}`" for k, v in aliases.items()) if aliases else "none"
            await ctx.send(embed=emb("🏷️ Tax Aliases", val, C_GOLD))

        elif action == "clear":
            if not await self._confirm_clear(ctx, aliases, title="🏷️ Tax Aliases"):
                return
            cfg["tax_aliases"] = {}
            await save_guild_settings()
            await ctx.send(embed=emb("🏷️ Tax Aliases", "All aliases cleared.", C_GREEN))

        elif action == "add":
            if len(args) < 2:
                await ctx.send(embed=emb("⚙️ Tax Aliases", "Usage: `!settings tax-aliases add <word> [emoji]`", C_GREY))
                return
            word = args[1].lower()
            if not word.isalpha():
                await ctx.send(embed=emb("❌ Invalid Alias", "Alias must be a single word (letters only).", C_RED))
                return
            if word in aliases:
                await ctx.send(embed=emb("🏷️ Tax Aliases", f"`{word}` is already an alias.", C_GREY))
                return
            # A tax alias also answers as `!shop <word>`.
            if await self._refuse_taken_alias(ctx, word, own="tax_aliases", also=f"shop {word}"):
                return
            emoji = args[2] if len(args) > 2 else "💰"
            aliases[word] = emoji
            await save_guild_settings()
            await ctx.send(embed=emb("🏷️ Tax Aliases", f"Added {emoji} `!{word}`. Users can now use `!shop {word} @user` or `!{word} @user`.", C_GREEN))

        elif action == "remove":
            await self._remove_aliases(ctx, args, aliases, title="🏷️ Tax Aliases")

        else:
            await ctx.send(embed=emb("⚙️ Tax Aliases", "Usage: `!settings tax-aliases add|remove <word> [emoji]` / `list` / `clear`", C_GREY))

    # ── !settings channel ai ──────────────────────────────────────────────────
    @cmd_settings_channel.command(name="ai")
    @requires_perm
    async def settings_channel_ai(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        chosen = await self._channel_choice(ctx, args, title="⚙️ AI Channels", current=cfg.get("ai_channels"), multi=True)
        if chosen is None:
            return
        if not chosen:
            cfg["ai_channels"] = []
            await save_guild_settings()
            await ctx.send(embed=emb("⚙️ AI Channels", "AI channel restriction removed — all channels allowed.", C_GREEN))
        else:
            cfg["ai_channels"] = [c.id for c in chosen]
            await save_guild_settings()
            names = " ".join(c.mention for c in chosen)
            await ctx.send(embed=emb("⚙️ AI Channels", f"AI commands restricted to: {names}", C_GREEN))

    # ── !settings channel whitelist ───────────────────────────────────────────
    @cmd_settings_channel.command(name="whitelist")
    @requires_perm
    async def settings_channel_whitelist(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        chosen = await self._channel_choice(ctx, args, title="✅ Channel Whitelist", current=cfg.get("command_whitelist"), multi=True)
        if chosen is None:
            return
        if not chosen:
            cfg["command_whitelist"] = []
            await save_guild_settings()
            await ctx.send(embed=emb("✅ Channel Whitelist", "Whitelist removed — commands allowed in all channels.", C_GREEN))
        else:
            cfg["command_whitelist"] = [c.id for c in chosen]
            await save_guild_settings()
            names = " ".join(c.mention for c in chosen)
            await ctx.send(embed=emb("✅ Channel Whitelist", f"Commands restricted to: {names}\n(Note: `!settings` always works everywhere)", C_GREEN))

    # ── !settings channel blacklist ───────────────────────────────────────────
    @cmd_settings_channel.command(name="blacklist")
    @requires_perm
    async def settings_channel_blacklist(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        chosen = await self._channel_choice(ctx, args, title="❌ Channel Blacklist", current=cfg.get("command_blacklist"), multi=True)
        if chosen is None:
            return
        if not chosen:
            cfg["command_blacklist"] = []
            await save_guild_settings()
            await ctx.send(embed=emb("❌ Channel Blacklist", "Blacklist cleared — commands allowed in all channels.", C_GREEN))
        else:
            cfg["command_blacklist"] = [c.id for c in chosen]
            await save_guild_settings()
            names = " ".join(c.mention for c in chosen)
            await ctx.send(embed=emb("❌ Channel Blacklist", f"Commands blocked in: {names}", C_GREEN))

    # ── !settings channel game ────────────────────────────────────────────────
    @cmd_settings_channel.command(name="game")
    @requires_perm
    async def settings_channel_game(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        chosen = await self._channel_choice(ctx, args, title="🎮 Game Channels", current=cfg.get("game_channels"), multi=True)
        if chosen is None:
            return
        if not chosen:
            cfg["game_channels"] = []
            await save_guild_settings()
            await ctx.send(embed=emb("🎮 Game Channels", "Game channel restriction removed — games and gambling allowed everywhere.", C_GREEN))
        else:
            cfg["game_channels"] = [c.id for c in chosen]
            await save_guild_settings()
            names = " ".join(c.mention for c in chosen)
            await ctx.send(embed=emb("🎮 Game Channels", f"Games and gambling restricted to: {names}", C_GREEN))

    # ── !settings channel chess ───────────────────────────────────────────────
    @cmd_settings_channel.command(name="chess")
    @requires_perm
    async def settings_channel_chess(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        chosen = await self._channel_choice(ctx, args, title="♟️ Chess Channels", current=cfg.get("chess_channels"), multi=True)
        if chosen is None:
            return
        if not chosen:
            cfg["chess_channels"] = []
            await save_guild_settings()
            await ctx.send(embed=emb("♟️ Chess Channels", "Chess channel restriction removed — all channels allowed.", C_GREEN))
        else:
            cfg["chess_channels"] = [c.id for c in chosen]
            await save_guild_settings()
            names = " ".join(c.mention for c in chosen)
            await ctx.send(embed=emb("♟️ Chess Channels", f"Chess restricted to: {names}", C_GREEN))

    # ── !settings channel lottery ─────────────────────────────────────────────
    @cmd_settings_channel.command(name="lottery")
    @requires_perm
    async def settings_channel_lottery(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        chosen = await self._channel_choice(ctx, args, title="🎰 Lottery Channel", current=cfg.get("lottery_channel"), multi=False)
        if chosen is None:
            return
        if not chosen:
            cfg["lottery_channel"] = None
            await save_guild_settings()
            await ctx.send(embed=emb("🎰 Lottery Channel", "Lottery disabled.", C_GREEN))
        else:
            channel = chosen[0]
            cfg["lottery_channel"] = channel.id
            await save_guild_settings()

            current_month = lottery_month_key(_ct_now())
            lottery = await load_lottery(ctx.guild.id)
            # last_posted_week holds a YYYYMM month key — the name predates
            # the monthly lottery.
            if lottery.get("last_posted_week", 0) != current_month:
                lottery = {"prize_pool": LOTTERY_SEED_POOL, "players": {}, "last_posted_week": current_month}
                await drain_bot_balance_into_lottery(lottery, ctx.guild.id)
                await save_lottery(ctx.guild.id, lottery)
                try:
                    await announce_new_lottery(channel, lottery["prize_pool"])
                except Exception:
                    pass  # best-effort: the lottery is already saved

            await ctx.send(embed=emb("🎰 Lottery Channel", f"Lottery channel set to {channel.mention}\n🎟️ Lottery ready!", C_GREEN))

    # ── !settings channel levelup ─────────────────────────────────────────────
    @cmd_settings_channel.command(name="levelup")
    @requires_perm
    async def settings_channel_levelup(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        chosen = await self._channel_choice(ctx, args, title="📊 Level-Up Channel", current=cfg.get("levelup_channel"), multi=False)
        if chosen is None:
            return
        if not chosen:
            cfg["levelup_channel"] = None
            await save_guild_settings()
            await ctx.send(embed=emb("📊 Level-Up Channel", "Level-up announcements disabled.", C_GREEN))
        else:
            channel = chosen[0]
            cfg["levelup_channel"] = channel.id
            await save_guild_settings()
            await ctx.send(embed=emb("📊 Level-Up Channel", f"Level-up announcements will be sent to {channel.mention}.", C_GREEN))

    # ── !settings channel records ─────────────────────────────────────────────
    @cmd_settings_channel.command(name="records")
    @requires_perm
    async def settings_channel_records(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        chosen = await self._channel_choice(ctx, args, title="🏆 Records Channel", current=cfg.get("records_channel"), multi=False)
        if chosen is None:
            return
        if not chosen:
            cfg.pop("records_channel", None)
            await save_guild_settings()
            await ctx.send(embed=emb(
                "🏆 Records Channel",
                "Records mirror disabled — record announcements will only post in the channel where the event happened.",
                C_GREEN,
            ))
        else:
            channel = chosen[0]
            cfg["records_channel"] = channel.id
            await save_guild_settings()
            await ctx.send(embed=emb(
                "🏆 Records Channel",
                f"{channel.mention} will now show this server's new records **plus** every new global-top record from any server "
                "(in addition to the channel where the event happened).",
                C_GREEN,
            ))

    # ── !settings channel idle ────────────────────────────────────────────────
    @cmd_settings_channel.command(name="idle")
    @requires_perm
    async def settings_channel_idle(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        chosen = await self._channel_choice(ctx, args, title="⚔️ Idle RPG Channel", current=cfg.get("idle_channel"), multi=False)
        if chosen is None:
            return
        if not chosen:
            cfg["idle_channel"] = None
            await save_guild_settings()
            await ctx.send(embed=emb(
                "⚔️ Idle RPG Channel",
                "Idle RPG switched off. Every clock is frozen where it stands until a channel is set again.",
                C_GREEN,
            ))
        else:
            channel = chosen[0]
            cfg["idle_channel"] = channel.id
            await save_guild_settings()
            await ctx.send(embed=emb(
                "⚔️ Idle RPG Channel",
                f"The idle RPG runs in {channel.mention}: news posts there and each player's feed thread opens under it. "
                "I need **Create Public Threads** and **Send Messages in Threads** there. Players start with `!idle join <class>`.",
                C_GREEN,
            ))

    # ── !settings channel feature-request (per-guild, server admin) ──────────
    @cmd_settings_channel.command(name="feature-request")
    @requires_perm
    async def settings_channel_feature_request(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        chosen = await self._channel_choice(ctx, args, title="📖 Feature Request Channel", current=cfg.get("feature_request_channel"), multi=False)
        if chosen is None:
            return
        if not chosen:
            cfg.pop("feature_request_channel", None)
            await save_guild_settings()
            await ctx.send(embed=emb(
                "📖 Feature Request Channel",
                "User feature requests disabled in this server.",
                C_GREEN,
            ))
        else:
            channel = chosen[0]
            # int, like every other per-guild channel setting (old str rows
            # still work — consumers compare via str()/int() coercion).
            cfg["feature_request_channel"] = channel.id
            await save_guild_settings()
            await ctx.send(embed=emb(
                "📖 Feature Request Channel",
                f"Feature requests in this server will be posted to {channel.mention}.",
                C_GREEN,
            ))
            await _post_feature_request_hint(channel)

    # ── !settings channel admin-log (global, bot-admin only) ─────────────────
    @cmd_settings_channel.command(name="admin-log")
    @requires_perm
    async def settings_channel_admin_log(self, ctx: commands.Context, *args):
        chosen = await self._channel_choice(ctx, args, title="🛡️ Admin Log Channel", current=state.bot_settings.get("admin_log_channel"), multi=False)
        if chosen is None:
            return
        if not chosen:
            state.bot_settings.pop("admin_log_channel", None)
            await save_bot_settings()
            await ctx.send(embed=emb("🛡️ Admin Log Channel", "Admin command logging disabled.", C_GREEN))
        else:
            channel = chosen[0]
            # str, unlike the per-guild channel ids: bot_settings is a TEXT
            # column and loads back as str after a reboot.
            state.bot_settings["admin_log_channel"] = str(channel.id)
            await save_bot_settings()
            await ctx.send(embed=emb(
                "🛡️ Admin Log Channel",
                f"Admin command use and errors from **all servers** will be logged to {channel.mention}.",
                C_GREEN,
            ))

    # ── !settings channel error-log (global, bot-admin only) ─────────────────
    @cmd_settings_channel.command(name="error-log")
    @requires_perm
    async def settings_channel_error_log(self, ctx: commands.Context, *args):
        chosen = await self._channel_choice(ctx, args, title="⚠️ Error Log Channel", current=state.bot_settings.get("error_log_channel"), multi=False)
        if chosen is None:
            return
        if not chosen:
            state.bot_settings.pop("error_log_channel", None)
            await save_bot_settings()
            await ctx.send(embed=emb("⚠️ Error Log Channel", "Command error logging disabled.", C_GREEN))
        else:
            channel = chosen[0]
            state.bot_settings["error_log_channel"] = str(channel.id)
            await save_bot_settings()
            await ctx.send(embed=emb(
                "⚠️ Error Log Channel",
                f"Command errors from **all servers** will be logged to {channel.mention}.",
                C_GREEN,
            ))

    # ── !settings channel internal-issue (global, bot-admin only) ────────────
    @cmd_settings_channel.command(name="internal-issue")
    @requires_perm
    async def settings_channel_internal_issue(self, ctx: commands.Context, *args):
        chosen = await self._channel_choice(ctx, args, title="🐛 Internal Issue Channel", current=state.bot_settings.get("internal_issue_channel"), multi=False)
        if chosen is None:
            return
        if not chosen:
            state.bot_settings.pop("internal_issue_channel", None)
            await save_bot_settings()
            await ctx.send(embed=emb("🐛 Internal Issue Channel", "Internal issue routing disabled.", C_GREEN))
        else:
            channel = chosen[0]
            state.bot_settings["internal_issue_channel"] = str(channel.id)
            await save_bot_settings()
            await ctx.send(embed=emb(
                "🐛 Internal Issue Channel",
                f"Bug reports and internal issues from **all servers** will be posted to {channel.mention}.",
                C_GREEN,
            ))

    # ── Per-guild AI model selectors ──────────────────────────────────────────

    @commands.command(name="model")
    @requires_perm
    async def cmd_model(self, ctx: commands.Context, model_name: str = None):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Error", "This command only works in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if model_name is None:
            current = cfg.get("ask_model", OLLAMA_MODEL)
            await ctx.send(embed=emb("⚙️ Model", f"Current model: `{current}`", C_GREY))
            return
        cfg["ask_model"] = model_name
        await save_guild_settings()
        await ctx.send(embed=emb("⚙️ Model", f"Switched to `{model_name}`", C_GREY))


    @commands.command(name="roleplaymodel")
    @requires_perm
    async def cmd_roleplaymodel(self, ctx: commands.Context, model_name: str = None):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Error", "This command only works in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if model_name is None:
            current = cfg.get("roleplay_model", OLLAMA_MODEL)
            await ctx.send(embed=emb("⚙️ Roleplay Model", f"Current roleplay model: `{current}`", C_GREY))
            return
        cfg["roleplay_model"] = model_name
        await save_guild_settings()
        await ctx.send(embed=emb("⚙️ Roleplay Model", f"Switched to `{model_name}`", C_GREY))


    @commands.command(name="codingmodel")
    @requires_perm
    async def cmd_codingmodel(self, ctx: commands.Context, model_name: str = None):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Error", "This command only works in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if model_name is None:
            current = cfg.get("coding_model", OLLAMA_MODEL)
            await ctx.send(embed=emb("⚙️ Coding Model", f"Current coding puzzle model: `{current}`", C_GREY))
            return
        cfg["coding_model"] = model_name
        await save_guild_settings()
        await ctx.send(embed=emb("⚙️ Coding Model", f"Switched to `{model_name}`", C_GREY))


    @commands.command(name="vramtext")
    @requires_perm
    async def cmd_vramtext(self, ctx: commands.Context, *, text: str = None):
        if text is None:
            await ctx.send(embed=emb("⚙️ vRAM Text", state.bot_settings.get("vram_text", "16GB"), C_GREY))
            return
        state.bot_settings["vram_text"] = text
        await save_bot_settings()
        await ctx.send(embed=emb("⚙️ vRAM Text", f"Set to: {text}", C_GREY))


    # ── Per-channel system prompt overrides ───────────────────────────────────

    @commands.command(name="setprompt")
    @requires_perm
    async def cmd_setprompt(self, ctx: commands.Context, *, prompt: str):
        state.channel_prompts[ctx.channel.id] = prompt
        await save_channel_prompts(state.channel_prompts)
        await ctx.send(embed=emb("⚙️ Prompt Updated", "System prompt updated for this channel.", C_GREY))


    @commands.command(name="clearprompt")
    @requires_perm
    async def cmd_clearprompt(self, ctx: commands.Context):
        state.channel_prompts.pop(ctx.channel.id, None)
        await save_channel_prompts(state.channel_prompts)
        await ctx.send(embed=emb("⚙️ Prompt Cleared", "Using default system prompt.", C_GREY))


_FEATURE_REQUEST_HINT_TITLE = "📖 Feature Requests"
_FEATURE_REQUEST_HINT_BODY = (
    "Submit feature ideas with **`!featurerequest <description>`**.\n\n"
    "A bot admin will react ✅ to accept (an internal feature ticket is then "
    "created and tracked here) or ❌ to reject.\n\n"
    "React 👀 on any request to watch it — you'll get a DM when it's "
    "completed or rejected, even if it isn't yours."
)


async def _post_feature_request_hint(channel) -> None:
    """Post the !featurerequest hint embed in `channel` and pin it.

    Both the send and the pin are best-effort — missing Manage Messages or
    a hit Discord pin cap (50) shouldn't block the setting save.
    """
    try:
        msg = await channel.send(embed=emb(
            _FEATURE_REQUEST_HINT_TITLE, _FEATURE_REQUEST_HINT_BODY, C_BLUE,
        ))
    except (discord.Forbidden, discord.HTTPException):
        return
    try:
        await msg.pin()
    except (discord.Forbidden, discord.HTTPException):
        pass


async def setup(bot):
    await bot.add_cog(SettingsCog(bot))
