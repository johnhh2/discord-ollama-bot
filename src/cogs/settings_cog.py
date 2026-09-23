import discord
from discord import app_commands
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_BLUE, C_GREY,
)
from src.economy import (
    drain_bot_balance_into_lottery, announce_new_lottery,
    _ct_now, lottery_month_key,
)
from src.permissions import (
    requires_perm, is_admin, is_silenced, command_permitted,
)
from src.persistence import (
    save_guild_settings, save_bot_settings, save_channel_prompts,
    save_lottery,
    load_lottery
)
from src.guild_config import get_guild_cfg
from src.custom_names import name_conflict
from src.settings_views import (
    Field, list_editor, open_form, pick_channels, pick_from_list, pick_users, toggle_panel,
)
from src.settings_hub import (
    SHOP_ITEMS, channel_rows, channels_in, mentions, open_settings_hub,
)
from src.confirm_view import confirm_choice, confirm_prompt
from src.config import OLLAMA_MODEL, LOTTERY_SEED_POOL
from src.features import FEATURES, set_feature, features_overview
from src.setup_wizard import run_setup_wizard
from src.ai import list_ollama_models
from src import state


class SettingsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _overview_embed(self, ctx: commands.Context) -> discord.Embed:
        """Every setting on one embed — the panel's first page."""
        cfg = get_guild_cfg(ctx.guild.id)
        embed = discord.Embed(title="⚙️ Server Settings", color=C_BLUE)
        embed.add_field(name="🧩 Features", value=features_overview(ctx.guild.id), inline=False)
        rows = channel_rows(cfg, with_global=is_admin(ctx))
        embed.add_field(
            name="📁 Channels",
            value="\n".join(f"{label}: {value}" for label, value, _ in rows),
            inline=False,
        )

        shop_items = cfg.get("shop_items", {})
        embed.add_field(
            name="🛒 Shop items",
            value="  ".join(f"{n} {'✅' if shop_items.get(n, True) else '❌'}" for n in SHOP_ITEMS),
            inline=False,
        )

        nsfw = "✅ on" if cfg.get("nsfw_enabled", False) else "❌ off"
        nsfw += " · channels: " + mentions(cfg.get("nsfw_channels"), "all")
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
        if is_admin(ctx):
            embed.add_field(
                name="🤖 AI (bot admin)",
                value=(
                    f"Ask `{cfg.get('ask_model', OLLAMA_MODEL)}` · roleplay `{cfg.get('roleplay_model', OLLAMA_MODEL)}` · "
                    f"coding `{cfg.get('coding_model', OLLAMA_MODEL)}`\n"
                    f"Passive replies: {'✅ on' if state.bot_settings.get('ai_enabled', True) else '❌ off'} · "
                    f"this channel's prompt: {'custom' if state.channel_prompts.get(ctx.channel.id) else 'default'}"
                ),
                inline=False,
            )
        return embed

    @commands.group(name="settings", aliases=["setting"], invoke_without_command=True,
                    help="Open the settings panel: every server setting, pick a category then a setting")
    @requires_perm
    async def cmd_settings(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        await open_settings_hub(ctx, self, overview=self._overview_embed)

    @app_commands.command(name="settings", description="Open the server settings panel — only you see it")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def slash_settings(self, interaction: discord.Interaction):
        """The same panel, ephemeral. A slash command skips `process_commands`,
        so the gates it would have met are applied here: the blocklist, and
        the `settings` permission tier (answered privately, not with the
        public ❌ embed the prefix gate sends)."""
        if is_silenced(interaction.user.id, interaction.guild_id):
            return
        ctx = await self.bot.get_context(interaction)
        ctx.command = self.cmd_settings
        if not command_permitted(ctx):
            await interaction.response.send_message(embed=emb("❌ No Permission", "", C_RED), ephemeral=True)
            return
        await open_settings_hub(ctx, self, overview=self._overview_embed, ephemeral=True)

    async def run_captured(self, ctx, method: str, *args):
        """Run a settings command in its typed form for the panel and return
        its reply instead of sending it. `method` is a SettingsCog attribute,
        or `bot:<qualified name>` for another cog's command."""
        from src.forwarding import CapturingContext as _CapturingContext
        if method.startswith("bot:"):
            command = self.bot.get_command(method[4:]) if self.bot is not None else None
        else:
            command = getattr(self, method, None)
        if command is None:
            return emb("❌", f"No such setting command: `{method}`", C_RED)
        captured = _CapturingContext(ctx)
        await self._forward(captured, command, *args)
        return captured.captured[-1] if captured.captured else None

    # ── !settings features ────────────────────────────────────────────────────
    @cmd_settings.command(name="features", aliases=["feature"],
                          help="Turn the economy, gambling, savings, assets, shop, artifacts or AI on or off for this server",
                          usage="<economy|gambling|savings|assets|shop|artifacts|ai> <on|off>")
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

    @cmd_settings.group(name="channel", aliases=["channels"], invoke_without_command=True,
                        help="Open the settings panel on the channels page")
    @requires_perm
    async def cmd_settings_channel(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        await open_settings_hub(ctx, self, category="channels", overview=self._overview_embed)

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
        gated on the forwarded path too. A command from another cog runs
        with that cog as `self`."""
        ctx.command = command
        await command.callback(command.cog or self, ctx, *args)

    # ── !settings shop ────────────────────────────────────────────────────────
    @cmd_settings.command(name="shop", help="Turn one of the shop's items on or off for this server",
                          usage="<nickname|role|unassignrole|roleup|roledown|ragebait|buyxp> <on|off>")
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
    @cmd_settings.command(name="leaderboard", help="Set which scope a bare !lb shows by default: server or global",
                          usage="[server|global]")
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
    @cmd_settings.command(name="idle-enroll", help="Auto-enroll members who hold one of the bot's roles into the idle RPG, a few at a time",
                          usage="[on|off]")
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
    @cmd_settings.command(name="idle-pace", help="Set the idle RPG's event pace: lively (default) or the classic IRC odds",
                          usage="[lively|classic]")
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
    @cmd_settings_channel.command(name="bounty", help="Set the channel where !bounty posts, or clear it to disable bounties",
                                  usage="[#channel] | clear")
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
    @cmd_settings_channel.command(name="minecraft", help="Set the channel for Minecraft server up/down alerts and player-count notices",
                                  usage="[#channel] | clear")
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
    @cmd_settings_channel.command(name="dailies", help="Set the self-cleaning channel that holds the react-to-claim dailies embed",
                                  usage="[#channel] | clear")
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
    @cmd_settings.command(name="nsfw", help="Turn NSFW commands on or off, restrict them to channels, or ban tags",
                          usage="[on|off] | channels <set|add|remove|clear|list> [#channel ...] | ban <tag> | unban <tag ...> | banned")
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
                    f"**Channels:** {channels}\n**Banned tags:** {banned}"
                ),
                choices=[
                    {"label": "Turn off" if enabled else "Turn on", "value": "off" if enabled else "on", "default": True},
                    {"label": "Channels", "value": "channels"},
                    {"label": "Ban a tag", "value": "ban"},
                    {"label": "Unban tags", "value": "unban"},
                ],
                payer=ctx.author,
                not_yours="Not your prompt.",
            )
            if picked is None:
                return
            args = (picked,)
        action = args[0].lower()
        usage = "`!settings nsfw on|off` / `channels <set|add|remove|clear|list> [#channel …]` / `ban <tag>` / `unban <tag …>` / `banned`"
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
                    typed_usage="`!settings nsfw channels <set|add|remove|clear|list> [#channel …]`",
                )
                if chosen is None:
                    return
                nsfw_channels[:] = [c.id for c in chosen]
                await save_guild_settings()
                val = " ".join(f"<#{cid}>" for cid in nsfw_channels) or "none"
                await ctx.send(embed=emb("⚙️ NSFW Channels", f"Whitelist is now: {val}", C_GREEN))
                return
            channel_action = args[1].lower()
            named = channels_in(ctx, args[2:])
            if channel_action in ("add", "remove", "set") and not named:
                await ctx.send(embed=emb("⚙️ NSFW", f"Please mention a channel to {channel_action}.", C_GREY))
                return
            if channel_action == "add":
                for channel in named:
                    if channel.id not in nsfw_channels:
                        nsfw_channels.append(channel.id)
                await save_guild_settings()
                await ctx.send(embed=emb("⚙️ NSFW Channels", f"Added {' '.join(ch.mention for ch in named)} to whitelist.", C_GREEN))
            elif channel_action == "remove":
                for channel in named:
                    if channel.id in nsfw_channels:
                        nsfw_channels.remove(channel.id)
                await save_guild_settings()
                await ctx.send(embed=emb("⚙️ NSFW Channels", f"Removed {' '.join(ch.mention for ch in named)} from whitelist.", C_GREEN))
            elif channel_action in ("set", "clear"):
                # The whole list at once — what the panel's picker forwards.
                nsfw_channels[:] = [c.id for c in named] if channel_action == "set" else []
                await save_guild_settings()
                val = " ".join(f"<#{cid}>" for cid in nsfw_channels) or "all channels"
                await ctx.send(embed=emb("⚙️ NSFW Channels", f"Whitelist is now: {val}", C_GREEN))
            elif channel_action == "list":
                val = " ".join(f"<#{cid}>" for cid in nsfw_channels) if nsfw_channels else "none"
                await ctx.send(embed=emb("⚙️ NSFW Channels", val, C_GREY))
            else:
                await ctx.send(embed=emb("⚙️ NSFW", "Usage: `!settings nsfw channels <set|add|remove|clear|list> [#channel …]`", C_GREY))
        elif action == "ban":
            if len(args) < 2:
                values = await open_form(
                    ctx, title="⚙️ NSFW — Ban a Tag", description="Usage: `!settings nsfw ban <tag>`",
                    fields=[Field("tag", "Tag to ban", placeholder="one tag", max_length=100)], button="Ban a tag…",
                )
                if not values or not values.get("tag"):
                    return
                args = ("ban", values["tag"])
            tag = args[1].lower()
            banned = cfg.setdefault("nsfw_banned_tags", [])
            if tag not in banned:
                banned.append(tag)
                await save_guild_settings()
            await ctx.send(embed=emb("⚙️ NSFW", f"Tag `{tag}` banned.", C_GREEN))
        elif action == "unban":
            banned = cfg.get("nsfw_banned_tags", [])
            if len(args) < 2:
                if not banned:
                    await ctx.send(embed=emb("⚙️ NSFW", "No tags are banned.", C_GREY))
                    return
                picked = await pick_from_list(
                    ctx, title="⚙️ NSFW Banned Tags", description="Pick the tags to unban.",
                    options=[(t, t) for t in banned], placeholder="Tags to unban…",
                )
                if not picked:
                    return
                args = ("unban", *picked)
            tags = [t.lower() for t in args[1:]]
            unbanned = [t for t in tags if t in banned]
            for tag in unbanned:
                banned.remove(tag)
            if unbanned:
                await save_guild_settings()
                await ctx.send(embed=emb("⚙️ NSFW", "Unbanned " + ", ".join(f"`{t}`" for t in unbanned) + ".", C_GREEN))
            else:
                await ctx.send(embed=emb("⚙️ NSFW", "None of those tags were banned.", C_GREY))
        elif action == "banned":
            banned = cfg.get("nsfw_banned_tags", [])
            val = ", ".join(f"`{t}`" for t in banned) if banned else "none"
            await ctx.send(embed=emb("⚙️ NSFW Banned Tags", val, C_GREY))
        else:
            await ctx.send(embed=emb("⚙️ NSFW", f"Usage: {usage}", C_GREY))

    async def _remove_aliases(self, ctx, args, aliases: dict, *, title: str) -> None:
        """`remove <word …>`, or a dropdown of the existing aliases when no word is typed."""
        if len(args) >= 2:
            words = [w.lower() for w in args[1:]]
            unknown = [w for w in words if w not in aliases]
            if unknown:
                await ctx.send(embed=emb(title, ", ".join(f"`{w}`" for w in unknown) + " not in the alias list.", C_GREY))
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

    async def _alias_editor(self, ctx, command, aliases: dict, *, title: str, entries, add_fields, to_add) -> None:
        """A bare alias command: the list with Add… / Remove / Clear all.
        The pick is applied through the command's own typed branch."""
        action = await list_editor(
            ctx, title=title,
            description="\n".join(entries) if entries else "No aliases yet — **Add…** makes one.",
            entries=[(f"!{w}", w) for w in aliases], add_title=f"{title} — Add", add_fields=add_fields,
        )
        if not action:
            return
        kind, payload = action
        if kind == "add":
            await self._forward(ctx, command, "add", *to_add(payload))
        elif kind == "remove":
            await self._forward(ctx, command, "remove", *payload)
        else:
            await self._forward(ctx, command, "clear")

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
        named = channels_in(ctx, args)
        if named:
            return named
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
    @cmd_settings.command(name="nsfw-alias", help="Add or remove custom aliases for !nsfw, each optionally pre-filling tags",
                          usage="[add <word> [tags ...] | remove [word ...] | list | clear]")
    @requires_perm
    async def settings_nsfw_alias(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        aliases: dict = cfg.setdefault("nsfw_aliases", {})

        if not args:
            await self._alias_editor(
                ctx, self.settings_nsfw_alias, aliases, title="🔞 NSFW Aliases",
                entries=[f"`!{k}`" + (f" — tags: `{v.get('tags')}`" if isinstance(v, dict) and v.get("tags") else "") for k, v in aliases.items()],
                add_fields=[Field("word", "Alias word", placeholder="pics", max_length=32),
                            Field("tags", "Tags it pre-fills", required=False, placeholder="optional", max_length=200)],
                to_add=lambda v: (v["word"],) + ((v["tags"],) if v.get("tags") else ()),
            )
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
    @cmd_settings.command(name="story-alias", help="Add or remove custom !story aliases, each with its own system prompt",
                          usage="[add <word> <system prompt> | remove [word ...] | list | clear]")
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
            await self._alias_editor(
                ctx, self.settings_story_alias, aliases, title="📖 Story Aliases",
                entries=[f"`!{k}` — {(v[:80] + '…') if isinstance(v, str) and len(v) > 80 else v}" for k, v in aliases.items()],
                add_fields=[Field("word", "Alias word", placeholder="scifi", max_length=32),
                            Field("prompt", "System prompt", kind="paragraph", placeholder="You write hard science fiction…", max_length=2000)],
                to_add=lambda v: (v["word"], v["prompt"]),
            )
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
    @cmd_settings.command(name="quote", help="Let !quote work in any channel, ignoring channel restrictions",
                          usage="[bypass [on|off]]")
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
    @cmd_settings.command(name="soundboard-ratelimit", help="Pick users the AI roasts for spamming the soundboard in voice",
                          usage="<add|remove> [@user|userid ...] | list")
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
    @cmd_settings.command(name="gambler-role", help="Track scratchoff streaks and auto-assign the Gamblers role",
                          usage="[on|off]")
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
    @cmd_settings.command(name="tax-aliases", help="Add or remove custom aliases for the shop's tax, usable as !word @user or !shop word @user",
                          usage="[add <word> [emoji] | remove [word ...] | list | clear]")
    @requires_perm
    async def settings_tax_aliases(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        aliases: dict = cfg.setdefault("tax_aliases", {})

        if not args:
            await self._alias_editor(
                ctx, self.settings_tax_aliases, aliases, title="🏷️ Tax Aliases",
                entries=[f"{v} `!{k}`" for k, v in aliases.items()],
                add_fields=[Field("word", "Alias word", placeholder="rent", max_length=32),
                            Field("emoji", "Emoji shown in the tax message", required=False, placeholder="💰", max_length=8)],
                to_add=lambda v: (v["word"],) + ((v["emoji"],) if v.get("emoji") else ()),
            )
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
    @cmd_settings_channel.command(name="ai", help="Restrict @mentions and AI commands to these channels, or clear to allow all",
                                  usage="[#channel ...] | clear")
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
    @cmd_settings_channel.command(name="whitelist", help="Only allow commands in these channels (!settings still works everywhere), or clear",
                                  usage="[#channel ...] | clear")
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
    @cmd_settings_channel.command(name="blacklist", help="Block commands in these channels, or clear the list",
                                  usage="[#channel ...] | clear")
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
    @cmd_settings_channel.command(name="game", help="Restrict games and gambling to these channels, or clear to allow them everywhere",
                                  usage="[#channel ...] | clear")
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
    @cmd_settings_channel.command(name="chess", help="Restrict chess to these channels (default: the game channels), or clear",
                                  usage="[#channel ...] | clear")
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
    @cmd_settings_channel.command(name="lottery", help="Set the channel where the monthly lottery runs, or clear it to disable the lottery",
                                  usage="[#channel] | clear")
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
    @cmd_settings_channel.command(name="levelup", help="Set the channel for level-up announcements, or clear to disable them",
                                  usage="[#channel] | clear")
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
    @cmd_settings_channel.command(name="records", help="Set a channel that mirrors this server's new records and every global-top record",
                                  usage="[#channel] | clear")
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
    @cmd_settings_channel.command(name="idle", help="Set the idle RPG channel for news and feed threads; the game is off without one",
                                  usage="[#channel] | clear")
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
    @cmd_settings_channel.command(name="feature-request", help="Set the channel where !featurerequest posts, or clear to disable requests here",
                                  usage="[#channel] | clear")
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
    @cmd_settings_channel.command(name="admin-log", help="Set the bot-wide channel that logs admin command use and errors from every server",
                                  usage="[#channel] | clear")
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
    @cmd_settings_channel.command(name="error-log", help="Set the bot-wide channel that logs command errors from every server",
                                  usage="[#channel] | clear")
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
    @cmd_settings_channel.command(name="internal-issue", help="Set the bot-wide channel for bug reports and internal issues from every server",
                                  usage="[#channel] | clear")
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

    async def _model_setting(self, ctx: commands.Context, model_name, *, key: str, title: str, what: str) -> None:
        """One of the three per-guild model settings. Bare, it offers the
        models Ollama has installed; a typed name is taken as is."""
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Error", "This command only works in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        current = cfg.get(key, OLLAMA_MODEL)
        if model_name is None:
            models = await list_ollama_models()
            if not models:
                await ctx.send(embed=emb(title, f"Current {what}: `{current}`\nOllama didn't answer, so type one: `!{ctx.command.qualified_name} <name>`", C_GREY))
                return
            picked = await pick_from_list(
                ctx, title=title, description=f"Current {what}: `{current}`\nPick one of Ollama's installed models.",
                options=[(m + (" (current)" if m == current else ""), m) for m in models], placeholder="Models…", multi=False,
            )
            if not picked:
                return
            model_name = picked[0]
        cfg[key] = model_name
        await save_guild_settings()
        await ctx.send(embed=emb(title, f"Switched to `{model_name}`", C_GREY))

    @commands.command(name="model", help="Change this server's AI model for !ask and @mentions; bare lists the installed models")
    @requires_perm
    async def cmd_model(self, ctx: commands.Context, model_name: str = None):
        await self._model_setting(ctx, model_name, key="ask_model", title="⚙️ Model", what="model")

    @commands.command(name="roleplaymodel", help="Change this server's model for !roleplay and !rpg; bare lists the installed models")
    @requires_perm
    async def cmd_roleplaymodel(self, ctx: commands.Context, model_name: str = None):
        await self._model_setting(ctx, model_name, key="roleplay_model", title="⚙️ Roleplay Model", what="roleplay model")

    @commands.command(name="codingmodel", help="Change this server's model for AI coding puzzles; bare lists the installed models")
    @requires_perm
    async def cmd_codingmodel(self, ctx: commands.Context, model_name: str = None):
        await self._model_setting(ctx, model_name, key="coding_model", title="⚙️ Coding Model", what="coding puzzle model")

    @commands.command(name="vramtext", help="View or set the vRAM text shown in !stats (bot-wide)")
    @requires_perm
    async def cmd_vramtext(self, ctx: commands.Context, *, text: str = None):
        current = state.bot_settings.get("vram_text", "16GB")
        if text is None:
            values = await open_form(
                ctx, title="⚙️ vRAM Text", description=f"Shown in `!stats`. Currently: {current}\nUsage: `!vramtext <text>`",
                fields=[Field("text", "vRAM text", default=current, max_length=100)], button="Change…",
            )
            if not values or not values.get("text"):
                return
            text = values["text"]
        state.bot_settings["vram_text"] = text
        await save_bot_settings()
        await ctx.send(embed=emb("⚙️ vRAM Text", f"Set to: {text}", C_GREY))


    # ── Per-channel system prompt overrides ───────────────────────────────────

    @commands.command(name="setprompt", help="Set a custom AI system prompt for this channel; bare opens a form")
    @requires_perm
    async def cmd_setprompt(self, ctx: commands.Context, *, prompt: str = None):
        current = state.channel_prompts.get(ctx.channel.id)
        if prompt is None:
            values = await open_form(
                ctx, title="⚙️ Channel Prompt",
                description=f"The AI's system prompt in this channel. Currently: {'custom' if current else 'default'}\n"
                            "Usage: `!setprompt <prompt>` / `!clearprompt`",
                fields=[Field("prompt", "System prompt", kind="paragraph", required=False, default=current,
                              placeholder="Leave empty for the default prompt", max_length=2000)],
                button="Edit prompt…",
            )
            if values is None:
                return
            prompt = values.get("prompt", "")
        if not prompt.strip():  # an emptied form clears, like !clearprompt
            await self._forward(ctx, self.cmd_clearprompt)
            return
        state.channel_prompts[ctx.channel.id] = prompt
        await save_channel_prompts(state.channel_prompts)
        await ctx.send(embed=emb("⚙️ Prompt Updated", "System prompt updated for this channel.", C_GREY))


    @commands.command(name="clearprompt", help="Reset this channel's AI system prompt to the default")
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
