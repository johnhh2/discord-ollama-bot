import asyncio
import os
import subprocess
import time

import discord
from discord.ext import commands

from src.config import RACE_TRACK_LEN, EPHEMERAL_DELETE_AFTER, EPHEMERAL_MSG_FILE
from src.persistence import _load_json, _save_json

# ── Embed colors ──────────────────────────────────────────────────────────────

C_GREEN  = 0x2ecc71  # win, success, economy
C_RED    = 0xe74c3c  # loss, error
C_GOLD   = 0xf1c40f  # gambling neutral, admin, cooldown
C_ORANGE = 0xe67e22  # games in-progress
C_BLUE   = 0x3498db  # blackjack in-progress, info
C_PURPLE = 0x9b59b6  # shop
C_GREY   = 0x95a5a6  # utility, neutral


def emb(title: str, description: str, color: int) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def mocking_font(text: str) -> str:
    """Convert text to mocking alternating case: LiKe ThIs."""
    result = []
    uppercase = False
    for char in text:
        if char.isalpha():
            result.append(char.upper() if uppercase else char.lower())
            uppercase = not uppercase
        else:
            result.append(char)
    return "".join(result)


def curse_font(text: str) -> str:
    """Convert text to cursed alternating case: tHiS iS cUrSeD."""
    result = []
    uppercase = True
    for char in text:
        if char.isalpha():
            result.append(char.upper() if uppercase else char.lower())
            uppercase = not uppercase
        else:
            result.append(char)
    return "".join(result)


def get_memory_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 0.0


def format_uptime() -> str:
    from src import state
    seconds = int(time.monotonic() - state.bot_start_time)
    days, r = divmod(seconds, 86400)
    hours, r = divmod(r, 3600)
    minutes = r // 60
    return f"{days}d {hours}h {minutes}m"


def get_version() -> str:
    try:
        commit_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        commit_count = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return f"{commit_count} ({commit_hash})"
    except Exception:
        return "unknown"


def get_system_prompt(channel_id: int) -> str:
    from src import state
    from src.config import SYSTEM_PROMPT
    return state.channel_prompts.get(channel_id, SYSTEM_PROMPT)


def log_bot_permission_error(ctx: commands.Context, error_msg: str):
    from src import state
    state.audit_log.append({
        "time": time.time(),
        "user": f"{ctx.author.display_name} ({ctx.author.id})",
        "command": ctx.message.content[:100],
        "error": f"Bot Permission Error: {error_msg}",
    })


def _log_audit(user: str, command: str, error: str):
    from src import state
    state.audit_log.append({
        "time": time.time(),
        "user": user,
        "command": command,
        "error": error,
    })


async def send_ephemeral(ctx: commands.Context, *args, **kwargs) -> discord.Message:
    """Send a message with delete_after=60 and register it for cleanup on restart."""
    kwargs["delete_after"] = EPHEMERAL_DELETE_AFTER
    msg = await ctx.send(*args, **kwargs)
    records = _load_json(EPHEMERAL_MSG_FILE, [])
    records.append({"channel_id": msg.channel.id, "message_id": msg.id})
    _save_json(EPHEMERAL_MSG_FILE, records)
    return msg


async def parse_amount(
    ctx: commands.Context, value: str, min_val: int = 1,
    error_msg: str = "Please provide a positive whole number amount."
) -> "int | None":
    """Parse a string as a positive integer >= min_val.

    Accepts plain integers or percentage strings like '50%', which resolve
    to that percentage of the caller's current balance.
    """
    from src.economy import get_balance

    resolved = value.strip()

    if resolved.endswith("%"):
        try:
            pct = float(resolved[:-1])
            assert 0 < pct <= 100
            balance = get_balance(ctx.author.id)
            amount = max(0, int(balance * pct / 100))
        except (ValueError, AssertionError):
            await ctx.send("Percentage must be between 1% and 100%.")
            return None
    else:
        try:
            amount = int(resolved)
        except ValueError:
            if error_msg:
                await ctx.send(error_msg)
            return None

    if amount < min_val:
        if error_msg:
            await ctx.send(error_msg)
        return None
    return amount


def resolve_role(guild: discord.Guild, token: str) -> "discord.Role | None":
    """Resolve a role from a mention (<@&ID>) or plain name string."""
    token = token.strip()
    if token.startswith("<@&") and token.endswith(">"):
        try:
            role_id = int(token[3:-1])
            return guild.get_role(role_id)
        except ValueError:
            return None
    return discord.utils.get(guild.roles, name=token)


async def fetch_member(guild: discord.Guild, user_id: int) -> "discord.Member | None":
    """Return a guild member by ID, falling back to an API fetch if not cached."""
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.HTTPException):
            return None
    return member


class MemberConverter(commands.Converter):
    """Accepts a mention, user ID, or case-insensitive display name / username substring."""

    async def convert(self, ctx: commands.Context, argument: str) -> discord.Member:
        # Try built-in converter first (handles mentions and exact IDs)
        try:
            return await commands.MemberConverter().convert(ctx, argument)
        except commands.BadArgument:
            pass

        if ctx.guild is None:
            raise commands.BadArgument(f"Member '{argument}' not found.")

        # Case-insensitive substring match against display name and username
        query = argument.lower()
        matches = [
            m for m in ctx.guild.members
            if query in m.display_name.lower() or query in m.name.lower()
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = ", ".join(m.display_name for m in matches[:5])
            raise commands.BadArgument(f"'{argument}' matched multiple members: {names}")
        raise commands.BadArgument(f"Member '{argument}' not found.")


async def toggle_member_role(
    member: discord.Member, role: discord.Role, add: bool, reason: str = ""
) -> bool:
    """Add or remove *role* from *member*. Returns True on success."""
    try:
        if add:
            if role not in member.roles:
                await member.add_roles(role, reason=reason)
        else:
            if role in member.roles:
                await member.remove_roles(role, reason=reason)
        return True
    except Exception:
        return False


async def shop_charge(
    ctx: commands.Context, uid: int, cost: int, cost_label: str = None
) -> bool:
    """Check and deduct *cost* coins for a shop action. Returns True if the user can proceed."""
    from src import state
    from src.economy import deduct_balance, get_balance
    if uid in state.godmode_users or cost == 0:
        return True
    if not deduct_balance(uid, cost):
        label_str = f"This costs **{cost_label or f'{cost:,}'} 🪙**. "
        await ctx.send(embed=emb(
            "💸 Insufficient Funds",
            f"{label_str}Balance: {get_balance(uid)} 🪙",
            C_RED,
        ))
        return False
    return True


def _render_race(game: dict) -> str:
    """Render the race board with each player's lane."""
    lines = []
    for uid in game["players"]:
        pos = game["positions"][uid]
        name = game["names"][uid]
        track = "▓" * pos + "🏇" + "░" * (RACE_TRACK_LEN - pos)
        lines.append(f"`{track}` **{name}**")
    return "\n".join(lines)


async def _delete_after(message: discord.Message, delay: float = 5.0):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden):
        pass


async def _edit_board(
    channel: discord.abc.Messageable, game: dict, embed: discord.Embed
):
    """Edit the persistent board message in-place; falls back to a new send if deleted."""
    try:
        msg = await channel.fetch_message(game["board_msg_id"])
        await msg.edit(embed=embed)
    except (discord.NotFound, discord.HTTPException):
        await channel.send(embed=embed)
