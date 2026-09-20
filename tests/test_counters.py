"""!count / !counter: per-guild counters, the write allow-list, the two-step
add prompt, and the !<counter> shortcut (src/cogs/counter_cog.py)."""
import discord
import pytest
from discord.ext import commands

import src.state as _state
import src.persistence as _persistence
import src.cogs.counter_cog as _counter_cog
from src.cogs.counter_cog import CounterCog
from src.permissions import PermissionDenied, check_command_permission

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember

pytestmark = pytest.mark.asyncio

ADMIN, TRUSTED, PLAIN, TARGET = 1, 2, 3, 4


def _guild(gid: int = 1) -> FakeGuild:
    guild = FakeGuild(gid=gid)
    guild.members = [
        FakeMember(ADMIN, "boss", administrator=True),
        FakeMember(TRUSTED, "trusty"),
        FakeMember(PLAIN, "rando"),
        FakeMember(TARGET, "sleepy"),
    ]
    return guild


def _ctx(guild: FakeGuild, uid: int = ADMIN, content: str = "") -> FakeCtx:
    ctx = FakeCtx(author=guild.get_member(uid), guild=guild, command_name="count")
    ctx.message.content = content
    return ctx


def _make(gid: int, name: str, *, kind: str = "number", user_required: bool = False) -> dict:
    counter = {
        "description": f"{name} things", "user_required": user_required, "kind": kind,
        "created_by": ADMIN, "created_at": 0, "values": {},
    }
    _state.counters.setdefault(gid, {})[name] = counter
    return counter


@pytest.fixture(autouse=True)
def _builtin_member_converter(monkeypatch):
    """discord.py's converter needs a real Guild; resolve mentions and ids
    here and let the project MemberConverter's name matching run for real."""
    async def _convert(self, ctx, argument):
        digits = argument.strip("<@!>")
        member = ctx.guild.get_member(int(digits)) if digits.isdigit() else None
        if member is None:
            raise commands.BadArgument(f"Member '{argument}' not found.")
        return member
    monkeypatch.setattr(commands.MemberConverter, "convert", _convert)


@pytest.fixture
def cog():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    return CounterCog(bot)


def _choices(monkeypatch, *picks):
    """confirm_choice stub that returns `picks` in order and records each prompt."""
    seen, queue = [], list(picks)

    async def _pick(ctx, *, title, description, **kwargs):
        seen.append((title, description))
        return queue.pop(0)
    monkeypatch.setattr(_counter_cog, "confirm_choice", _pick)
    return seen


# ── !counter add / remove ────────────────────────────────────────────────────

async def test_add_previews_then_creates(cog, monkeypatch):
    guild = _guild()
    ctx = _ctx(guild)
    seen = _choices(monkeypatch, "required", "time")

    await cog.cmd_counter_add.callback(cog, ctx, "AFK", description="Times gone afk")

    assert len(seen) == 2
    assert "`afk`" in seen[0][1] and "Times gone afk" in seen[0][1]
    counter = _state.counters[1]["afk"]
    assert counter["user_required"] is True and counter["kind"] == "time"
    assert "!afk" in ctx.sent_embeds[-1].description


async def test_add_cancel_at_either_prompt_creates_nothing(cog, monkeypatch):
    guild = _guild()
    for picks in (("optional", None), (None,)):
        _choices(monkeypatch, *picks)
        await cog.cmd_counter_add.callback(cog, _ctx(guild), "afk", description="d")
    assert _state.counters == {}


@pytest.mark.parametrize("ref", ["add", "remove", "perms", "has space", "bad!", "x" * 33])
async def test_add_rejects_reserved_and_invalid_names(cog, monkeypatch, ref):
    seen = _choices(monkeypatch)
    ctx = _ctx(_guild())
    await cog.cmd_counter_add.callback(cog, ctx, ref, description="d")
    assert seen == [] and _state.counters == {}
    assert ctx.sent_embeds[-1].title.startswith("❌")


