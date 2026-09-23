"""The command reference the AI carries (src/command_reference.py), the
prompts that carry it (src/ai.py), and the DM rule in src/events.py: a DM
that isn't a command gets no reply."""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord.ext import commands

from src import ai as _ai
from src import state
from src.command_reference import RULES, build_command_reference, guild_reference
from src.features import set_feature
from src.events import EventsCog
from tests.fakes.discord import FakeMember, FakeMessage, FakeThread

GID = 4242


def _bot():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())

    @bot.command(name="pay", aliases=["give"], help="Send coins to another user", usage="@user <amount>")
    async def pay(ctx, *, args: str = None):
        pass

    @bot.command(name="slots", help="Spin the slots")
    async def slots(ctx, amount: int = None):
        pass

    @bot.command(name="ban", help="Silence a user in this server")
    async def ban(ctx, user: str):
        pass

    @bot.command(name="godmode", help="Never lose")
    async def godmode(ctx):
        pass

    @bot.command(name="rig", hidden=True, help="Rig a game")
    async def rig(ctx):
        pass

    @bot.group(name="idle", invoke_without_command=True, help="Your idle character")
    async def idle(ctx):
        pass

    @idle.command(name="map", aliases=["chart"], help="Draw the map")
    async def idle_map(ctx):
        pass

    # The bare form the idle cog registers for each subcommand: same
    # callback, so the reference folds it into `!idle map` as an alias.
    bot.add_command(commands.Command(idle_map.callback, name="map", help=idle_map.help))
    return bot


def _perms():
    state.command_perms.update({
        "ban": {"tier": "server_admin", "hidden": False},
        "godmode": {"tier": "global_admin", "hidden": True},
    })


def _lines(text: str) -> dict[str, str]:
    """Each line keyed by its command name (`!idle map`), which is the head
    up to the first argument, alias list or dash."""
    body = text[len(RULES):]
    return {re.match(r"^(!?\S+(?: [a-z][a-z-]*)*)", line).group(1): line for line in body.splitlines()}


async def test_reference_lists_each_visible_command_once_with_usage_aliases_and_help():
    _perms()
    lines = _lines(build_command_reference(_bot()))
    assert lines["!pay"] == "!pay @user <amount> (also !give) — Send coins to another user"
    assert lines["!slots"] == "!slots [amount] — Spin the slots"
    assert lines["!idle"] == "!idle — Your idle character"
    assert "!map" not in lines, "the bare alias is folded into its subcommand's line"
    assert lines["!idle map"] == "!idle map (also !map, !idle chart) — Draw the map"


async def test_reference_leaves_hidden_commands_out_and_marks_admin_tiers():
    _perms()
    text = build_command_reference(_bot())
    assert "rig" not in text and "godmode" not in text
    assert _lines(text)["!ban"] == "!ban <user> — Silence a user in this server [server admins only]"


async def test_reference_marks_what_the_server_switched_off():
    _perms()
    set_feature(GID, "gambling", False)
    text = build_command_reference(_bot(), GID)
    assert _lines(text)["!slots"].endswith("[switched off in this server]")
    assert text.endswith("\nSwitched off in this server by its admins: 🎲 Gambling.")
    dm_lines = _lines(build_command_reference(_bot(), None))
    assert "[switched off" not in dm_lines["!slots"], "a DM has no guild to be switched off in"


async def test_reference_is_empty_without_a_bot_and_starts_with_the_rules():
    assert build_command_reference(None) == ""
    assert guild_reference(None)(GID) == ""
    text = build_command_reference(_bot())
    assert text.startswith(RULES)
    assert "Never invent" in RULES and "ONLY commands" in RULES


# ── the prompts that carry it ────────────────────────────────────────────────

async def _captured_system_prompt(monkeypatch, channel, kind: str | None, guild_id):
    captured = {}

    async def _stream(channel, reply_to, messages, history, **kwargs):
        captured["system"] = messages[0]["content"]
    monkeypatch.setattr(_ai, "_execute_ollama_stream", _stream)
    monkeypatch.setattr(_ai, "save_ai_threads", AsyncMock())
    if kind is not None:
        state.ai_threads[channel.id] = {
            "kind": kind, "owner_id": 1, "guild_id": guild_id, "invited_ids": {1},
            "system_prompt": "persona", "character_prompt": None, "history": [],
        }
    await _ai.respond(channel, 1, "hi", FakeMessage(), guild_id=guild_id)
    return captured["system"]


