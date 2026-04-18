import asyncio

import discord
from discord.ext import commands

from src.helpers import emb, C_RED, _delete_after
from src.persistence import get_guild_cfg


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


def is_admin(ctx: commands.Context) -> bool:
    from src import state
    return ctx.author.id in state.bot_admins


def is_server_admin(ctx: commands.Context) -> bool:
    return ctx.guild is not None and ctx.author.guild_permissions.administrator


def can_manage_settings(ctx: commands.Context) -> bool:
    return is_admin(ctx) or is_server_admin(ctx)


def get_command_perm(command_name: str) -> dict:
    from src import state
    return state.command_perms.get(command_name, {"tier": "everyone", "hidden": False})


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


def check_rate_limit(user_id: int) -> bool:
    import time
    from src import state
    from src.config import RATE_LIMIT_SECONDS
    now = time.monotonic()
    last = state.user_last_request.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    state.user_last_request[user_id] = now
    return False