async def test_add_loses_to_a_counter_created_during_the_prompts(cog, monkeypatch):
    guild = _guild()

    async def _pick(ctx, **kwargs):
        _make(1, "afk")["description"] = "theirs"
        return kwargs["choices"][0]["value"]
    monkeypatch.setattr(_counter_cog, "confirm_choice", _pick)

    await cog.cmd_counter_add.callback(cog, _ctx(guild), "afk", description="mine")
    assert _state.counters[1]["afk"]["description"] == "theirs"


async def test_add_warns_when_the_name_is_a_real_command(cog, monkeypatch):
    await cog.bot.add_cog(cog)
    _choices(monkeypatch, "optional", "number")
    ctx = _ctx(_guild())
    await cog.cmd_counter_add.callback(cog, ctx, "count", description="d")
    assert "already a command" in ctx.sent_embeds[-1].description


async def test_remove_confirms_and_deletes(cog, monkeypatch):
    guild = _guild()
    _make(1, "afk")["values"][TARGET] = 3

    async def _no(*a, **k):
        return False
    monkeypatch.setattr(_counter_cog, "confirm_prompt", _no)
    await cog.cmd_counter_remove.callback(cog, _ctx(guild), "afk")
    assert "afk" in _state.counters[1]

    async def _yes(*a, **k):
        return True
    monkeypatch.setattr(_counter_cog, "confirm_prompt", _yes)
    await cog.cmd_counter_remove.callback(cog, _ctx(guild), "afk")
    assert "afk" not in _state.counters[1]


# ── !count: writes, views, titles ────────────────────────────────────────────

async def test_user_write_view_and_title(cog):
    guild = _guild()
    _make(1, "afk")
    await cog._count(_ctx(guild), "afk", f"<@{TARGET}> 1")
    await cog._count(_ctx(guild), "afk", "sleepy 2")
    assert _state.counters[1]["afk"]["values"] == {TARGET: 3}

    ctx = _ctx(guild, PLAIN)
    await cog._count(ctx, "afk", "sleepy")
    assert ctx.sent_embeds[-1].title == "sleepy Afk Count"
    assert "**3**" in ctx.sent_embeds[-1].description


async def test_total_includes_user_and_unattributed_values(cog):
    guild = _guild()
    _make(1, "afk")
    await cog._count(_ctx(guild), "afk", "5")
    await cog._count(_ctx(guild), "afk", "sleepy 2")

    ctx = _ctx(guild, PLAIN)
    await cog._count(ctx, "afk", "")
    assert ctx.sent_embeds[-1].title == "Afk Count"
    assert "**7**" in ctx.sent_embeds[-1].description


async def test_negative_clamps_at_zero(cog):
    guild = _guild()
    _make(1, "afk")["values"][TARGET] = 2
    await cog._count(_ctx(guild), "afk", "sleepy -5")
    assert _state.counters[1]["afk"]["values"][TARGET] == 0


async def test_time_counter_parses_and_formats_durations(cog):
    guild = _guild()
    _make(1, "nap", kind="time")
    await cog._count(_ctx(guild), "nap", "sleepy 1h")
    await cog._count(_ctx(guild), "nap", "sleepy 30m")
    assert _state.counters[1]["nap"]["values"][TARGET] == 5400

    ctx = _ctx(guild)
    await cog._count(ctx, "nap", "sleepy")
    assert ctx.sent_embeds[-1].title == "sleepy Nap Time"
    assert "1h 30m" in ctx.sent_embeds[-1].description

    # A bare number isn't a duration, so it reads as a (missing) user.
    await cog._count(ctx, "nap", "5")
    assert _state.counters[1]["nap"]["values"] == {TARGET: 5400}


