import asyncio
import functools

from discord.ext import commands

from src import state
from src.helpers import emb, C_RED, _delete_after
from src.guild_config import get_guild_cfg
def requires_perm(func):
    """Wrap a cog command so it short-circuits with a No Permission embed
    (or silently, if the command_perms entry has hidden=true) when the author
    lacks the configured tier. Equivalent to opening the body with
    `if not await check_command_permission(ctx): return`.
    """
    @functools.wraps(func)
    async def wrapper(self, ctx, *args, **kwargs):
        if not await check_command_permission(ctx):
            return
        return await func(self, ctx, *args, **kwargs)
    return wrapper


async def _wrong_channel_reply(ctx_or_msg, text: str) -> None:
    """Send a ❌ Wrong Channel embed and delete both the trigger and the reply after 10 s."""
    if isinstance(ctx_or_msg, commands.Context):
        message = ctx_or_msg.message
        reply_fn = ctx_or_msg.reply
    else:
        message = ctx_or_msg
        reply_fn = ctx_or_msg.reply
    reply = await reply_fn(embed=emb("❌ Wrong Channel", text, C_RED), mention_author=False)
    asyncio.create_task(_delete_after(message, 10.0))
    asyncio.create_task(_delete_after(reply, 10.0))


async def check_channel(ctx: commands.Context, *config_keys: str, label: str = "These") -> bool:
    """Return True (and send a timed reply) if the current channel is not in the configured channel lists."""
    if not ctx.guild:
        return False
    cfg = get_guild_cfg(ctx.guild.id)
    allowed: set = set()
    for key in config_keys:
        allowed |= set(cfg.get(key, []))
    if allowed and ctx.channel.id not in allowed:
        names = " ".join(f"<#{cid}>" for cid in allowed)
        await _wrong_channel_reply(ctx, f"{label} commands are only allowed in: {names}")
        return True
    return False


async def check_game_channel(ctx: commands.Context, label: str = "Games") -> bool:
    return await check_channel(ctx, "game_channels", label=label)


async def check_ai_channel(ctx: commands.Context) -> bool:
    return await check_channel(ctx, "ai_channels", label="AI")


async def check_puzzle_channel(ctx: commands.Context) -> bool:
    return await check_channel(ctx, "ai_channels", "game_channels", label="Puzzle")


async def check_chess_channel(ctx: commands.Context) -> bool:
    cfg = get_guild_cfg(ctx.guild.id) if ctx.guild else {}
    chess_channels = cfg.get("chess_channels", []) or cfg.get("game_channels", [])
    if chess_channels and ctx.channel.id not in chess_channels:
        names = " ".join(f"<#{cid}>" for cid in chess_channels)
        await _wrong_channel_reply(ctx, f"Chess is only allowed in: {names}")
        return True
    return False


def _override_tier(ctx: commands.Context) -> str | None:
    """Return the per-guild user override tier for ctx.author, or None."""
    if ctx.guild is None:
        return None
    return state.user_perm_overrides.get((ctx.guild.id, ctx.author.id))


def is_admin(ctx: commands.Context) -> bool:
    if ctx.author.id in state.bot_admins:
        return True
    return _override_tier(ctx) == "bot_admin"


def is_server_admin(ctx: commands.Context) -> bool:
    if ctx.guild is None:
        return False
    if ctx.author.guild_permissions.administrator:
        return True
    return _override_tier(ctx) in ("server_admin", "bot_admin")


def can_manage_settings(ctx: commands.Context) -> bool:
    return is_admin(ctx) or is_server_admin(ctx)


def is_bannable(member) -> bool:
    """True if `member` may be added to a blocklist.

    Server admins (Discord administrator role or server_admin/bot_admin
    override in their guild) and bot admins (env BOT_ADMIN_IDS) are
    protected. Bot accounts are allowed.
    """
    if member.id in state.bot_admins:
        return False
    guild_perms = getattr(member, "guild_permissions", None)
    if guild_perms is not None and guild_perms.administrator:
        return False
    guild = getattr(member, "guild", None)
    if guild is not None:
        tier = state.user_perm_overrides.get((guild.id, member.id))
        if tier in ("server_admin", "bot_admin"):
            return False
    return True


def get_command_perm(command_name: str) -> dict:
    perms = state.command_perms
    # Walk from most-specific to least-specific: "settings-channel ai" → "settings-channel" → default
    parts = command_name.split(" ")
    for i in range(len(parts), 0, -1):
        key = " ".join(parts[:i])
        if key in perms:
            return perms[key]
    return {"tier": "everyone", "hidden": False}


async def check_command_permission(ctx: commands.Context) -> bool:
    """Return True if the author may run the command; send error / silently ignore and return False if not."""
    entry = get_command_perm(ctx.command.qualified_name)
    tier = entry.get("tier", "everyone")
    hidden = entry.get("hidden", False)

    if tier == "everyone":
        allowed = True
    elif tier == "server_admin":
        allowed = can_manage_settings(ctx)
    elif tier == "bot_admin":
        allowed = is_admin(ctx)
    else:
        allowed = True

    if not allowed:
        if not hidden:
            await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return False
    return True
