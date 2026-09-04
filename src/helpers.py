import asyncio
import os
import re
import subprocess
import time

import discord
from discord.ext import commands

from src import state
from src.config import RACE_TRACK_LEN, EPHEMERAL_DELETE_AFTER
from src.persistence import add_ephemeral_msg

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


RECORD_LABELS = {
    "highest_balance": "highest balance (wallet + savings)",
    "flip": "biggest flip payout",
    "race": "biggest race payout",
    "slots_jackpot": "biggest slots jackpot payout",
    "slots_non_jackpot": "biggest non-jackpot slots payout",
    "lottery": "biggest lottery payout",
    "blackjack": "biggest blackjack payout",
    "hangman_payout": "biggest hangman payout",
    "highest_bot_chess_elo_defeated": "highest Elo defeated",
    "chess_pvp_wins": "most PvP chess wins",
    "total_artifacts": "most artifacts owned",
    "total_assets": "most properties owned",
    "highest_property_value": "highest property portfolio value",
    "command_streak": "longest daily streak",
    "scratchoff_day": "biggest scratchoff day payout",
    "crime": "biggest crime payout",
}


def _record_label(category: str) -> str:
    if category.startswith("hangman_wins_"):
        return "most hangman wins"
    return RECORD_LABELS.get(category, category.replace("_", " "))


def _resolve_channel_via(source_channel, target_id: int):
    """Best-effort cache lookup for a channel id, using *source_channel* as a
    handle into the bot's ConnectionState. Returns None if not found or if
    the lookup raises. Used to deliver records-channel embeds without
    threading the bot reference through every announce call site.
    """
    if target_id is None:
        return None
    try:
        client = source_channel._state._get_client()
        return client.get_channel(int(target_id))
    except Exception:
        return None


async def announce_record(channel, category: str, holder_name: str, value: int, *,
                          detail: str | None = None, holder_id: int | None = None,
                          notify: bool = False) -> None:
    """Send a record-broken announcement embed to `channel`. Best-effort; swallows errors.

    `detail` is an optional italicized second line for categories whose value
    alone is ambiguous — the shared `crime` record uses it to say which crime
    set the score (and, for a bank heist, who split the cut).

    Also logs the break to `notable_events` so !recap can mention records
    set today. This is the single hook point for every record category —
    callers in lottery/gambling/games all funnel through here.

    Records-channel routing (best-effort, never blocks the source-channel post):
    each guild may configure a single `records_channel`. That channel receives
    (a) every record set in its own guild, and (b) every new GLOBAL-top record
    from any guild (a value that beats every other guild's value for the
    category). Cross-guild global tops are tagged with the source guild name.

    Notification policy: when `holder_id` is given, the source-channel post
    carries a content mention of the holder so the achiever is highlighted.
    That send is still `silent=True` by default — nearly every record breaks
    off the holder's own action (a gamble, a purchase, a winning move), so
    they're already watching the channel and a push notification is noise.
    Pass `notify=True` only when the holder is NOT present for the trigger
    (e.g. the scheduled lottery draw) and the ping needs to be real. The
    records-channel copy and the cross-guild mirrors are always `silent=True`:
    they fire unprompted off other people's gambling, and a records channel
    mirroring every guild would be a notification firehose.
    """
    if channel is None:
        return
    label = _record_label(category)
    if category.startswith("hangman_wins_") or category == "chess_pvp_wins":
        suffix = f"**{value:,}** wins"
    elif category == "highest_bot_chess_elo_defeated":
        suffix = f"**{value:,} Elo**"
    elif category == "total_artifacts":
        suffix = f"**{value:,}** artifact{'' if value == 1 else 's'}"
    elif category == "total_assets":
        suffix = f"**{value:,}** propert{'y' if value == 1 else 'ies'}"
    elif category == "command_streak":
        suffix = f"**{value:,}** day{'' if value == 1 else 's'}"
    else:
        suffix = f"**{value:,} 🪙**"
    desc = f"**{holder_name}** just set a new {label} record: {suffix}"
    if detail:
        desc += f"\n*{detail}*"
    embed = emb("🏆 New Record!", desc, C_GOLD)
    try:
        if holder_id is not None:
            await channel.send(content=f"<@{holder_id}>", embed=embed, silent=not notify)
        else:
            await channel.send(embed=embed, silent=True)
    except Exception:
        pass

    guild = getattr(channel, "guild", None)
    source_channel_id = getattr(channel, "id", None)

    if guild is not None:
        from src.guild_config import get_guild_cfg
        from src.persistence import is_global_top

        # Is this also a new global top? If so it fans out to every guild's
        # records channel; otherwise only the source guild's channel sees it.
        try:
            is_top = await is_global_top(category, value, guild.id)
        except Exception:
            is_top = False

        # Source guild's own records channel — always gets its own records.
        own_chan_id = get_guild_cfg(guild.id).get("records_channel")
        if own_chan_id and int(own_chan_id) != source_channel_id:
            own_chan = _resolve_channel_via(channel, own_chan_id)
            if own_chan is not None:
                try:
                    await own_chan.send(embed=embed, silent=True)
                except Exception:
                    pass

        # Global top → also mirror into every OTHER guild's records channel.
        if is_top:
            global_embed = emb(
                "🌍 New Global Record!",
                f"{desc}\n*from {guild.name}*",
                C_GOLD,
            )
            for gid_str, cfg in list(state.guild_settings.items()):
                try:
                    gid = int(gid_str)
                except (TypeError, ValueError):
                    continue
                if gid == guild.id:
                    continue  # source guild already handled above
                other_chan_id = cfg.get("records_channel")
                if not other_chan_id or int(other_chan_id) == source_channel_id:
                    continue
                other_chan = _resolve_channel_via(channel, other_chan_id)
                if other_chan is not None:
                    try:
                        await other_chan.send(embed=global_embed, silent=True)
                    except Exception:
                        pass

    # Best-effort notable-events log — failures here must never break the
    # announcement path. Function-local imports dodge the helpers↔economy
    # import cycle.
    try:
        if guild is not None:
            from src.economy import _ct_today
            from src.persistence import log_notable_event
            await log_notable_event(
                guild.id, _ct_today(), "record", category, holder_name, value,
            )
    except Exception:
        pass


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
    seconds = int(time.monotonic() - state.bot_start_time)
    days, r = divmod(seconds, 86400)
    hours, r = divmod(r, 3600)
    minutes = r // 60
    return f"{days}d {hours}h {minutes}m"


