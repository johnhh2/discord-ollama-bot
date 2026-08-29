"""!profile — user-overview embed tests."""
import pytest

import src.state as _state
from src.cogs.profile_cog import ProfileCog, _BONUS_BIN_COUNT
from src.economy import add_balance
from src.games import bot_chess_rewards as br

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember

pytestmark = pytest.mark.asyncio


class _StubBot:
    user = None


def _profile_desc(ctx: FakeCtx) -> str:
    assert ctx.sent_embeds, "profile sent no embed"
    return ctx.sent_embeds[-1].description


async def test_profile_shows_wallet_tickets_and_empty_chess_ranks(db):
    cog = ProfileCog(bot=_StubBot())
    member = FakeMember(uid=9100, display_name="Alice")
    ctx = FakeCtx(author=member, guild=FakeGuild(gid=42))
    await add_balance(9100, 12_345)

    await cog.cmd_profile.callback(cog, ctx, target=None)

    desc = _profile_desc(ctx)
    assert "12,345" in desc
    assert "Lottery tickets: **0**" in desc
    assert "Chess Ranks" in desc
    assert "No bot defeats yet" in desc


async def test_profile_shows_chess_ranks_and_bonus_progress(db):
    cog = ProfileCog(bot=_StubBot())
    member = FakeMember(uid=9101, display_name="Bob")
    ctx = FakeCtx(author=member, guild=FakeGuild(gid=42))
    await br.award_bot_defeat(user_id=9101, guild_id=42, holder_name="Bob", bot_elo=1500)
    await br.award_bot_defeat(user_id=9101, guild_id=42, holder_name="Bob", bot_elo=1200)

    await cog.cmd_profile.callback(cog, ctx, target=None)

    desc = _profile_desc(ctx)
    assert "Max Elo defeated: **1,500**" in desc
    assert "Total Elo defeated: **2,700**" in desc
    assert f"**2/{_BONUS_BIN_COUNT}** claimed" in desc


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
    assert "Chess Ranks" in desc
