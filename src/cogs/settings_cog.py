
import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_BLUE, C_GREY,
    send_ephemeral,
)
from src.economy import (
    drain_bot_balance_into_lottery, announce_new_lottery,
    _ct_now, lottery_week_key,
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
from src.config import OLLAMA_MODEL
from src import state


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
        ai_channels = cfg.get("ai_channels", [])
        cmd_whitelist = cfg.get("command_whitelist", [])
        cmd_blacklist = cfg.get("command_blacklist", [])
        game_channels = cfg.get("game_channels", [])
        chess_channels = cfg.get("chess_channels", [])
        shop_items = cfg.get("shop_items", {})
        nsfw_enabled = cfg.get("nsfw_enabled", False)
        nsfw_channels = cfg.get("nsfw_channels", [])
        nsfw_banned = cfg.get("nsfw_banned_tags", [])
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
        nsfw_val = ("✅ enabled" if nsfw_enabled else "❌ disabled")
        nsfw_ch_val = " ".join(f"<#{c}>" for c in nsfw_channels) if nsfw_channels else "all channels"
        nsfw_val += f"\nChannels: {nsfw_ch_val}"
        if nsfw_banned:
            nsfw_val += f"\nBanned tags: {', '.join(nsfw_banned)}"
        nsfw_aliases = cfg.get("nsfw_aliases", {})
        if nsfw_aliases:
            nsfw_val += "\nAliases: " + ", ".join(f"`!{k}`" for k in nsfw_aliases)
        lottery_val = f"<#{lottery_channel_id}>" if lottery_channel_id else "❌ disabled"
        levelup_channel_id = cfg.get("levelup_channel")
        levelup_val = f"<#{levelup_channel_id}>" if levelup_channel_id else "❌ disabled"
        feature_req_channel_id = cfg.get("feature_request_channel")
        feature_req_val = f"<#{feature_req_channel_id}>" if feature_req_channel_id else "❌ disabled"
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
        tax_aliases = cfg.get("tax_aliases", {})
        tax_aliases_val = ", ".join(f"{v} `!{k}`" for k, v in tax_aliases.items()) if tax_aliases else "none"
        story_aliases = cfg.get("story_aliases", {})
        story_aliases_val = ", ".join(f"`!{k}`" for k in story_aliases) if story_aliases else "none"

        embed = discord.Embed(title="⚙️ Server Settings", color=C_BLUE)
        embed.add_field(name="🤖 AI channels", value=ai_val, inline=False)
        embed.add_field(name="✅ Channel whitelist", value=whitelist_val, inline=False)
        embed.add_field(name="❌ Channel blacklist", value=blacklist_val, inline=False)
        embed.add_field(name="🎮 Game channels", value=game_val, inline=False)
        embed.add_field(name="♟️ Chess channels", value=chess_val, inline=False)
        embed.add_field(name="🛒 Shop items", value=shop_val, inline=False)
        embed.add_field(name="🔞 NSFW", value=nsfw_val, inline=False)
        embed.add_field(name="🎰 Lottery channel", value=lottery_val, inline=False)
        embed.add_field(name="📊 Level-up channel", value=levelup_val, inline=False)
        embed.add_field(name="📖 Feature request channel", value=feature_req_val, inline=False)
        embed.add_field(name="🔇 Soundboard rate-limit", value=rl_val, inline=False)
        embed.add_field(name="🎲 Gambler role", value=gambler_role_val, inline=False)
        embed.add_field(name="🏷️ Tax aliases", value=tax_aliases_val, inline=False)
        embed.add_field(name="📖 Story aliases", value=story_aliases_val, inline=False)
        if is_admin(ctx):
            admin_log_id = state.bot_settings.get("admin_log_channel")
            admin_log_val = f"<#{admin_log_id}>" if admin_log_id else "❌ disabled"
            embed.add_field(name="🛡️ Admin log channel (global)", value=admin_log_val, inline=False)
            error_log_id = state.bot_settings.get("error_log_channel")
            error_log_val = f"<#{error_log_id}>" if error_log_id else "❌ disabled"
            embed.add_field(name="⚠️ Error log channel (global)", value=error_log_val, inline=False)
            issue_chan_id = state.bot_settings.get("internal_issue_channel")
            issue_chan_val = f"<#{issue_chan_id}>" if issue_chan_id else "❌ disabled"
            embed.add_field(name="🐛 Internal issue channel (global)", value=issue_chan_val, inline=False)
        footer_text = (
            "Subcommands:\n"
            "ai-channels #ch... / clear\n"
            "cmd-whitelist #ch... / clear\n"
            "cmd-blacklist #ch... / clear\n"
            "game-channels #ch... / clear\n"
            "chess-channels #ch... / clear\n"
            "shop <item> on|off\n"
            "nsfw on|off / channels add|remove|list / ban <tag> / unban <tag> / banned\n"
            "nsfw-alias add|remove <word> [tags...] / list / clear\n"
            "lottery-channel #channel / clear\n"
            "soundboard-ratelimit add|remove @user|<userid> / list\n"
            "gambler-role on|off\n"
            "channel-levelup #channel / clear\n"
            "tax-aliases add|remove <word> / list / clear\n"
            "feature-request-channel #channel / clear"
        )
        if is_admin(ctx):
            footer_text += (
                "\n\nBot admin:\n"
                "admin-log-channel #channel / clear\n"
                "error-log-channel #channel / clear\n"
                "internal-issue-channel #channel / clear"
            )
        embed.set_footer(text=footer_text)
        await send_ephemeral(ctx, embed=embed)

    # ── !settings ai-channels ─────────────────────────────────────────────────
    @cmd_settings.command(name="ai-channels")
    @requires_perm
    async def settings_ai_channels(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if args and args[0].lower() == "clear":
            cfg["ai_channels"] = []
            await save_guild_settings()
            await ctx.send(embed=emb("⚙️ AI Channels", "AI channel restriction removed — all channels allowed.", C_GREEN))
        elif ctx.message.channel_mentions:
            cfg["ai_channels"] = [c.id for c in ctx.message.channel_mentions]
            await save_guild_settings()
            names = " ".join(c.mention for c in ctx.message.channel_mentions)
            await ctx.send(embed=emb("⚙️ AI Channels", f"AI commands restricted to: {names}", C_GREEN))
        else:
            await ctx.send(embed=emb("⚙️ AI Channels", "Usage: `!settings ai-channels #channel ...` or `!settings ai-channels clear`", C_GREY))

    # ── !settings cmd-whitelist ───────────────────────────────────────────────
    @cmd_settings.command(name="cmd-whitelist")
    @requires_perm
    async def settings_cmd_whitelist(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if args and args[0].lower() == "clear":
            cfg["command_whitelist"] = []
            await save_guild_settings()
            await ctx.send(embed=emb("✅ Channel Whitelist", "Whitelist removed — commands allowed in all channels.", C_GREEN))
        elif ctx.message.channel_mentions:
            cfg["command_whitelist"] = [c.id for c in ctx.message.channel_mentions]
            await save_guild_settings()
            names = " ".join(c.mention for c in ctx.message.channel_mentions)
            await ctx.send(embed=emb("✅ Channel Whitelist", f"Commands restricted to: {names}\n(Note: `!settings` always works everywhere)", C_GREEN))
        else:
            await ctx.send(embed=emb("✅ Channel Whitelist", "Usage: `!settings cmd-whitelist #channel ...` or `!settings cmd-whitelist clear`", C_GREY))

    # ── !settings cmd-blacklist ───────────────────────────────────────────────
    @cmd_settings.command(name="cmd-blacklist")
    @requires_perm
    async def settings_cmd_blacklist(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if args and args[0].lower() == "clear":
            cfg["command_blacklist"] = []
            await save_guild_settings()
            await ctx.send(embed=emb("❌ Channel Blacklist", "Blacklist cleared — commands allowed in all channels.", C_GREEN))
        elif ctx.message.channel_mentions:
            cfg["command_blacklist"] = [c.id for c in ctx.message.channel_mentions]
            await save_guild_settings()
            names = " ".join(c.mention for c in ctx.message.channel_mentions)
            await ctx.send(embed=emb("❌ Channel Blacklist", f"Commands blocked in: {names}", C_GREEN))
        else:
            await ctx.send(embed=emb("❌ Channel Blacklist", "Usage: `!settings cmd-blacklist #channel ...` or `!settings cmd-blacklist clear`", C_GREY))

    # ── !settings chess-channels ──────────────────────────────────────────────
    @cmd_settings.command(name="chess-channels")
    @requires_perm
    async def settings_chess_channels(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if args and args[0].lower() == "clear":
            cfg["chess_channels"] = []
            await save_guild_settings()
            await ctx.send(embed=emb("♟️ Chess Channels", "Chess channel restriction removed — all channels allowed.", C_GREEN))
        elif ctx.message.channel_mentions:
            cfg["chess_channels"] = [c.id for c in ctx.message.channel_mentions]
            await save_guild_settings()
            names = " ".join(c.mention for c in ctx.message.channel_mentions)
            await ctx.send(embed=emb("♟️ Chess Channels", f"Chess restricted to: {names}", C_GREEN))
        else:
            await ctx.send(embed=emb("♟️ Chess Channels", "Usage: `!settings chess-channels #channel ...` or `!settings chess-channels clear`", C_GREY))

    # ── !settings game-channels ───────────────────────────────────────────────
    @cmd_settings.command(name="game-channels")
    @requires_perm
    async def settings_game_channels(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if args and args[0].lower() == "clear":
            cfg["game_channels"] = []
            await save_guild_settings()
            await ctx.send(embed=emb("🎮 Game Channels", "Game channel restriction removed — games and gambling allowed everywhere.", C_GREEN))
        elif ctx.message.channel_mentions:
            cfg["game_channels"] = [c.id for c in ctx.message.channel_mentions]
            await save_guild_settings()
            names = " ".join(c.mention for c in ctx.message.channel_mentions)
            await ctx.send(embed=emb("🎮 Game Channels", f"Games and gambling restricted to: {names}", C_GREEN))
        else:
            await ctx.send(embed=emb("🎮 Game Channels", "Usage: `!settings game-channels #channel ...` or `!settings game-channels clear`", C_GREY))

    # ── !settings shop ────────────────────────────────────────────────────────
    @cmd_settings.command(name="shop")
    @requires_perm
    async def settings_shop(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
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
        await save_guild_settings()
        status = "✅ enabled" if enabled else "❌ disabled"
        await ctx.send(embed=emb("⚙️ Shop", f"**{item}** is now {status}.", C_GREEN))

    # ── !settings nsfw ────────────────────────────────────────────────────────
    @cmd_settings.command(name="nsfw")
    @requires_perm
    async def settings_nsfw(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if not args:
            await ctx.send(embed=emb("⚙️ NSFW", "Usage: `!settings nsfw on|off` / `channels <add|remove|list> [#channel]` / `ban <tag>` / `unban <tag>` / `banned`", C_GREY))
            return
        action = args[0].lower()
        if action in ("on", "off"):
            cfg["nsfw_enabled"] = (action == "on")
            await save_guild_settings()
            status = "✅ enabled" if action == "on" else "❌ disabled"
            await ctx.send(embed=emb("⚙️ NSFW", f"NSFW commands are now {status}.", C_GREEN))
        elif action == "channels":
            if len(args) < 2:
                await ctx.send(embed=emb("⚙️ NSFW", "Usage: `!settings nsfw channels <add|remove|list> [#channel]`", C_GREY))
                return
            channel_action = args[1].lower()
            nsfw_channels = cfg.setdefault("nsfw_channels", [])
            if channel_action == "add":
                if not ctx.message.channel_mentions:
                    await ctx.send(embed=emb("⚙️ NSFW", "Please mention a channel to add.", C_GREY))
                    return
                for channel in ctx.message.channel_mentions:
                    if channel.id not in nsfw_channels:
                        nsfw_channels.append(channel.id)
                await save_guild_settings()
                names = " ".join(f"<#{cid}>" for cid in ctx.message.channel_mentions)
                await ctx.send(embed=emb("⚙️ NSFW Channels", f"Added {names} to whitelist.", C_GREEN))
            elif channel_action == "remove":
                if not ctx.message.channel_mentions:
                    await ctx.send(embed=emb("⚙️ NSFW", "Please mention a channel to remove.", C_GREY))
                    return
                for channel in ctx.message.channel_mentions:
                    if channel.id in nsfw_channels:
                        nsfw_channels.remove(channel.id)
                await save_guild_settings()
                names = " ".join(f"<#{cid}>" for cid in ctx.message.channel_mentions)
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
            tags = " ".join(args[2:]) if len(args) > 2 else ""
            aliases[word] = {"tags": tags}
            await save_guild_settings()
            tag_info = f" (pre-fills tags: `{tags}`)" if tags else ""
            await ctx.send(embed=emb("🔞 NSFW Aliases", f"Added `!{word}`{tag_info}.", C_GREEN))

        elif action == "remove":
            if len(args) < 2:
                await ctx.send(embed=emb("⚙️ NSFW Aliases", "Usage: `!settings nsfw-alias remove <word>`", C_GREY))
                return
            word = args[1].lower()
            if word not in aliases:
                await ctx.send(embed=emb("🔞 NSFW Aliases", f"`{word}` is not in the alias list.", C_GREY))
                return
            del aliases[word]
            await save_guild_settings()
            await ctx.send(embed=emb("🔞 NSFW Aliases", f"Removed `{word}`.", C_GREEN))

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
            if len(args) < 2:
                await ctx.send(embed=emb("⚙️ Story Aliases", "Usage: `!settings story-alias remove <word>`", C_GREY))
                return
            word = args[1].lower()
            if word not in aliases:
                await ctx.send(embed=emb("📖 Story Aliases", f"`{word}` is not in the alias list.", C_GREY))
                return
            del aliases[word]
            await save_guild_settings()
            await ctx.send(embed=emb("📖 Story Aliases", f"Removed `{word}`.", C_GREEN))

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
                await save_guild_settings()
                status = "✅ enabled" if bypass_action == "on" else "❌ disabled"
                await ctx.send(embed=emb("⚙️ quote", f"Quote bypass is now {status} (quote works in any channel).", C_GREEN))
            else:
                await ctx.send(embed=emb("⚙️ quote", "Usage: `!settings quote bypass on|off`", C_GREY))
        else:
            await ctx.send(embed=emb("⚙️ quote", "Usage: `!settings quote bypass on|off`", C_GREY))

    # ── !settings lottery-channel ─────────────────────────────────────────────
    @cmd_settings.command(name="lottery-channel")
    @requires_perm
    async def settings_lottery_channel(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if args and args[0].lower() == "clear":
            cfg["lottery_channel"] = None
            await save_guild_settings()
            await ctx.send(embed=emb("🎰 Lottery Channel", "Lottery disabled.", C_GREEN))
        elif ctx.message.channel_mentions:
            channel = ctx.message.channel_mentions[0]
            cfg["lottery_channel"] = channel.id
            await save_guild_settings()

            current_week = lottery_week_key(_ct_now())
            lottery = await load_lottery(ctx.guild.id)
            if lottery.get("last_posted_week", 0) != current_week:
                lottery = {"prize_pool": 2000, "players": {}, "last_posted_week": current_week}
                await drain_bot_balance_into_lottery(lottery, ctx.guild.id)
                await save_lottery(ctx.guild.id, lottery)
                try:
                    await announce_new_lottery(channel, lottery["prize_pool"])
                except Exception:
                    pass

            await ctx.send(embed=emb("🎰 Lottery Channel", f"Lottery channel set to {channel.mention}\n🎟️ Lottery ready!", C_GREEN))
        else:
            await ctx.send(embed=emb("🎰 Lottery Channel", "Usage: `!settings lottery-channel #channel` or `!settings lottery-channel clear`", C_GREY))

    # ── !settings admin-log-channel (global, bot-admin only) ─────────────────
    @cmd_settings.command(name="admin-log-channel")
    @requires_perm
    async def settings_admin_log_channel(self, ctx: commands.Context, *args):
        if args and args[0].lower() == "clear":
            state.bot_settings.pop("admin_log_channel", None)
            await save_bot_settings()
            await ctx.send(embed=emb("🛡️ Admin Log Channel", "Admin command logging disabled.", C_GREEN))
        elif ctx.message.channel_mentions:
            channel = ctx.message.channel_mentions[0]
            state.bot_settings["admin_log_channel"] = str(channel.id)
            await save_bot_settings()
            await ctx.send(embed=emb(
                "🛡️ Admin Log Channel",
                f"Admin command use and errors from **all servers** will be logged to {channel.mention}.",
                C_GREEN,
            ))
        else:
            await ctx.send(embed=emb(
                "🛡️ Admin Log Channel",
                "Usage: `!settings admin-log-channel #channel` or `!settings admin-log-channel clear`",
                C_GREY,
            ))

    # ── !settings error-log-channel (global, bot-admin only) ─────────────────
    @cmd_settings.command(name="error-log-channel")
    @requires_perm
    async def settings_error_log_channel(self, ctx: commands.Context, *args):
        if args and args[0].lower() == "clear":
            state.bot_settings.pop("error_log_channel", None)
            await save_bot_settings()
            await ctx.send(embed=emb("⚠️ Error Log Channel", "Command error logging disabled.", C_GREEN))
        elif ctx.message.channel_mentions:
            channel = ctx.message.channel_mentions[0]
            state.bot_settings["error_log_channel"] = str(channel.id)
            await save_bot_settings()
            await ctx.send(embed=emb(
                "⚠️ Error Log Channel",
                f"Command errors from **all servers** will be logged to {channel.mention}.",
                C_GREEN,
            ))
        else:
            await ctx.send(embed=emb(
                "⚠️ Error Log Channel",
                "Usage: `!settings error-log-channel #channel` or `!settings error-log-channel clear`",
                C_GREY,
            ))

    # ── !settings feature-request-channel (per-guild, server admin) ─────────
    @cmd_settings.command(name="feature-request-channel")
    @requires_perm
    async def settings_feature_request_channel(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if args and args[0].lower() == "clear":
            cfg.pop("feature_request_channel", None)
            await save_guild_settings()
            await ctx.send(embed=emb(
                "📖 Feature Request Channel",
                "User feature requests disabled in this server.",
                C_GREEN,
            ))
        elif ctx.message.channel_mentions:
            channel = ctx.message.channel_mentions[0]
            cfg["feature_request_channel"] = str(channel.id)
            await save_guild_settings()
            await ctx.send(embed=emb(
                "📖 Feature Request Channel",
                f"Feature requests in this server will be posted to {channel.mention}.",
                C_GREEN,
            ))
            await _post_feature_request_hint(channel)
        else:
            await ctx.send(embed=emb(
                "📖 Feature Request Channel",
                "Usage: `!settings feature-request-channel #channel` or `!settings feature-request-channel clear`",
                C_GREY,
            ))

    # ── !settings internal-issue-channel (global, bot-admin only) ────────────
    @cmd_settings.command(name="internal-issue-channel")
    @requires_perm
    async def settings_internal_issue_channel(self, ctx: commands.Context, *args):
        if args and args[0].lower() == "clear":
            state.bot_settings.pop("internal_issue_channel", None)
            await save_bot_settings()
            await ctx.send(embed=emb("🐛 Internal Issue Channel", "Internal issue routing disabled.", C_GREEN))
        elif ctx.message.channel_mentions:
            channel = ctx.message.channel_mentions[0]
            state.bot_settings["internal_issue_channel"] = str(channel.id)
            await save_bot_settings()
            await ctx.send(embed=emb(
                "🐛 Internal Issue Channel",
                f"Bug reports and internal issues from **all servers** will be posted to {channel.mention}.",
                C_GREEN,
            ))
        else:
            await ctx.send(embed=emb(
                "🐛 Internal Issue Channel",
                "Usage: `!settings internal-issue-channel #channel` or `!settings internal-issue-channel clear`",
                C_GREY,
            ))

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
        if not args or args[0].lower() not in ("on", "off"):
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

    # ── !settings channel-levelup ─────────────────────────────────────────────
    @cmd_settings.command(name="channel-levelup")
    @requires_perm
    async def settings_channel_levelup(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if args and args[0].lower() == "clear":
            cfg["levelup_channel"] = None
            await save_guild_settings()
            await ctx.send(embed=emb("📊 Level-Up Channel", "Level-up announcements disabled.", C_GREEN))
        elif ctx.message.channel_mentions:
            channel = ctx.message.channel_mentions[0]
            cfg["levelup_channel"] = channel.id
            await save_guild_settings()
            await ctx.send(embed=emb("📊 Level-Up Channel", f"Level-up announcements will be sent to {channel.mention}.", C_GREEN))
        else:
            await ctx.send(embed=emb("📊 Level-Up Channel", "Usage: `!settings channel-levelup #channel` or `!settings channel-levelup clear`", C_GREY))


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
            emoji = args[2] if len(args) > 2 else "💰"
            aliases[word] = emoji
            await save_guild_settings()
            await ctx.send(embed=emb("🏷️ Tax Aliases", f"Added {emoji} `!{word}`. Users can now use `!shop {word} @user` or `!{word} @user`.", C_GREEN))

        elif action == "remove":
            if len(args) < 2:
                await ctx.send(embed=emb("⚙️ Tax Aliases", "Usage: `!settings tax-aliases remove <word>`", C_GREY))
                return
            word = args[1].lower()
            if word not in aliases:
                await ctx.send(embed=emb("🏷️ Tax Aliases", f"`{word}` is not in the alias list.", C_GREY))
                return
            del aliases[word]
            await save_guild_settings()
            await ctx.send(embed=emb("🏷️ Tax Aliases", f"Removed `{word}`.", C_GREEN))

        else:
            await ctx.send(embed=emb("⚙️ Tax Aliases", "Usage: `!settings tax-aliases add|remove <word> [emoji]` / `list` / `clear`", C_GREY))


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
    "created and tracked here) or ❌ to reject."
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