_version_cache: "str | None" = None


def get_version() -> str:
    # The commit can't change for the lifetime of the process — don't shell
    # out to git twice on every call.
    global _version_cache
    if _version_cache is not None:
        return _version_cache
    _version_cache = _compute_version()
    return _version_cache


def _compute_version() -> str:
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
    from src.config import SYSTEM_PROMPT
    return state.channel_prompts.get(channel_id, SYSTEM_PROMPT)


def log_bot_permission_error(ctx: commands.Context, error_msg: str):
    state.audit_log.append({
        "time": time.time(),
        "user": f"{ctx.author.display_name} ({ctx.author.id})",
        "command": ctx.message.content[:100],
        "error": f"Bot Permission Error: {error_msg}",
    })


def _log_audit(user: str, command: str, error: str):
    state.audit_log.append({
        "time": time.time(),
        "user": user,
        "command": command,
        "error": error,
    })


async def send_ephemeral(ctx: commands.Context, *args, **kwargs) -> discord.Message:
    """Send a message with delete_after=EPHEMERAL_DELETE_AFTER and register it for cleanup on restart."""
    kwargs["delete_after"] = EPHEMERAL_DELETE_AFTER
    msg = await ctx.send(*args, **kwargs)
    await add_ephemeral_msg(msg.channel.id, msg.id)
    return msg


def parse_int_amount(value: str, allow_negative: bool = False) -> "int | None":
    """Parse a coin/ticket/bet amount string into an int.

    Accepts plain integers and a `k`/`m` suffix shorthand (case-insensitive)
    where `k` = thousand and `m` = million, including decimal multipliers:
    `1k` → 1000, `2.5k` → 2500, `100k` → 100000, `3m` → 3000000.
    Underscores and commas as digit separators are also accepted (`1_000`,
    `1,000`). Returns None if the string isn't a valid amount.

    By default negatives are rejected (returns None). Pass
    ``allow_negative=True`` for signed admin grants like `!give @user -5k`.
    """
    s = value.strip().lower().replace("_", "").replace(",", "")
    if not s:
        return None

    sign = 1
    if s[0] in ("+", "-"):
        if s[0] == "-":
            sign = -1
        s = s[1:]

    mult = 1
    if s and s[-1] in ("k", "m"):
        mult = 1000 if s[-1] == "k" else 1_000_000
        s = s[:-1]

    try:
        if mult != 1:
            amount = sign * int(float(s) * mult)
        else:
            # Reject decimals for plain integers (e.g. "2.5" is not a count).
            amount = sign * int(s)
    except (ValueError, OverflowError):
        # OverflowError: int(float("inf") * mult) from inputs like "infk" —
        # must be a usage error, not an uncaught-exception bug report.
        return None

    if amount < 0 and not allow_negative:
        return None
    return amount


_DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86_400,
    "w": 604_800,
    "mo": 2_592_000,    # 30 days
    "y": 31_536_000,    # 365 days
}


