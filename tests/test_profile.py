"""!profile — user-overview embed tests."""
import pytest

import src.state as _state
from src.cogs.profile_cog import ProfileCog
from src.economy import add_balance
from src.games import bot_chess_rewards as br

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember

pytestmark = pytest.mark.asyncio


class _StubBot:
    user = None


def _profile_desc(ctx: FakeCtx) -> str:
    assert ctx.sent_embeds, "profile sent no embed"
    return ctx.sent_embeds[-1].description


async def test_profile_shows_wallet_and_hides_chess_ranks_without_defeats(db):
    cog = ProfileCog(bot=_StubBot())
    member = FakeMember(uid=9100, display_name="Alice")
    ctx = FakeCtx(author=member, guild=FakeGuild(gid=42))
    await add_balance(9100, 12_345)

    await cog.cmd_profile.callback(cog, ctx, target=None)

    desc = _profile_desc(ctx)
    assert "12,345" in desc
    assert "Lottery tickets: **0**" in desc
    # No bot defeats → the whole chess section is omitted.
    assert "Chess Ranks" not in desc


async def test_profile_shows_chess_ranks(db):
    cog = ProfileCog(bot=_StubBot())
    member = FakeMember(uid=9101, display_name="Bob")
    ctx = FakeCtx(author=member, guild=FakeGuild(gid=42))
    await br.award_bot_defeat(user_id=9101, guild_id=42, holder_name="Bob", bot_elo=1500)
    await br.award_bot_defeat(user_id=9101, guild_id=42, holder_name="Bob", bot_elo=1200)

    await cog.cmd_profile.callback(cog, ctx, target=None)

    desc = _profile_desc(ctx)
    assert "Max Elo defeated: **1,500**" in desc
    assert "Total Elo defeated: **2,700**" in desc
    # The bonus-progress line was deliberately dropped from the profile.
    assert "First-defeat bonuses" not in desc


async def test_profile_level_line_shows_global_level_without_lvl_hint(db):
    """The level line drops the !lvl hint and adds the cross-guild global level."""
    cog = ProfileCog(bot=_StubBot())
    member = FakeMember(uid=9107, display_name="Eve")
    ctx = FakeCtx(author=member, guild=FakeGuild(gid=42))
    # 150 XP here (level 1 → display 2); 100 XP elsewhere → 250 global
    # (level 2 → display 3, thresholds at 100 / 202 / 309 XP).
    _state.leveling.setdefault("42", {})["9107"] = {"xp": 150, "level": 1}
    _state.leveling.setdefault("77", {})["9107"] = {"xp": 100, "level": 0}

    await cog.cmd_profile.callback(cog, ctx, target=None)

    desc = _profile_desc(ctx)
    assert "Level **2**" in desc
    assert "Global level **3**" in desc
    assert "!lvl" not in desc


async def test_profile_hides_global_level_when_equal_to_server_level(db):
    """XP in only one guild → global level matches, so the global part is hidden."""
    cog = ProfileCog(bot=_StubBot())
    member = FakeMember(uid=9109, display_name="Gil")
    ctx = FakeCtx(author=member, guild=FakeGuild(gid=42))
    _state.leveling.setdefault("42", {})["9109"] = {"xp": 150, "level": 1}

    await cog.cmd_profile.callback(cog, ctx, target=None)

    desc = _profile_desc(ctx)
    assert "Level **2**" in desc
    assert "Global level" not in desc


async def test_profile_counts_records_held_in_guild(db):
    from src.persistence import save_records

    cog = ProfileCog(bot=_StubBot())
    member = FakeMember(uid=9108, display_name="Fay")
    ctx = FakeCtx(author=member, guild=FakeGuild(gid=42))
    await save_records(42, {
        "slots": {"value": 500, "holder_id": 9108, "holder_name": "Fay"},
        "crime": {"value": 300, "holder_id": 9108, "holder_name": "Fay"},
        "flip": {"value": 900, "holder_id": 1234, "holder_name": "Someone"},
    })
    # A record in another guild doesn't count here.
    await save_records(77, {
        "lottery": {"value": 100, "holder_id": 9108, "holder_name": "Fay"},
    })

    await cog.cmd_profile.callback(cog, ctx, target=None)

    assert "Records held: **2**" in _profile_desc(ctx)


async def test_profile_targets_other_member(db):
    """!profile @other shows the target's numbers, not the invoker's."""
    cog = ProfileCog(bot=_StubBot())
    author = FakeMember(uid=9102, display_name="Caller")
    other = FakeMember(uid=9103, display_name="Other")
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=42))
    await add_balance(9103, 777)

    await cog.cmd_profile.callback(cog, ctx, target=other)

    embed = ctx.sent_embeds[-1]
    assert "Other" in embed.title
    assert "777" in embed.description


async def test_profile_bot_target_short_circuits(db):
    cog = ProfileCog(bot=_StubBot())
    author = FakeMember(uid=9104, display_name="Caller")
    robot = FakeMember(uid=9105, display_name="Botty")
    robot.bot = True
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=42))

    await cog.cmd_profile.callback(cog, ctx, target=robot)

    assert "don't have profiles" in _profile_desc(ctx)
    # No economy row materialized for the bot.
    assert "9105" not in _state.economy["users"]


async def test_profile_works_in_dm(db):
    """DM: guild-only lines (tickets, level) are skipped, embed still sends."""
    cog = ProfileCog(bot=_StubBot())
    member = FakeMember(uid=9106, display_name="Dee")
    ctx = FakeCtx(author=member)
    ctx.guild = None
    await add_balance(9106, 5)

    await cog.cmd_profile.callback(cog, ctx, target=None)

    desc = _profile_desc(ctx)
    assert "Lottery tickets" not in desc
    assert "Chess Ranks" not in desc