async def test_lone_snowflake_is_a_user_not_an_amount(cog):
    guild = _guild()
    big = FakeMember(123456789012345678, "biggie")
    guild.members.append(big)
    _make(1, "afk")
    ctx = _ctx(guild)
    await cog._count(ctx, "afk", str(big.id))
    assert _state.counters[1]["afk"]["values"] == {}
    assert ctx.sent_embeds[-1].title == "biggie Afk Count"


async def test_required_user_refuses_bare_write_and_shows_leaderboard(cog):
    guild = _guild()
    counter = _make(1, "afk", user_required=True)
    ctx = _ctx(guild)
    await cog._count(ctx, "afk", "1")
    assert counter["values"] == {} and ctx.sent_embeds[-1].title.startswith("❌")

    counter["values"].update({TARGET: 2, PLAIN: 9})
    await cog._count(ctx, "afk", "")
    body = ctx.sent_embeds[-1].description
    assert "Top" in ctx.sent_embeds[-1].title
    assert body.index(f"<@{PLAIN}>") < body.index(f"<@{TARGET}>")


async def test_unknown_counter_and_bare_list(cog):
    guild = _guild()
    _make(1, "afk")
    ctx = _ctx(guild, PLAIN)
    await cog._count(ctx, "nope", "")
    assert ctx.sent_embeds[-1].title == "❌ Unknown Counter"
    await cog._count(ctx, None, "")
    assert "`afk`" in ctx.sent_embeds[-1].description


# ── write allow-list ─────────────────────────────────────────────────────────

async def test_plain_user_can_view_but_not_write(cog):
    guild = _guild()
    _make(1, "afk")
    ctx = _ctx(guild, PLAIN)
    await cog._count(ctx, "afk", "sleepy 1")
    assert ctx.sent_embeds[-1].title == "❌ No Permission"
    assert _state.counters[1]["afk"]["values"] == {}


async def test_addperm_grants_and_removeperm_revokes(cog):
    guild = _guild()
    _make(1, "afk")
    trusted = guild.get_member(TRUSTED)

    await cog.cmd_counter_addperm.callback(cog, _ctx(guild), member=trusted)
    await cog._count(_ctx(guild, TRUSTED), "afk", "sleepy 1")
    assert _state.counters[1]["afk"]["values"] == {TARGET: 1}

    await cog.cmd_counter_removeperm.callback(cog, _ctx(guild), member=trusted)
    await cog._count(_ctx(guild, TRUSTED), "afk", "sleepy 1")
    assert _state.counters[1]["afk"]["values"] == {TARGET: 1}


async def test_bot_admin_writes_without_a_grant(cog):
    guild = _guild()
    _make(1, "afk")
    _state.bot_admins.add(PLAIN)
    await cog._count(_ctx(guild, PLAIN), "afk", "1")
    assert _state.counters[1]["afk"]["values"] == {0: 1}


async def test_addperm_refuses_bots(cog):
    guild = _guild()
    robot = FakeMember(50, "robot")
    robot.bot = True
    await cog.cmd_counter_addperm.callback(cog, _ctx(guild), member=robot)
    assert _state.counter_perms.get(1, set()) == set()


async def test_counter_group_is_admin_only_in_command_perms(cog):
    import json
    from pathlib import Path
    perms = json.loads(Path("src/command_perms.json").read_text(encoding="utf-8"))
    assert perms["counter"]["tier"] == "server_admin"
    assert perms["count"]["tier"] == "everyone"


# ── per-guild isolation ──────────────────────────────────────────────────────

async def test_same_name_in_two_guilds_shares_nothing(cog, monkeypatch):
    a, b = _guild(1), _guild(2)
    _make(1, "afk")
    _make(2, "afk", kind="time")
    _state.counter_perms[1] = {TRUSTED}

    await cog._count(_ctx(a, TRUSTED), "afk", "sleepy 4")
    assert _state.counters[2]["afk"]["values"] == {}

    # The grant is guild A's: the same user is a nobody in B.
    ctx_b = _ctx(b, TRUSTED)
    await cog._count(ctx_b, "afk", "sleepy 1m")
    assert ctx_b.sent_embeds[-1].title == "❌ No Permission"

    async def _yes(*args, **kwargs):
        return True
    monkeypatch.setattr(_counter_cog, "confirm_prompt", _yes)
    await cog.cmd_counter_remove.callback(cog, _ctx(b), "afk")
    assert _state.counters[1]["afk"]["values"] == {TARGET: 4}
    assert "afk" not in _state.counters[2]