def parse_duration(value: str) -> "int | None":
    """Parse a duration string like ``1s``/``5m``/``2h``/``3d``/``1w``/``6mo``/``1y``
    into seconds. Returns None if the string isn't a valid duration.

    Units (case-insensitive): ``s`` seconds, ``m`` minutes, ``h`` hours,
    ``d`` days, ``w`` weeks, ``mo`` months (30d), ``y`` years (365d). Note the
    minute/month collision is resolved by spelling month as ``mo`` — a bare
    ``m`` is always minutes. A decimal multiplier is allowed (``1.5h``).
    """
    s = value.strip().lower()
    if not s:
        return None
    # Longest-suffix-first so "mo" wins over "m".
    for unit in ("mo", "s", "m", "h", "d", "w", "y"):
        if s.endswith(unit):
            num = s[: -len(unit)]
            if not num:
                return None
            try:
                qty = float(num)
                if qty <= 0:
                    return None
                return int(qty * _DURATION_UNITS[unit])
            except (ValueError, OverflowError):
                # OverflowError: "infh" and friends — invalid, not a crash.
                return None
    return None


def format_duration(seconds: "int | float") -> str:
    """Render a duration in seconds as a compact human string, e.g.
    ``90`` → ``1m 30s``, ``93784`` → ``1d 2h 3m``. Used by !effects to show
    remaining time. Shows at most the two largest non-zero units."""
    seconds = int(seconds)
    if seconds <= 0:
        return "0s"
    units = [("d", 86_400), ("h", 3600), ("m", 60), ("s", 1)]
    parts = []
    for label, size in units:
        if seconds >= size:
            qty, seconds = divmod(seconds, size)
            parts.append(f"{qty}{label}")
    return " ".join(parts[:2])


def _effect_expired(data: dict) -> bool:
    """True if a shop-effect dict has a set `expires_at` that's in the past.

    An effect with no `expires_at` (None or missing) is permanent and never
    expires by time. Counter-based effects (mock/curse/ragebait) leave
    expires_at unset and are expired by their `remaining` counter instead.
    """
    import time as _t
    exp = data.get("expires_at")
    return exp is not None and exp <= _t.time()


async def parse_amount(
    ctx: commands.Context, value: str, min_val: int = 1,
    error_msg: str = "Please provide a positive whole number amount."
) -> "int | None":
    """Parse a string as a positive integer >= min_val.

    Accepts plain integers, a `k`/`m` suffix shorthand (`2.5k` → 2500), or
    percentage strings like '50%', which resolve to that percentage of the
    caller's current balance.
    """
    from src.economy import get_balance

    resolved = value.strip()

    if resolved.endswith("%"):
        try:
            pct = float(resolved[:-1])
            if not (0 < pct <= 100):
                raise ValueError
            balance = await get_balance(ctx.author.id)
            amount = max(0, int(balance * pct / 100))
        except ValueError:
            await ctx.send("Percentage must be between 1% and 100%.")
            return None
    else:
        amount = parse_int_amount(resolved)
        if amount is None:
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
    """Accepts a mention, user ID, or case-insensitive display name / username substring.

    Purely numeric tokens only ever resolve as exact IDs (via the built-in
    converter) — they are never substring-matched against names, so a stray
    number like `!pay 42 100` can't silently target someone with "42" in
    their name. Mirrors `GlobalUser`'s rule."""

    async def convert(self, ctx: commands.Context, argument: str) -> discord.Member:
        # Try built-in converter first (handles mentions and exact IDs)
        try:
            return await commands.MemberConverter().convert(ctx, argument)
        except commands.BadArgument:
            pass

        if ctx.guild is None or argument.isdigit():
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


class OptionalMember(commands.Converter):
    """Like `MemberConverter`, but never raises `BadArgument` back to the command.

    - **Not found** → returns `None`, so the command sees the same value as a
      missing argument and can fall through to its own usage message. This
      avoids `BadArgument: Member '1' not found.` for inputs like `!pay 1`.
    - **Ambiguous** (substring matched several members) → sends its own
      disambiguation embed to the channel and returns `None`, so individual
      commands don't each have to catch and handle that case.

    Use this for command-signature annotations that already default to `None`.
    Code paths that need to distinguish not-found from a real member (e.g.
    `shop_cog`'s "no @user → target self" logic) should keep using
    `MemberConverter` and catch `BadArgument` themselves.
    """

    async def convert(self, ctx: commands.Context, argument: str) -> "discord.Member | None":
        try:
            return await MemberConverter().convert(ctx, argument)
        except commands.BadArgument as exc:
            msg = str(exc)
            if "matched multiple members" in msg:
                try:
                    await ctx.send(embed=emb("❌ Ambiguous Member", msg, C_RED))
                except Exception:
                    pass
            return None


_USER_TOKEN_RE = re.compile(r"<@!?(\d{15,20})>|(\d{15,20})")


