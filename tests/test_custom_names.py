"""Alias-creating commands refuse a word that `!<word>` already answers to
(src/custom_names.py): a real command or alias, another alias family, or a
counter in the same guild."""
import discord
import pytest
from discord.ext import commands

import src.state as _state
from src.cogs.counter_cog import CounterCog
from src.cogs.settings_cog import SettingsCog
from src.guild_config import get_guild_cfg

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember

pytestmark = pytest.mark.asyncio

ADDERS = {
    "nsfw_aliases": ("settings_nsfw_alias", ("add", "{w}")),
    "story_aliases": ("settings_story_alias", ("add", "{w}", "a", "prompt")),
    "tax_aliases": ("settings_tax_aliases", ("add", "{w}")),
}


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    # settings_cog binds save_guild_settings at import, past conftest's stubs.
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr("src.cogs.settings_cog.save_guild_settings", _noop)


@pytest.fixture
async def cog():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())

    @bot.command(name="ping", aliases=["pong"])
    async def _ping(ctx):
        pass

    @bot.group(name="shop")
    async def _shop(ctx):
        pass

    @_shop.command(name="tax")
    async def _shop_tax(ctx):
        pass

    return SettingsCog(bot)


def _ctx() -> FakeCtx:
    return FakeCtx(author=FakeMember(1, administrator=True), guild=FakeGuild(gid=1))


async def _add(cog, family: str, word: str) -> FakeCtx:
    method, args = ADDERS[family]
    ctx = _ctx()
    await getattr(cog, method).callback(cog, ctx, *(a.format(w=word) for a in args))
    return ctx


@pytest.mark.parametrize("family", ADDERS)
@pytest.mark.parametrize("word", ["ping", "pong"])
async def test_alias_refuses_a_real_command_or_its_alias(cog, family, word):
    ctx = await _add(cog, family, word)
    assert ctx.sent_embeds[-1].title == "❌ Name Taken"
    assert word not in get_guild_cfg(1).get(family, {})


@pytest.mark.parametrize("family", ADDERS)
async def test_alias_refuses_another_familys_word_and_a_counter(cog, family):
    other = next(f for f in ADDERS if f != family)
    await _add(cog, other, "taken")
    _state.counters[1] = {"tally": {}}

    for word in ("taken", "tally"):
        ctx = await _add(cog, family, word)
        assert ctx.sent_embeds[-1].title == "❌ Name Taken"
    assert get_guild_cfg(1).get(family, {}) == {}


@pytest.mark.parametrize("family", ADDERS)
async def test_free_word_is_added_and_other_guilds_dont_count(cog, family):
    get_guild_cfg(2)["nsfw_aliases"] = {"fresh": {"tags": ""}}
    _state.counters[2] = {"fresh": {}}
    await _add(cog, family, "fresh")
    assert "fresh" in get_guild_cfg(1)[family]


async def test_tax_alias_refuses_a_shop_subcommand(cog):
    ctx = await _add(cog, "tax_aliases", "tax")
    assert "shop tax" in ctx.sent_embeds[-1].description
    assert "tax" not in get_guild_cfg(1).get("tax_aliases", {})


async def test_story_alias_can_still_be_updated(cog):
    await _add(cog, "story_aliases", "scifi")
    ctx = _ctx()
    await cog.settings_story_alias.callback(cog, ctx, "add", "scifi", "new", "prompt")
    assert get_guild_cfg(1)["story_aliases"]["scifi"] == "new prompt"


async def test_counter_refuses_an_alias_word(cog):
    await _add(cog, "story_aliases", "scifi")
    counter_cog = CounterCog(cog.bot)
    ctx = _ctx()
    await counter_cog.cmd_counter_add.callback(counter_cog, ctx, "scifi", description="d")
    assert "story alias" in ctx.sent_embeds[-1].description
    assert _state.counters.get(1, {}) == {}
