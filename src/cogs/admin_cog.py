import os
import re
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord import ui

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_PURPLE, C_GREY,
    log_bot_permission_error, OptionalMember,
)
from src.permissions import (
    is_admin, is_bannable, requires_perm,
)
from src.persistence import (
    save_guild_settings,
    save_godmode_users, save_ragebait,
    save_user_perm_override, delete_user_perm_override,
    save_blocklist, delete_blocklist,
    save_global_blocklist, delete_global_blocklist,
    save_restart_msg
)
from src.guild_config import get_guild_cfg
from src.config import (
    DISCORD_CLIENT_ID,
)
from src import state



class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="godmode")
    @requires_perm
    async def cmd_godmode(self, ctx: commands.Context, user: OptionalMember = None):

        target_user = user if user else ctx.author
        if target_user.id in state.godmode_users:
            state.godmode_users.remove(target_user.id)
            status = "disabled"
        else:
            state.godmode_users.add(target_user.id)
            status = "enabled"

        await save_godmode_users()
        await ctx.send(embed=emb("👑 Godmode", f"Godmode **{status}** for {target_user.mention}.", C_GOLD))


    @commands.command(name="adminragebait")
    @requires_perm
    async def cmd_adminragebait(self, ctx: commands.Context, target: OptionalMember = None, n: str = None):

        if target is None:
            await ctx.send(embed=emb("❌ Missing User", "Usage: `!adminragebait @user [n]` or `!adminragebait <userid> [n]`", C_RED))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return

        # Parse optional message count (default 5)
        try:
            count = int(n) if n else 5
            if count <= 0:
                await ctx.send(embed=emb("❌ Invalid Count", "Please provide a positive number.", C_RED))
                return
        except ValueError:
            await ctx.send(embed=emb("❌ Invalid Count", f"Could not parse `{n}` as a number.", C_RED))
            return

        state.active_ragebaits[(ctx.guild.id, target.id)] = {"remaining": count, "history": [], "channel_id": ctx.channel.id}
        await save_ragebait()
        await ctx.send(embed=emb(
            "🎭 Ragebait Activated",
            f"Ragebait enabled for user `{target.id}` (next **{count}** message(s))",
            C_PURPLE,
        ))


    @commands.command(name="say")
    @requires_perm
    async def cmd_say(self, ctx: commands.Context, *, text: str = None):
        if text is None:
            await ctx.send(embed=emb("🔊 Say", "Usage: `!say <text>`", C_GREY))
            return
        # Try to delete the command message (fail silently)
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass
        # Send the message
        await ctx.send(text)


    @commands.command(name="botinvitelink", aliases=["botinvite"])
    @requires_perm
    async def cmd_botinvite(self, ctx: commands.Context):

        if not DISCORD_CLIENT_ID:
            await ctx.send(embed=emb("❌ Not Configured", "`DISCORD_CLIENT_ID` is not set. Add it to your `.env` to enable this command.", C_RED))
            return

        permissions = "6192724835560529"
        invite_url = f"https://discord.com/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&permissions={permissions}&integration_type=0&scope=bot"

        # Create a view with a button
        _bot = self.bot
        class InviteView(ui.View):
            @ui.button(label="Get Bot Invitation Link", style=discord.ButtonStyle.primary)
            async def copy_button(self, interaction: discord.Interaction, button: ui.Button):
                # Verify the user clicking the button is an admin
                user_ctx = await _bot.get_context(interaction.message)
                user_ctx.author = interaction.user
                if not is_admin(user_ctx):
                    await interaction.response.send_message("❌ You don't have permission to view this link.", ephemeral=True)
                    return
                await interaction.response.send_message(f"```\n{invite_url}\n```", ephemeral=True)

        embed = discord.Embed(
            title="🤖 Bot Invite Link",
            description="Click the button below to get a copy of the bot invite URL",
            color=discord.Color(0x9932CC)
        )
        embed.add_field(name="Client ID", value=DISCORD_CLIENT_ID, inline=False)
        embed.add_field(name="Permissions", value=permissions, inline=False)

        await ctx.send(embed=embed, view=InviteView())


    @commands.command(name="invitelink")
    @requires_perm
    async def cmd_invite(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return

        try:
            # Try to get vanity URL first (if server has one)
            if ctx.guild.vanity_url:
                invite_url = str(ctx.guild.vanity_url)
            else:
                # Create an invite link
                invite = await ctx.channel.create_invite(max_age=0, max_uses=0)
                invite_url = invite.url

            # Create a view with a button
            class ServerInviteView(ui.View):
                @ui.button(label="Get Server Invitation Link", style=discord.ButtonStyle.primary)
                async def copy_button(self, interaction: discord.Interaction, button: ui.Button):
                    await interaction.response.send_message(f"```\n{invite_url}\n```", ephemeral=True)

            embed = discord.Embed(
                title=f"📩 Invite to {ctx.guild.name}",
                description="Click the button below to get a copy of the server invite URL",
                color=discord.Color(0x9932CC)
            )
            if ctx.guild.icon:
                embed.set_thumbnail(url=ctx.guild.icon.url)

            await ctx.send(embed=embed, view=ServerInviteView())
        except discord.Forbidden:
            log_bot_permission_error(ctx, "create invites")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to create invites in this channel.", C_RED))
        except Exception as e:
            await ctx.send(embed=emb("❌ Error", f"Failed to generate invite: {str(e)}", C_RED))


    @commands.command(name="restart")
    @requires_perm
    async def cmd_restart(self, ctx: commands.Context):
        msg = await ctx.send(embed=emb("🔄 Restarting", "Bot is restarting...", C_GOLD))
        await save_restart_msg(msg.channel.id, msg.id)
        try:
            await self.bot.close()
        finally:
            # Exit even if close() raises — a half-closed process (gateway
            # down, container alive) never gets restarted by the supervisor.
            os._exit(0)


    @commands.command(name="setperm")
    @requires_perm
    async def cmd_setperm(self, ctx: commands.Context, user: OptionalMember = None, tier: str = None):
        """Grant a per-guild permission override to one user.

        Tiers:
          server_admin — treat the user as a server admin in this guild
          bot_admin    — treat the user as a bot admin in this guild
          clear        — remove any existing override for this user in this guild

        The override only adds permission. It cannot revoke permission a user
        already has via Discord server-admin role or BOT_ADMIN_IDS env var;
        clearing the override does not demote those users.
        """
        valid_tiers = ("server_admin", "bot_admin", "clear")
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if user is None or tier is None:
            await ctx.send(embed=emb(
                "⚙️ setperm",
                "Usage: `!setperm @user <server_admin|bot_admin|clear>`\n"
                "• `server_admin` / `bot_admin` — grant this guild-scoped tier to the user\n"
                "• `clear` — remove any existing override for this user in this guild\n\n"
                "Overrides only add permission; they cannot revoke Discord server-admin "
                "role or `BOT_ADMIN_IDS`-granted access.",
                C_GOLD,
            ))
            return
        if tier not in valid_tiers:
            await ctx.send(embed=emb("❌ Invalid Tier", f"Tier must be one of: {', '.join(valid_tiers)}", C_RED))
            return
        if user.bot:
            await ctx.send(embed=emb("❌ Bots Not Allowed", "Permission overrides cannot target bot accounts.", C_RED))
            return

        key = (ctx.guild.id, user.id)
        if tier == "clear":
            had = state.user_perm_overrides.get(key)
            await delete_user_perm_override(ctx.guild.id, user.id)
            state.user_perm_overrides.pop(key, None)
            msg = (
                f"Cleared override for {user.mention} (was **{had}**)."
                if had else f"{user.mention} had no override."
            )
        else:
            await save_user_perm_override(ctx.guild.id, user.id, tier)
            state.user_perm_overrides[key] = tier
            msg = (
                f"{user.mention} now has **{tier}** permission in this server.\n"
                "Note: this only adds permission; the user keeps any access they "
                "already had from their Discord roles or bot-admin status."
            )
        await ctx.send(embed=emb("✅ Permission Updated", msg, C_GREEN))


    @commands.command(name="adminunlock")
    @requires_perm
    async def cmd_adminunlock(self, ctx: commands.Context, target: str = None):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not target:
            await ctx.send(embed=emb("⚙️ adminunlock", "Usage: `!adminunlock <#channel|@role|id>`", C_GOLD))
            return

        ch_match = re.match(r"<#(\d+)>", target)
        role_match = re.match(r"<@&(\d+)>", target)

        if ch_match:
            channel_id = int(ch_match.group(1))
            ch = ctx.guild.get_channel(channel_id)
            if ch is None:
                await ctx.send(embed=emb("❌ Not Found", "Could not find that channel.", C_RED))
                return
            if channel_id not in state.locked_channels:
                await ctx.send(embed=emb("❌ Not Locked", f"{ch.mention} is not locked.", C_RED))
                return
            del state.locked_channels[channel_id]
            cfg = get_guild_cfg(ctx.guild.id)
            cfg.get("locked_channels", {}).pop(str(channel_id), None)
            await save_guild_settings()
            await ctx.send(embed=emb("🔓 Channel Unlocked", f"{ch.mention} has been force-unlocked by an admin.", C_GREEN))
            return

        if role_match:
            role_id = int(role_match.group(1))
            r = ctx.guild.get_role(role_id)
            if r is None:
                await ctx.send(embed=emb("❌ Not Found", "Could not find that role.", C_RED))
                return
            if role_id not in state.locked_roles:
                await ctx.send(embed=emb("❌ Not Locked", f"**{r.name}** is not locked.", C_RED))
                return
            del state.locked_roles[role_id]
            cfg = get_guild_cfg(ctx.guild.id)
            cfg.get("locked_roles", {}).pop(str(role_id), None)
            await save_guild_settings()
            await ctx.send(embed=emb("🔓 Role Unlocked", f"**{r.name}** has been force-unlocked by an admin.", C_GREEN))
            return

        if target.isdigit():
            obj_id = int(target)
            if obj_id in state.locked_channels:
                ch = ctx.guild.get_channel(obj_id)
                del state.locked_channels[obj_id]
                cfg = get_guild_cfg(ctx.guild.id)
                cfg.get("locked_channels", {}).pop(str(obj_id), None)
                await save_guild_settings()
                label = ch.mention if ch else f"channel `{obj_id}`"
                await ctx.send(embed=emb("🔓 Channel Unlocked", f"{label} has been force-unlocked by an admin.", C_GREEN))
                return
            if obj_id in state.locked_roles:
                r = ctx.guild.get_role(obj_id)
                del state.locked_roles[obj_id]
                cfg = get_guild_cfg(ctx.guild.id)
                cfg.get("locked_roles", {}).pop(str(obj_id), None)
                await save_guild_settings()
                label = f"**{r.name}**" if r else f"role `{obj_id}`"
                await ctx.send(embed=emb("🔓 Role Unlocked", f"{label} has been force-unlocked by an admin.", C_GREEN))
                return
            await ctx.send(embed=emb("❌ Not Locked", f"No locked channel or role with ID `{obj_id}`.", C_RED))
            return

        await ctx.send(embed=emb("❌ Invalid Target", "Please supply a `#channel` mention, `@role` mention, or a numeric ID.", C_RED))


    @commands.command(name="ban")
    @requires_perm
    async def cmd_ban(self, ctx: commands.Context, user: OptionalMember = None, *, reason: str = None):
        """Add a user to this guild's blocklist — bot will silently ignore them here."""
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if user is None:
            await ctx.send(embed=emb(
                "🔨 ban",
                "Usage: `!ban @user [reason]`\n"
                "Silently ignores the user inside this server — no AI replies, no command "
                "processing, no XP/economy side effects. They remain in the server.",
                C_GOLD,
            ))
            return
        if not is_bannable(user):
            await ctx.send(embed=emb(
                "❌ Cannot Ban",
                "Server admins and bot admins cannot be banned.",
                C_RED,
            ))
            return

        await save_blocklist(ctx.guild.id, user.id, reason, ctx.author.id)
        state.blocklist[(ctx.guild.id, user.id)] = {
            "reason": reason,
            "banned_by": ctx.author.id,
            "banned_at": datetime.now(timezone.utc),
        }
        msg = f"{user.mention} has been banned in this server."
        if reason:
            msg += f"\nReason: {reason}"
        await ctx.send(embed=emb("🔨 Banned", msg, C_GREEN))


    @commands.command(name="unban")
    @requires_perm
    async def cmd_unban(self, ctx: commands.Context, user: OptionalMember = None):
        """Remove a user from this guild's blocklist."""
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if user is None:
            await ctx.send(embed=emb("🔓 unban", "Usage: `!unban @user`", C_GOLD))
            return

        key = (ctx.guild.id, user.id)
        if key not in state.blocklist:
            await ctx.send(embed=emb("❌ Not Banned", f"{user.mention} is not banned in this server.", C_RED))
            return
        await delete_blocklist(ctx.guild.id, user.id)
        state.blocklist.pop(key, None)
        await ctx.send(embed=emb("🔓 Unbanned", f"{user.mention} has been unbanned in this server.", C_GREEN))


    @commands.command(name="globalban")
    @requires_perm
    async def cmd_globalban(self, ctx: commands.Context, user: OptionalMember = None, *, reason: str = None):
        """Add a user to the bot-wide blocklist — silenced in every guild."""
        if user is None:
            await ctx.send(embed=emb(
                "🌐 globalban",
                "Usage: `!globalban @user [reason]`\n"
                "Silently ignores the user across every guild the bot is in.",
                C_GOLD,
            ))
            return
        if not is_bannable(user):
            await ctx.send(embed=emb(
                "❌ Cannot Ban",
                "Server admins and bot admins cannot be banned.",
                C_RED,
            ))
            return

        await save_global_blocklist(user.id, reason, ctx.author.id)
        state.global_blocklist[user.id] = {
            "reason": reason,
            "banned_by": ctx.author.id,
            "banned_at": datetime.now(timezone.utc),
        }
        msg = f"{user.mention} has been globally banned."
        if reason:
            msg += f"\nReason: {reason}"
        await ctx.send(embed=emb("🌐 Globally Banned", msg, C_GREEN))


    @commands.command(name="globalunban")
    @requires_perm
    async def cmd_globalunban(self, ctx: commands.Context, user: OptionalMember = None):
        """Remove a user from the bot-wide blocklist."""
        if user is None:
            await ctx.send(embed=emb("🌐 globalunban", "Usage: `!globalunban @user`", C_GOLD))
            return
        if user.id not in state.global_blocklist:
            await ctx.send(embed=emb("❌ Not Banned", f"{user.mention} is not globally banned.", C_RED))
            return
        await delete_global_blocklist(user.id)
        state.global_blocklist.pop(user.id, None)
        await ctx.send(embed=emb("🌐 Globally Unbanned", f"{user.mention} has been globally unbanned.", C_GREEN))


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