# ── !<counter> shortcut ──────────────────────────────────────────────────────

def _not_found(ctx, word: str):
    ctx.invoked_with = word
    ctx.command = None
    return commands.CommandNotFound(f'Command "{word}" is not found')


async def test_shortcut_routes_to_count(cog):
    guild = _guild()
    _make(1, "afk")
    ctx = _ctx(guild, content=f"!afk <@{TARGET}> 1")
    await cog.on_command_error(ctx, _not_found(ctx, "afk"))
    assert _state.counters[1]["afk"]["values"] == {TARGET: 1}
    assert ctx.command is cog.cmd_count


async def test_shortcut_ignores_other_guilds_counters_and_other_errors(cog):
    _make(2, "afk")
    ctx = _ctx(_guild(1), content="!afk")
    await cog.on_command_error(ctx, _not_found(ctx, "afk"))
    await cog.on_command_error(ctx, commands.BadArgument("x"))
    assert ctx.sent_embeds == []


async def test_shortcut_defers_to_an_nsfw_alias(cog):
    from src.guild_config import get_guild_cfg
    _make(1, "afk")
    get_guild_cfg(1)["nsfw_aliases"] = {"afk": {"tags": ""}}
    ctx = _ctx(_guild(), content="!afk")
    await cog.on_command_error(ctx, _not_found(ctx, "afk"))
    assert ctx.sent_embeds == []


async def test_shortcut_runs_the_global_checks(cog, monkeypatch):
    """The shortcut never went through bot.invoke, so it must run the bot's
    checks itself — here the command_perms gate, with `count` locked down."""
    guild = _guild()
    _make(1, "afk")
    _state.command_perms["count"] = {"tier": "bot_admin", "hidden": True}

    @cog.bot.check
    async def _gate(ctx):
        if not await check_command_permission(ctx):
            raise PermissionDenied()
        return True

    dispatched = []
    monkeypatch.setattr(cog.bot, "dispatch", lambda *args: dispatched.append(args))

    ctx = _ctx(guild, content="!afk 1")
    await cog.on_command_error(ctx, _not_found(ctx, "afk"))
    assert _state.counters[1]["afk"]["values"] == {}
    assert dispatched and isinstance(dispatched[0][2], PermissionDenied)


# ── persistence ──────────────────────────────────────────────────────────────

async def test_round_trip_through_the_db(cog, db, monkeypatch):
    guild = _guild()
    _choices(monkeypatch, "optional", "time")
    await cog.cmd_counter_add.callback(cog, _ctx(guild), "nap", description="Naps taken")
    await cog._count(_ctx(guild), "nap", "sleepy 2h")
    await cog._count(_ctx(guild), "nap", "10m")
    await cog.cmd_counter_addperm.callback(cog, _ctx(guild), member=guild.get_member(TRUSTED))

    _state.counters.clear()
    _state.counter_perms.clear()
    await _persistence.init_db_state()

    counter = _state.counters[1]["nap"]
    assert counter["kind"] == "time" and counter["description"] == "Naps taken"
    assert counter["values"] == {TARGET: 7200, 0: 600}
    assert _state.counter_perms == {1: {TRUSTED}}

    async def _yes(*args, **kwargs):
        return True
    monkeypatch.setattr(_counter_cog, "confirm_prompt", _yes)
    await cog.cmd_counter_remove.callback(cog, _ctx(guild), "nap")
    _state.counters.clear()
    await _persistence.init_db_state()
    assert _state.counters == {}