async def test_mention_chat_and_ask_threads_get_the_reference_but_fiction_does_not(monkeypatch):
    monkeypatch.setattr(_ai, "COMMAND_REFERENCE_PROVIDER", lambda gid: f"REF for {gid}")
    channel = SimpleNamespace(id=10)
    assert (await _captured_system_prompt(monkeypatch, channel, None, GID)).endswith("\n\nREF for 4242")
    thread = FakeThread(thread_id=11)
    thread.send = AsyncMock(return_value=FakeMessage())
    assert (await _captured_system_prompt(monkeypatch, thread, "ask", GID)) == "persona\n\nREF for 4242"
    for fiction in ("story", "roleplay", "rpg"):
        thread = FakeThread(thread_id=12)
        thread.send = AsyncMock(return_value=FakeMessage())
        assert (await _captured_system_prompt(monkeypatch, thread, fiction, GID)) == "persona"


async def test_prompts_go_out_unchanged_without_a_provider(monkeypatch):
    monkeypatch.setattr(_ai, "COMMAND_REFERENCE_PROVIDER", None)
    channel = SimpleNamespace(id=10)
    system = await _captured_system_prompt(monkeypatch, channel, None, GID)
    assert "Your commands" not in system


async def test_the_cog_hands_the_ai_module_a_provider_over_its_bot():
    from src.cogs.ai_cog import AICog
    bot = _bot()
    _perms()
    AICog(bot=bot)
    assert "!pay @user <amount>" in _ai.COMMAND_REFERENCE_PROVIDER(None)


# ── DMs ──────────────────────────────────────────────────────────────────────

def _dm_channel(cid: int = 77):
    channel = object.__new__(discord.DMChannel)
    channel.id = cid
    return channel


def _events_cog():
    bot_user = FakeMember(999)
    return SimpleNamespace(bot=SimpleNamespace(user=bot_user, process_commands=AsyncMock(), get_context=AsyncMock(), invoke=AsyncMock()))


async def test_a_plain_dm_gets_no_ai_reply(monkeypatch):
    respond = AsyncMock()
    monkeypatch.setattr("src.events.respond", respond)
    cog = _events_cog()
    message = FakeMessage(content="hello there", author=FakeMember(5), channel=_dm_channel())
    message.guild = None
    await EventsCog._handle_ai_routing(cog, message)
    respond.assert_not_awaited()
    message.reply.assert_not_awaited()
    cog.bot.process_commands.assert_awaited_once_with(message)


async def test_a_command_dm_still_reaches_the_dispatcher(monkeypatch):
    respond = AsyncMock()
    monkeypatch.setattr("src.events.respond", respond)
    cog = _events_cog()
    message = FakeMessage(content="!balance", author=FakeMember(5), channel=_dm_channel())
    message.guild = None
    await EventsCog._handle_ai_routing(cog, message)
    respond.assert_not_awaited()
    cog.bot.process_commands.assert_awaited_once_with(message)


async def test_a_mention_in_a_server_is_still_answered(monkeypatch):
    respond = AsyncMock()
    monkeypatch.setattr("src.events.respond", respond)
    cog = _events_cog()
    message = FakeMessage(content=f"<@{cog.bot.user.id}> hi", author=FakeMember(5))
    message.guild = SimpleNamespace(id=GID, text_channels=[])
    message.mentions = [cog.bot.user]
    await EventsCog._handle_ai_routing(cog, message)
    respond.assert_awaited_once()
    assert respond.await_args.args[2] == "hi"


async def test_a_plain_dm_no_longer_triggers_the_auto_daily(monkeypatch):
    auto_daily = AsyncMock(return_value=(0, 0))
    monkeypatch.setattr("src.events._auto_daily", auto_daily)
    cog = _events_cog()
    plain = FakeMessage(content="hello", author=FakeMember(5), channel=_dm_channel())
    plain.guild = None
    await EventsCog._handle_auto_daily(cog, plain)
    auto_daily.assert_not_awaited()
    command = FakeMessage(content="!daily", author=FakeMember(5), channel=_dm_channel())
    command.guild = None
    await EventsCog._handle_auto_daily(cog, command)
    auto_daily.assert_awaited_once()
