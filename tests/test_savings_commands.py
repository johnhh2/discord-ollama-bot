"""!savings / !deposit / !withdraw command-surface tests: sign routing,
shorthands, `all`, missing-amount usage, and the shared level gate."""
import pytest

from src.cogs.economy_cog import EconomyCog
from src.economy import add_balance, get_balance, get_savings_value
from src.level_unlocks import lookup

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember

pytestmark = pytest.mark.asyncio


class _StubBotUser:
    id = 999_999_999


class _StubBot:
    user = _StubBotUser()


def _ctx(member):
    return FakeCtx(author=member, guild=FakeGuild(gid=42), command_name="savings")


def _cog() -> EconomyCog:
    """EconomyCog with its command copies bound, so the shorthand commands'
    `await self.cmd_savings(ctx, ...)` delegation works without bot.add_cog
    (which is what sets Command.cog in production)."""
    cog = EconomyCog(bot=_StubBot())
    for cmd in cog.__cog_commands__:
        cmd.cog = cog
    return cog


async def test_deposit_and_withdraw_shorthands(db):
    cog = _cog()
    m = FakeMember(uid=8100, display_name="Saver")
    await add_balance(8100, 1_000)

    await cog.cmd_deposit.callback(cog, _ctx(m), amount="600")
    assert await get_balance(8100) == 400
    assert int(await get_savings_value(8100)) == 600

    await cog.cmd_withdraw.callback(cog, _ctx(m), amount="200")
    assert await get_balance(8100) == 600
    assert int(await get_savings_value(8100)) == 400


async def test_negative_amount_always_means_withdraw(db):
    """A minus on the amount flips to withdraw everywhere — matching the
    fused `!savings -N` form — instead of a 'must be positive' dead end."""
    cog = _cog()
    m = FakeMember(uid=8101, display_name="Saver")
    await add_balance(8101, 1_000)
    await cog.cmd_deposit.callback(cog, _ctx(m), amount="500")

    await cog.cmd_withdraw.callback(cog, _ctx(m), amount="-200")
    assert int(await get_savings_value(8101)) == 300

    await cog.cmd_deposit.callback(cog, _ctx(m), amount="-100")
    assert int(await get_savings_value(8101)) == 200

    await cog.cmd_savings.callback(cog, _ctx(m), action="add", amount="-50")
    assert int(await get_savings_value(8101)) == 150
    assert await get_balance(8101) == 850


async def test_plus_sign_and_k_shorthand(db):
    cog = _cog()
    m = FakeMember(uid=8102, display_name="Saver")
    await add_balance(8102, 3_000)

    await cog.cmd_deposit.callback(cog, _ctx(m), amount="+1k")
    assert int(await get_savings_value(8102)) == 1_000

    await cog.cmd_withdraw.callback(cog, _ctx(m), amount="+500")
    assert int(await get_savings_value(8102)) == 500
    assert await get_balance(8102) == 2_500


async def test_all_deposits_wallet_and_withdraws_everything(db):
    cog = _cog()
    m = FakeMember(uid=8103, display_name="Saver")
    await add_balance(8103, 750)

    await cog.cmd_save.callback(cog, _ctx(m), amount="all")
    assert await get_balance(8103) == 0
    assert int(await get_savings_value(8103)) == 750

    await cog.cmd_withdraw.callback(cog, _ctx(m), amount="all")
    assert await get_balance(8103) == 750
    assert int(await get_savings_value(8103)) == 0

    ctx = _ctx(m)
    await cog.cmd_withdraw.callback(cog, ctx, amount="all")
    assert ctx.sent_embeds[-1].title == "❌ Nothing to Move"
    assert await get_balance(8103) == 750


async def test_missing_amount_shows_usage_not_crash(db):
    cog = _cog()
    m = FakeMember(uid=8104, display_name="Saver")
    await add_balance(8104, 100)

    ctx = _ctx(m)
    await cog.cmd_deposit.callback(cog, ctx, amount=None)
    assert ctx.sent_embeds[-1].title == "❌ Missing Amount"
    assert "!deposit" in ctx.sent_embeds[-1].description

    ctx2 = _ctx(m)
    await cog.cmd_withdraw.callback(cog, ctx2, amount="   ")
    assert ctx2.sent_embeds[-1].title == "❌ Missing Amount"
    assert "!withdraw" in ctx2.sent_embeds[-1].description
    assert await get_balance(8104) == 100


async def test_shorthands_share_the_savings_level_gate():
    savings_gate = lookup("savings")
    assert savings_gate is not None
    for cmd in ("save", "deposit", "withdraw"):
        assert lookup(cmd) is savings_gate
