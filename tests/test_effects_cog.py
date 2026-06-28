"""!effects command + duration helpers.

Effects are server-scoped: state dicts are keyed (guild_id, uid). These tests
drive EffectsCog methods directly with a FakeCtx (gid=42) and assert on the
embeds it sends and the state it mutates.
"""
import time

import pytest

import src.state as _state
import src.cogs.effects_cog as _effects_cog
from src.cogs.effects_cog import EffectsCog
from src.helpers import parse_duration, format_duration

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild


# Only the async tests need the asyncio mark; the duration-helper tests below
# are plain sync functions, so we mark per-test rather than module-wide.
_aio = pytest.mark.asyncio


# ── duration helpers ───────────────────────────────────────────────────────────

def test_parse_duration_units():
    assert parse_duration("30s") == 30
    assert parse_duration("5m") == 300
    assert parse_duration("2h") == 7200
    assert parse_duration("1d") == 86_400
    assert parse_duration("1w") == 604_800
    assert parse_duration("6mo") == 6 * 2_592_000
    assert parse_duration("1y") == 31_536_000


def test_parse_duration_month_vs_minute():
    # `m` is minutes, `mo` is months — the collision the spec called out.
    assert parse_duration("1m") == 60
    assert parse_duration("1mo") == 2_592_000


def test_parse_duration_decimal_and_case():
    assert parse_duration("1.5h") == 5400
    assert parse_duration("2H") == 7200


def test_parse_duration_rejects_garbage():
    assert parse_duration("") is None
    assert parse_duration("abc") is None
    assert parse_duration("10") is None     # no unit
    assert parse_duration("0s") is None      # non-positive
    assert parse_duration("-5m") is None
    assert parse_duration("h") is None       # no number


def test_format_duration():
    assert format_duration(0) == "0s"
    assert format_duration(45) == "45s"
    assert format_duration(90) == "1m 30s"
    assert format_duration(3661) == "1h 1m"
    assert format_duration(93784) == "1d 2h"


# ── !effects view ──────────────────────────────────────────────────────────────

@_aio
async def test_effects_view_no_effects():
    cog = EffectsCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=1), guild=FakeGuild(gid=42))
    await cog.cmd_effects.callback(cog, ctx)
    assert any("no active effects" in (e.description or "") for e in ctx.sent_embeds)


@_aio
async def test_effects_view_lists_active_with_duration():
    cog = EffectsCog(bot=None)
    uid = 7001
    _state.active_spellchecks[(42, uid)] = {
        "started_by": 9, "days": 1, "channel_id": None,
        "activated_at": time.time(), "expires_at": time.time() + 3600,
    }
    _state.active_curses[(42, uid)] = {"cursed_by": 9, "remaining": 3, "channel_id": None}

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.cmd_effects.callback(cog, ctx)

    desc = ctx.sent_embeds[-1].description
    assert "spellcheck" in desc
    assert "curse" in desc
    assert "left" in desc          # remaining time / count shown


@_aio
async def test_effects_view_permanent_omits_duration():
    cog = EffectsCog(bot=None)
    uid = 7002
    _state.insurance[(42, uid)] = {
        "expires_at": None, "protected_from": ["tax"],
    }
    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.cmd_effects.callback(cog, ctx)
    desc = ctx.sent_embeds[-1].description
    assert "insurance" in desc
    assert "left" not in desc      # no duration for a permanent effect


@_aio
async def test_effects_view_other_user():
    cog = EffectsCog(bot=None)
    target = FakeMember(uid=7003, display_name="victim")
    _state.active_mocks[(42, target.id)] = {"remaining": 2, "started_by": 9, "channel_id": None}

    ctx = FakeCtx(author=FakeMember(uid=7004), guild=FakeGuild(gid=42))

    class _Stub:
        async def convert(self, ctx, arg):
            return target
    import src.cogs.effects_cog as ec
    ec_member = ec.MemberConverter
    ec.MemberConverter = lambda: _Stub()
    try:
        await cog.cmd_effects.callback(cog, ctx, f"<@{target.id}>")
    finally:
        ec.MemberConverter = ec_member

    assert "victim" in (ctx.sent_embeds[-1].title or "")


@_aio
async def test_effects_view_is_guild_scoped():
    """A spellcheck in guild 42 is invisible from guild 99."""
    cog = EffectsCog(bot=None)
    uid = 7005
    _state.active_spellchecks[(42, uid)] = {
        "started_by": 9, "days": 1, "channel_id": None,
        "activated_at": time.time(), "expires_at": time.time() + 3600,
    }
    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=99))
    await cog.cmd_effects.callback(cog, ctx)
    assert any("no active effects" in (e.description or "") for e in ctx.sent_embeds)


# ── !effects list (admin) ──────────────────────────────────────────────────────

@_aio
async def test_effects_list_requires_admin():
    cog = EffectsCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=1, administrator=False), guild=FakeGuild(gid=42))
    await cog.cmd_effects.callback(cog, ctx, "list")
    assert any("No Permission" in (e.title or "") for e in ctx.sent_embeds)


