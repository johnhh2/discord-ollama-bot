"""!count / !counter — per-guild tallies ("how many times has X gone afk").

Everything is keyed by guild id: two servers with an `afk` counter share
nothing but the name. A counter records either a number or a time (seconds);
each user has their own value, uid 0 holds writes made with no user, and the
counter's total is the sum of all of them.
"""
import re
import time

from discord.ext import commands

import src.persistence as persistence
from src import state
from src.confirm_view import confirm_choice, confirm_prompt
from src.custom_names import COUNTERS, custom_name_holder
from src.helpers import (
    emb, C_BLUE, C_GREEN, C_GREY, C_RED,
    MemberConverter, format_duration, parse_duration, parse_int_amount,
)
from src.permissions import can_manage_settings

# Subcommands of !counter, so `!counter <word>` is never ambiguous.
RESERVED_NAMES = frozenset({"add", "remove", "list", "addperm", "removeperm", "perms"})
_NAME_RE = re.compile(r"[a-z0-9_-]{1,32}")
_SNOWFLAKE_RE = re.compile(r"\d{15,20}")
MAX_DESCRIPTION = 200
MAX_VALUE = 2**62  # inside the signed BIGINT column
LEADERBOARD_SIZE = 10
UNATTRIBUTED = 0
NOT_YOURS = "Not your prompt."


def _guild_counters(guild_id: int) -> dict:
    return state.counters.get(guild_id, {})


def _label(name: str, counter: dict) -> str:
    noun = "Time" if counter["kind"] == "time" else "Count"
    return f"{name.replace('_', ' ').replace('-', ' ').title()} {noun}"


def _fmt(counter: dict, value: int) -> str:
    return format_duration(value) if counter["kind"] == "time" else f"{value:,}"


def _parse_delta(counter: dict, token: str) -> "int | None":
    """A signed amount in the counter's unit, or None if `token` isn't one."""
    sign = -1 if token.startswith("-") else 1
    body = token.lstrip("+-")
    parsed = parse_duration(body) if counter["kind"] == "time" else parse_int_amount(body)
    return None if parsed is None else sign * parsed


def can_write_counters(ctx: commands.Context) -> bool:
    """Admins implicitly, plus anyone `!counter addperm` trusted in this guild.
    Checked inline because `count` is one command whose view half is open and
    whose write half isn't — a command_perms.json tier can't split that."""
    return can_manage_settings(ctx) or ctx.author.id in state.counter_perms.get(ctx.guild.id, set())


class CounterCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── !count ───────────────────────────────────────────────────────────

    @commands.command(name="count")
    async def cmd_count(self, ctx: commands.Context, name: str = None, *, rest: str = ""):
        """!count <counter> [user] [n] — view a counter, or add n to it."""
        await self._count(ctx, name, rest)

    async def _count(self, ctx: commands.Context, name: "str | None", rest: str):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Counters", "Counters only work in a server.", C_RED))
            return
        if name is None:
            await self._send_list(ctx)
            return
        name = name.lower()
        counter = _guild_counters(ctx.guild.id).get(name)
        if counter is None:
            await ctx.send(embed=emb("❌ Unknown Counter", f"No counter named `{name}` here. See `!count`.", C_RED))
            return

        tokens = rest.split()
        delta = None
        # A lone snowflake is a user to look up, not a number to add.
        if tokens and not (len(tokens) == 1 and _SNOWFLAKE_RE.fullmatch(tokens[0])):
            delta = _parse_delta(counter, tokens[-1])
            if delta is not None:
                tokens = tokens[:-1]

        member = None
        if tokens:
            try:
                member = await MemberConverter().convert(ctx, " ".join(tokens))
            except commands.BadArgument as exc:
                await ctx.send(embed=emb("❌ Count", str(exc), C_RED))
                return

        if delta is None:
            await self._send_view(ctx, name, counter, member)
        else:
            await self._write(ctx, name, counter, member, delta)

    async def _write(self, ctx, name: str, counter: dict, member, delta: int):
        if not can_write_counters(ctx):
            await ctx.send(embed=emb(
                "❌ No Permission",
                "Only admins and users added with `!counter addperm` can change counters.",
                C_RED,
            ))
            return
        if delta == 0:
            await ctx.send(embed=emb("❌ Count", "The amount can't be zero.", C_RED))
            return
        if member is None and counter["user_required"]:
            await ctx.send(embed=emb("❌ Count", f"`{name}` needs a user: `!count {name} @user <n>`.", C_RED))
            return

        uid = member.id if member else UNATTRIBUTED
        values = counter["values"]
        new = min(max(values.get(uid, 0) + delta, 0), MAX_VALUE)
        values[uid] = new
        await persistence.save_counter_value(ctx.guild.id, name, uid, new)

        sign = "+" if delta > 0 else "-"
        change = f"{sign}{_fmt(counter, abs(delta))}"
        if member:
            title = f"{member.display_name} {_label(name, counter)}"
            body = f"{change} → **{_fmt(counter, new)}**"
        else:
            title = _label(name, counter)
            body = f"{change} → **{_fmt(counter, sum(values.values()))}**"
        await ctx.send(embed=emb(title, f"*{counter['description']}*\n\n{body}", C_GREEN))

    async def _send_view(self, ctx, name: str, counter: dict, member):
        values = counter["values"]
        if member:
            title = f"{member.display_name} {_label(name, counter)}"
            body = f"**{_fmt(counter, values.get(member.id, 0))}**"
        elif counter["user_required"]:
            title = f"{_label(name, counter)} — Top {LEADERBOARD_SIZE}"
            body = self._leaderboard(counter) or "Nothing counted yet."
        else:
            title = _label(name, counter)
            body = f"**{_fmt(counter, sum(values.values()))}**"
        await ctx.send(embed=emb(title, f"*{counter['description']}*\n\n{body}", C_BLUE))

    @staticmethod
    def _leaderboard(counter: dict) -> str:
        ranked = sorted(
            ((uid, v) for uid, v in counter["values"].items() if uid != UNATTRIBUTED and v > 0),
            key=lambda pair: (-pair[1], pair[0]),
        )[:LEADERBOARD_SIZE]
        return "\n".join(f"**{i}.** <@{uid}> — {_fmt(counter, v)}" for i, (uid, v) in enumerate(ranked, 1))

    async def _send_list(self, ctx):
        counters = _guild_counters(ctx.guild.id)
        if not counters:
            await ctx.send(embed=emb("🔢 Counters", "No counters yet. An admin can make one with `!counter add`.", C_GREY))
            return
        lines = [
            f"`{name}` — {c['description']} ({'time' if c['kind'] == 'time' else 'number'}"
            f"{', user required' if c['user_required'] else ''})"
            for name, c in sorted(counters.items())
        ]
        lines.append("\n`!count <counter> [@user]` to view · `!count <counter> [@user] <n>` to add")
        await ctx.send(embed=emb("🔢 Counters", "\n".join(lines), C_BLUE))

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Route !<counter> to !count. Only ever reached on CommandNotFound,
        so a real command or alias of the same name always wins."""
        if not isinstance(error, commands.CommandNotFound) or ctx.guild is None:
            return
        parts = ctx.message.content.strip().split(None, 1)
        word = (ctx.invoked_with or "").lower()
        if word not in _guild_counters(ctx.guild.id):
            return
        if custom_name_holder(ctx.guild.id, word, own=COUNTERS):
            return  # a collision from before add-time checks — that alias's listener answers
        # This path skipped bot.invoke, so nothing has run the global checks
        # (perm gate, level gate, channel lists, !session allowlist) yet.
        ctx.command = self.cmd_count
        try:
            if not await self.bot.can_run(ctx):
                return
        except commands.CommandError as exc:
            self.bot.dispatch("command_error", ctx, exc)
            return
        await self._count(ctx, word, parts[1] if len(parts) > 1 else "")

    # ── !counter ─────────────────────────────────────────────────────────

    @commands.group(name="counter", aliases=["counters"], invoke_without_command=True)
    async def cmd_counter(self, ctx: commands.Context):
        """!counter add|remove|addperm|removeperm|perms|list"""
        if ctx.guild is None:
            return
        await self._send_list(ctx)

    @cmd_counter.command(name="list")
    async def cmd_counter_list(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        await self._send_list(ctx)

    @cmd_counter.command(name="add")
    async def cmd_counter_add(self, ctx: commands.Context, ref: str = None, *, description: str = None):
        if ctx.guild is None:
            return
        if not ref or not description:
            await ctx.send(embed=emb("❌ Usage", "`!counter add <name> <description>`", C_RED))
            return
        name = ref.lower()
        gid = ctx.guild.id
        problem = None
        if not _NAME_RE.fullmatch(name):
            problem = "Names are 1–32 characters: letters, digits, `_` and `-`, no spaces."
        elif name in RESERVED_NAMES:
            problem = f"`{name}` is reserved for a `!counter` subcommand."
        elif name in _guild_counters(gid):
            problem = f"A counter named `{name}` already exists."
        elif holder := custom_name_holder(gid, name, own=COUNTERS):
            # Unlike a real command (warned about below), an alias's listener
            # would answer `!name` alongside ours.
            problem = f"`!{name}` is already {holder} here."
        elif len(description) > MAX_DESCRIPTION:
            problem = f"Keep the description under {MAX_DESCRIPTION} characters."
        if problem:
            await ctx.send(embed=emb("❌ Counter", problem, C_RED))
            return

        preview = f"**Name:** `{name}`\n**Description:** {description}"
        user_required = await confirm_choice(
            ctx,
            title=f"New Counter: {name}",
            description=f"{preview}\n\nDoes every entry need a user, or may `!count {name} <n>` count toward the total alone?",
            choices=[
                {"label": "User optional", "value": "optional", "default": True},
                {"label": "User required", "value": "required"},
            ],
            payer=ctx.author,
            not_yours=NOT_YOURS,
        )
        if user_required is None:
            return
        kind = await confirm_choice(
            ctx,
            title=f"New Counter: {name}",
            description=f"{preview}\n**User:** {user_required}\n\nRecord it as a number, or as a time (`1s`, `1m`, `1h`, `1d`)?",
            choices=[
                {"label": "Number", "value": "number", "default": True},
                {"label": "Time", "value": "time"},
            ],
            payer=ctx.author,
            not_yours=NOT_YOURS,
        )
        if kind is None:
            return

        # The prompts were long awaits — someone else may have taken the name.
        if name in _guild_counters(gid):
            await ctx.send(embed=emb("❌ Counter", f"A counter named `{name}` already exists.", C_RED))
            return
        state.counters.setdefault(gid, {})[name] = {
            "description": description,
            "user_required": user_required == "required",
            "kind": kind,
            "created_by": ctx.author.id,
            "created_at": int(time.time()),
            "values": {},
        }
        await persistence.save_counter(gid, name)

        shadowed = self.bot.get_command(name) is not None
        usage = (
            f"`!{name}` is already a command here, so use `!count {name}`."
            if shadowed else f"Use `!{name}` or `!count {name}`."
        )
        await ctx.send(embed=emb("✅ Counter Added", f"{preview}\n\n{usage}", C_GREEN))

    @cmd_counter.command(name="remove")
    async def cmd_counter_remove(self, ctx: commands.Context, ref: str = None):
        if ctx.guild is None:
            return
        name = (ref or "").lower()
        gid = ctx.guild.id
        counter = _guild_counters(gid).get(name)
        if counter is None:
            await ctx.send(embed=emb("❌ Counter", f"No counter named `{name}` here.", C_RED))
            return
        entries = sum(1 for v in counter["values"].values() if v)
        if not await confirm_prompt(
            ctx,
            title=f"Remove Counter: {name}",
            description=f"*{counter['description']}*\n\nThis deletes the counter and its {entries} recorded value(s) for good.",
            payer=ctx.author,
            not_yours=NOT_YOURS,
        ):
            return
        # Identity check: a remove + re-add during the prompt made a new counter.
        if _guild_counters(gid).get(name) is not counter:
            return
        del state.counters[gid][name]
        await persistence.delete_counter(gid, name)
        await ctx.send(embed=emb("🗑️ Counter Removed", f"`{name}` is gone.", C_GREY))

    @cmd_counter.command(name="addperm")
    async def cmd_counter_addperm(self, ctx: commands.Context, *, member: MemberConverter = None):
        if ctx.guild is None:
            return
        if member is None:
            await ctx.send(embed=emb("❌ Usage", "`!counter addperm @user`", C_RED))
            return
        if member.bot:
            await ctx.send(embed=emb("❌ Counter", "Bots can't be given counter access.", C_RED))
            return
        state.counter_perms.setdefault(ctx.guild.id, set()).add(member.id)
        await persistence.save_counter_perm(ctx.guild.id, member.id, ctx.author.id)
        await ctx.send(embed=emb("✅ Counter Access", f"{member.mention} can now change this server's counters.", C_GREEN))

    @cmd_counter.command(name="removeperm")
    async def cmd_counter_removeperm(self, ctx: commands.Context, *, member: MemberConverter = None):
        if ctx.guild is None:
            return
        if member is None:
            await ctx.send(embed=emb("❌ Usage", "`!counter removeperm @user`", C_RED))
            return
        state.counter_perms.get(ctx.guild.id, set()).discard(member.id)
        await persistence.delete_counter_perm(ctx.guild.id, member.id)
        await ctx.send(embed=emb("✅ Counter Access", f"{member.mention} can no longer change counters.", C_GREY))

    @cmd_counter.command(name="perms")
    async def cmd_counter_perms(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        uids = sorted(state.counter_perms.get(ctx.guild.id, set()))
        body = "\n".join(f"<@{uid}>" for uid in uids) or "Nobody yet — `!counter addperm @user`."
        await ctx.send(embed=emb("🔢 Counter Access", f"Admins, plus:\n{body}", C_BLUE))


async def setup(bot):
    await bot.add_cog(CounterCog(bot))
