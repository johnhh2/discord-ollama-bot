import os
import re

import discord
from discord.ext import commands
from discord import ui

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_PURPLE, C_GREY,
    log_bot_permission_error, MemberConverter,
)
from src.permissions import (
    is_admin, requires_perm,
)
from src.persistence import (
    save_guild_settings,
    save_godmode_users, save_ragebait, save_command_perms,
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
    async def cmd_godmode(self, ctx: commands.Context, user: MemberConverter = None):

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
    async def cmd_adminragebait(self, ctx: commands.Context, target: MemberConverter = None, n: str = None):

        if target is None:
            await ctx.send(embed=emb("❌ Missing User", "Usage: `!adminragebait @user [n]` or `!adminragebait <userid> [n]`", C_RED))
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

        state.active_ragebaits[target.id] = {"remaining": count, "history": []}
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
            await ctx.send("❌ `DISCORD_CLIENT_ID` is not set. Add it to your `.env` to enable this command.")
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
        await self.bot.close()
        os._exit(0)


    @commands.command(name="setperm")
    @requires_perm
    async def cmd_setperm(self, ctx: commands.Context, command_name: str = None, tier: str = None, hidden: str = "false"):
        valid_tiers = ("everyone", "server_admin", "bot_admin")
        if command_name is None or tier is None:
            await ctx.send(embed=emb("⚙️ setperm", "Usage: `!setperm <command> <everyone|server_admin|bot_admin> [true|false]`", C_GOLD))
            return
        if tier not in valid_tiers:
            await ctx.send(embed=emb("❌ Invalid Tier", f"Tier must be one of: {', '.join(valid_tiers)}", C_RED))
            return
        hidden_bool = hidden.lower() in ("true", "1", "yes")
        state.command_perms[command_name] = {"tier": tier, "hidden": hidden_bool}
        await save_command_perms()
        await ctx.send(embed=emb(
            "✅ Permission Updated",
            f"`!{command_name}` → tier: **{tier}**, hidden: **{hidden_bool}**",
            C_GREEN,
        ))


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


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