def _match_users_by_name(users, query: str) -> list:
    """Case-insensitive name match over *users*; exact matches trump substrings."""
    q = query.lower()
    subs, exact = [], []
    for u in users:
        disp = u.display_name.lower()
        name = u.name.lower()
        if q == disp or q == name:
            exact.append(u)
        elif q in disp or q in name:
            subs.append(u)
    return exact or subs


class GlobalUser(commands.Converter):
    """Resolves a user beyond the current guild: mention, raw ID, or name.

    Resolution order:
    1. Mention / ID token → current-guild member if present, else a global
       ``fetch_user`` — so a user in no shared-with-here guild still resolves.
    2. Name (exact, then substring) against current-guild members.
    3. Name (exact, then substring) against every user the bot can see
       across all its guilds (``bot.users``).

    Purely numeric tokens shorter than a real ID are **never** name-matched —
    a short number is a count/amount, not a name. Unlike `OptionalMember`,
    failure raises `BadArgument`, so annotate as ``Optional[GlobalUser]``:
    discord.py then rewinds the failed token onto the next parameter
    (``!rig flip 5`` rigs 5 flips for yourself instead of eating the 5).
    Ambiguous names send their own embed before raising, like `OptionalMember`.
    """

    async def convert(self, ctx: commands.Context, argument: str) -> "discord.Member | discord.User":
        arg = argument.strip()

        m = _USER_TOKEN_RE.fullmatch(arg)
        if m:
            uid = int(m.group(1) or m.group(2))
            if ctx.guild:
                member = await fetch_member(ctx.guild, uid)
                if member:
                    return member
            user = ctx.bot.get_user(uid)
            if user:
                return user
            try:
                return await ctx.bot.fetch_user(uid)
            except discord.HTTPException:
                raise commands.BadArgument(f"No user with ID `{uid}`.")

        if arg.isdigit():
            raise commands.BadArgument(f"User '{arg}' not found.")

        pools = [ctx.guild.members] if ctx.guild else []
        pools.append(list(ctx.bot.users))
        for pool in pools:
            matches = _match_users_by_name(pool, arg)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                names = ", ".join(u.display_name for u in matches[:5])
                msg = f"'{arg}' matched multiple users: {names}"
                try:
                    await ctx.send(embed=emb("❌ Ambiguous User", msg, C_RED))
                except Exception:
                    pass
                raise commands.BadArgument(msg)
        raise commands.BadArgument(f"User '{arg}' not found.")


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
    from src.economy import deduct_balance, get_balance
    if uid in state.godmode_users or cost == 0:
        return True
    if not await deduct_balance(uid, cost):
        label_str = f"This costs **{cost_label or f'{cost:,}'} 🪙**. "
        # `ctx` may be a bare channel (dailies reaction claims) whose send is
        # loud by default — the payer just acted, so keep this quiet.
        await ctx.send(embed=emb(
            "💸 Insufficient Funds",
            f"{label_str}Balance: {await get_balance(uid):,} 🪙",
            C_RED,
        ), silent=True)
        return False
    return True


async def shop_payout(
    uid: int, amount: int, *, guild_id: int = None, holder_name: str = None
) -> bool:
    """Credit a gambling win/refund. The mirror image of `shop_charge`.

    `shop_charge` lets a godmode user play without paying. Crediting the win
    anyway made godmode an unbounded money printer into the shared economy —
    balances have no guild dimension, so `!flip 1m 100000` under godmode costs
    nothing, pays out billions, and those coins spend in every server. Free
    play means no money in *and* no money out, so this is a no-op for godmode
    users (refunds and pushes included: they never paid the bet).

    Returns True if the credit set a new highest_balance record.
    """
    from src.economy import add_balance
    if uid in state.godmode_users or amount <= 0:
        return False
    return await add_balance(uid, amount, guild_id=guild_id, holder_name=holder_name)


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
    channel: discord.abc.Messageable, game: dict, embed: discord.Embed,
    *, file: discord.File | None = None,
):
    """Edit the persistent board message in-place; falls back to a new send if deleted.

    When `file` is given, it replaces the message's attachments — caller must
    have set embed.set_image(url=f"attachment://{file.filename}") for it to show.
    """
    edit_kwargs: dict = {"embed": embed}
    if file is not None:
        edit_kwargs["attachments"] = [file]
    try:
        msg = await channel.fetch_message(game["board_msg_id"])
        await msg.edit(**edit_kwargs)
    except (discord.NotFound, discord.HTTPException):
        # Silent: the normal edit path notifies nobody, and any player
        # mentions here live inside the embed, which never notifies anyway.
        send_kwargs: dict = {"embed": embed, "silent": True}
        if file is not None:
            send_kwargs["file"] = file
        await channel.send(**send_kwargs)