@_aio
async def test_effects_list_shows_all_for_admin():
    cog = EffectsCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=1, administrator=True), guild=FakeGuild(gid=42))
    await cog.cmd_effects.callback(cog, ctx, "list")
    desc = ctx.sent_embeds[-1].description
    for name in ("spellcheck", "tax", "insurance", "mock", "curse", "ragebait"):
        assert name in desc


# ── !effects @user add/remove (admin) ──────────────────────────────────────────

def _admin_ctx(uid=1, gid=42):
    return FakeCtx(author=FakeMember(uid=uid, administrator=True), guild=FakeGuild(gid=gid))


async def _run_with_stub_target(cog, ctx, target, *args):
    class _Stub:
        async def convert(self, c, a):
            return target
    orig = _effects_cog.MemberConverter
    _effects_cog.MemberConverter = lambda: _Stub()
    try:
        await cog.cmd_effects.callback(cog, ctx, *args)
    finally:
        _effects_cog.MemberConverter = orig


@_aio
async def test_effects_add_with_duration(db):
    cog = EffectsCog(bot=None)
    target = FakeMember(uid=7101, display_name="t")
    ctx = _admin_ctx()
    await _run_with_stub_target(cog, ctx, target, f"<@{target.id}>", "add", "spellcheck", "2h")

    entry = _state.active_spellchecks[(42, target.id)]
    assert entry["expires_at"] is not None
    assert 7000 < entry["expires_at"] - time.time() <= 7200
    assert any("Effect Added" in (e.title or "") for e in ctx.sent_embeds)


@_aio
async def test_effects_add_permanent_when_no_duration(db):
    cog = EffectsCog(bot=None)
    target = FakeMember(uid=7102, display_name="t")
    ctx = _admin_ctx()
    await _run_with_stub_target(cog, ctx, target, f"<@{target.id}>", "add", "tax")

    entry = _state.active_taxes[(42, target.id)]
    assert entry["expires_at"] is None       # permanent
    assert entry["master"] == ctx.author.id  # admin becomes master


@_aio
async def test_effects_add_insurance_permanent(db):
    cog = EffectsCog(bot=None)
    target = FakeMember(uid=7103, display_name="t")
    ctx = _admin_ctx()
    await _run_with_stub_target(cog, ctx, target, f"<@{target.id}>", "add", "insurance")
    entry = _state.insurance[(42, target.id)]
    assert "tax" in entry["protected_from"]


@_aio
async def test_effects_remove(db):
    cog = EffectsCog(bot=None)
    target = FakeMember(uid=7104, display_name="t")
    _state.active_spellchecks[(42, target.id)] = {
        "started_by": 9, "days": None, "channel_id": None,
        "activated_at": time.time(), "expires_at": None,
    }
    ctx = _admin_ctx()
    await _run_with_stub_target(cog, ctx, target, f"<@{target.id}>", "remove", "spellcheck")
    assert (42, target.id) not in _state.active_spellchecks
    assert any("Removed" in (e.title or "") for e in ctx.sent_embeds)


@_aio
async def test_effects_add_rejects_counter_based_effect(db):
    cog = EffectsCog(bot=None)
    target = FakeMember(uid=7105, display_name="t")
    ctx = _admin_ctx()
    await _run_with_stub_target(cog, ctx, target, f"<@{target.id}>", "add", "mock")
    assert (42, target.id) not in _state.active_mocks
    assert any("Not Settable" in (e.title or "") for e in ctx.sent_embeds)


@_aio
async def test_effects_add_rejects_bad_duration(db):
    cog = EffectsCog(bot=None)
    target = FakeMember(uid=7106, display_name="t")
    ctx = _admin_ctx()
    await _run_with_stub_target(cog, ctx, target, f"<@{target.id}>", "add", "spellcheck", "bogus")
    assert (42, target.id) not in _state.active_spellchecks
    assert any("Bad Duration" in (e.title or "") for e in ctx.sent_embeds)


@_aio
async def test_effects_add_requires_admin(db):
    cog = EffectsCog(bot=None)
    target = FakeMember(uid=7107, display_name="t")
    ctx = FakeCtx(author=FakeMember(uid=2, administrator=False), guild=FakeGuild(gid=42))
    await _run_with_stub_target(cog, ctx, target, f"<@{target.id}>", "add", "spellcheck", "1d")
    assert (42, target.id) not in _state.active_spellchecks
    assert any("No Permission" in (e.title or "") for e in ctx.sent_embeds)


@_aio
async def test_effects_add_unknown_effect(db):
    cog = EffectsCog(bot=None)
    target = FakeMember(uid=7108, display_name="t")
    ctx = _admin_ctx()
    await _run_with_stub_target(cog, ctx, target, f"<@{target.id}>", "add", "banana", "1d")
    assert any("Unknown Effect" in (e.title or "") for e in ctx.sent_embeds)
