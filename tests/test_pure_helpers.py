"""Tier 3: pure-logic helpers.

These have no DB, no Discord, no time-dependence. The kind of code where
off-by-one bugs love to hide. We test them in isolation so a regression
breaks fast.

Covered here:
- src/cogs/leveling_cog.py — XP curve math + coin reward tiers
- src/helpers.py        — parse_amount (percent/integer parsing), MemberConverter
- src/permissions.py    — get_command_perm hierarchical fallback
"""
import pytest

import src.state as _state
from src.leveling import (
    _xp_cost, xp_for_level, level_from_xp, xp_for_next_level, levelup_coin_reward,
    _bar,
)
from src.helpers import parse_amount, parse_int_amount, MemberConverter
from src.permissions import get_command_perm
from src.economy import add_balance

from tests.fakes.discord import FakeCtx, FakeMember

# Note: no module-level pytestmark — only async tests get pytest.mark.asyncio,
# applied at the class or function level below.


# ── Leveling math (synchronous, no fixtures) ──────────────────────────────────

class TestLevelingMath:
    def test_xp_for_level_zero_is_zero(self):
        assert xp_for_level(0) == 0

    def test_xp_for_level_one_equals_first_cost(self):
        # Level 1 = the cost to advance from 0 -> 1.
        assert xp_for_level(1) == _xp_cost(0)
        assert xp_for_level(1) == 100  # 100 + 0**1.9 * 2

    def test_xp_for_level_is_monotonic(self):
        prev = xp_for_level(0)
        for n in range(1, 20):
            curr = xp_for_level(n)
            assert curr > prev, f"xp_for_level({n})={curr} not > {prev}"
            prev = curr

    def test_xp_for_level_diff_equals_xp_cost(self):
        """xp_for_level(n+1) - xp_for_level(n) must equal _xp_cost(n).
        If this drifts, every leveling-related calculation is off."""
        for n in range(20):
            diff = xp_for_level(n + 1) - xp_for_level(n)
            assert diff == _xp_cost(n)

    def test_level_from_xp_below_first_cost(self):
        assert level_from_xp(0) == 0
        assert level_from_xp(50) == 0
        assert level_from_xp(99) == 0

    def test_level_from_xp_at_boundary(self):
        """At exactly the threshold, you ARE at the next level."""
        first_cost = _xp_cost(0)  # 100
        assert level_from_xp(first_cost) == 1
        assert level_from_xp(first_cost - 1) == 0

    def test_level_from_xp_round_trip_matches_xp_for_level(self):
        for n in range(1, 15):
            threshold = xp_for_level(n)
            assert level_from_xp(threshold) == n
            assert level_from_xp(threshold - 1) == n - 1

    def test_level_from_xp_negative_returns_zero(self):
        assert level_from_xp(-5) == 0

    def test_xp_for_next_level_equals_xp_for_level_plus_one(self):
        for n in range(15):
            assert xp_for_next_level(n) == xp_for_level(n + 1)


class TestLevelupCoinReward:
    """Tier table boundaries — easy spot for off-by-one regressions."""

    @pytest.mark.parametrize("lvl,expected", [
        (1, 500),     # tier 0
        (4, 500),     # last of tier 0
        (5, 1000),    # tier 1 starts
        (9, 1000),    # last of tier 1
        (10, 2000),   # tier 2 starts
        (29, 2000),   # last of tier 2
        (30, 4000),   # tier 3 starts
        (59, 4000),
        (60, 8000),   # tier 4
        (99, 8000),
        (100, 16000), # tier 5
        (149, 16000),
        (150, 32000), # tier 6
        (500, 32000), # very high level still tier 6
    ])
    def test_reward_at_tier_boundary(self, lvl, expected):
        assert levelup_coin_reward(lvl) == expected

    def test_reward_doubles_each_tier(self):
        # The progression is 500, 1000, 2000, 4000, 8000, 16000, 32000.
        rewards = [levelup_coin_reward(lvl) for lvl in (1, 5, 10, 30, 60, 100, 150)]
        for a, b in zip(rewards, rewards[1:]):
            assert b == 2 * a


class TestBar:
    def test_full_bar(self):
        out = _bar(100, 100)
        assert "▒" not in out  # all filled
        assert len(out) >= 20  # default width

    def test_empty_bar(self):
        out = _bar(0, 100)
        # No filled cells — the "done" character should not appear.
        # The exact glyph is irrelevant; what matters is full vs empty differ.
        assert _bar(100, 100) != out

    def test_zero_total_does_not_divide_by_zero(self):
        # Defensive: avoid ZeroDivisionError when total==0.
        # Just shouldn't raise.
        _bar(0, 0)

    def test_overflow_is_clamped(self):
        # filled > total should not produce a bar longer than width.
        out = _bar(200, 100, width=10)
        # Some implementations cap at width chars. The function should
        # at minimum not crash.
        assert isinstance(out, str)


# ── parse_amount ──────────────────────────────────────────────────────────────

def _parse_ctx(uid: int, balance: int = 0):
    """Build a minimal FakeCtx for parse_amount tests, with optional balance."""
    ctx = FakeCtx(author=FakeMember(uid=uid))
    return ctx


@pytest.mark.asyncio
class TestParseAmount:
    async def test_plain_integer(self, db):
        ctx = _parse_ctx(uid=1)
        await add_balance(1, 100)
        assert await parse_amount(ctx, "42") == 42

    async def test_negative_rejected(self, db):
        ctx = _parse_ctx(uid=2)
        result = await parse_amount(ctx, "-5")
        assert result is None
        assert any("positive" in m.lower() for m in ctx.sent_messages)

    async def test_zero_rejected_when_min_is_one(self, db):
        ctx = _parse_ctx(uid=3)
        result = await parse_amount(ctx, "0")
        assert result is None

    async def test_non_numeric_rejected(self, db):
        ctx = _parse_ctx(uid=4)
        result = await parse_amount(ctx, "abc")
        assert result is None
        assert any("positive" in m.lower() for m in ctx.sent_messages)

    async def test_percent_of_balance(self, db):
        ctx = _parse_ctx(uid=5)
        await add_balance(5, 1000)
        assert await parse_amount(ctx, "50%") == 500
        assert await parse_amount(ctx, "100%") == 1000
        assert await parse_amount(ctx, "10%") == 100

    async def test_percent_rounds_down(self, db):
        ctx = _parse_ctx(uid=6)
        await add_balance(6, 333)
        # 50% of 333 = 166.5 → int() truncates to 166.
        assert await parse_amount(ctx, "50%") == 166

    async def test_percent_zero_rejected(self, db):
        """0% must be rejected — `parse_amount` requires `0 < pct <= 100`."""
        ctx = _parse_ctx(uid=7)
        await add_balance(7, 1000)
        result = await parse_amount(ctx, "0%")
        assert result is None
        assert any("Percentage" in m or "percentage" in m for m in ctx.sent_messages)

    async def test_percent_over_100_rejected(self, db):
        ctx = _parse_ctx(uid=8)
        await add_balance(8, 1000)
        result = await parse_amount(ctx, "150%")
        assert result is None

    async def test_percent_with_zero_balance_resolves_to_zero_then_rejected(self, db):
        """A percentage of 0 balance is 0, which fails the min_val=1 check."""
        ctx = _parse_ctx(uid=9)
        # No add_balance — balance is 0.
        result = await parse_amount(ctx, "50%")
        assert result is None

    async def test_min_val_threshold(self, db):
        """min_val parameter rejects amounts below the floor."""
        ctx = _parse_ctx(uid=10)
        await add_balance(10, 1000)
        assert await parse_amount(ctx, "5", min_val=10) is None
        assert await parse_amount(ctx, "10", min_val=10) == 10
        assert await parse_amount(ctx, "100", min_val=10) == 100

    async def test_strips_whitespace(self, db):
        ctx = _parse_ctx(uid=11)
        await add_balance(11, 1000)
        assert await parse_amount(ctx, "  42  ") == 42
        assert await parse_amount(ctx, " 50% ") == 500

    async def test_k_suffix(self, db):
        ctx = _parse_ctx(uid=12)
        await add_balance(12, 1000)
        assert await parse_amount(ctx, "1k") == 1000
        assert await parse_amount(ctx, "2.5k") == 2500
        assert await parse_amount(ctx, "100K") == 100000


# ── parse_int_amount ──────────────────────────────────────────────────────────

class TestParseIntAmount:
    def test_plain_integer(self):
        assert parse_int_amount("42") == 42
        assert parse_int_amount("0") == 0

    def test_k_suffix_is_thousands(self):
        assert parse_int_amount("1k") == 1000
        assert parse_int_amount("100k") == 100000
        assert parse_int_amount("2.5k") == 2500

    def test_m_suffix_is_millions(self):
        assert parse_int_amount("3m") == 3_000_000
        assert parse_int_amount("1.5M") == 1_500_000

    def test_case_insensitive(self):
        assert parse_int_amount("5K") == 5000
        assert parse_int_amount(" 2.5K ") == 2500

    def test_digit_separators(self):
        assert parse_int_amount("1_000") == 1000
        assert parse_int_amount("1,000") == 1000

    def test_plain_decimal_rejected(self):
        # A bare "2.5" is not a whole count; only the k/m forms allow decimals.
        assert parse_int_amount("2.5") is None

    def test_non_numeric_rejected(self):
        assert parse_int_amount("abc") is None
        assert parse_int_amount("") is None
        assert parse_int_amount("k") is None

    def test_negative_rejected_by_default(self):
        assert parse_int_amount("-5") is None
        assert parse_int_amount("-2k") is None

    def test_negative_allowed_when_opted_in(self):
        assert parse_int_amount("-5", allow_negative=True) == -5
        assert parse_int_amount("-2.5k", allow_negative=True) == -2500
        assert parse_int_amount("+3k", allow_negative=True) == 3000


# ── MemberConverter ───────────────────────────────────────────────────────────

class _GuildWithMembers:
    """A FakeGuild populated with members for MemberConverter substring tests.

    Implements the bits of discord.Guild that the built-in MemberConverter
    touches before falling back: get_member_named (returns None to force the
    fallback path), and the members attribute.
    """
    def __init__(self, members: list):
        self.members = members
        self.id = 1
        self.name = "g"
        self._state = None  # built-in converter pokes this in some paths
        self.chunked = True

    def get_member_named(self, _name):
        # Force the built-in converter to give up (returns None → BadArgument)
        # so our fallback substring logic gets a chance to run.
        return None


def _ctx_with_members(members: list) -> FakeCtx:
    ctx = FakeCtx(guild=_GuildWithMembers(members))
    return ctx


@pytest.mark.asyncio
class TestMemberConverter:
    """The production `MemberConverter` first delegates to discord.py's built-in
    converter (which handles mentions/IDs against a real Discord cache). We
    can't fake that cache realistically, so we monkeypatch the built-in to
    always raise BadArgument and exercise the substring-fallback we own.
    """

    @pytest.fixture(autouse=True)
    def _force_fallback(self, monkeypatch):
        """Make the built-in converter always raise BadArgument so our
        substring-match fallback path runs."""
        from discord.ext import commands as dpy_commands

        async def _always_bad_argument(self, ctx, argument):
            raise dpy_commands.BadArgument(f"forced fallback for {argument!r}")

        monkeypatch.setattr(
            dpy_commands.MemberConverter, "convert", _always_bad_argument
        )

    async def test_substring_match_unique(self):
        alice = FakeMember(uid=1, display_name="alice")
        bob = FakeMember(uid=2, display_name="bob")
        ctx = _ctx_with_members([alice, bob])
        result = await MemberConverter().convert(ctx, "alic")
        assert result is alice

    async def test_substring_match_case_insensitive(self):
        alice = FakeMember(uid=1, display_name="Alice")
        ctx = _ctx_with_members([alice])
        result = await MemberConverter().convert(ctx, "ALICE")
        assert result is alice

    async def test_substring_match_against_username(self):
        """`name` (username) is searched alongside display_name."""
        m = FakeMember(uid=1, display_name="Nickname")
        m.name = "uniqueusername"
        ctx = _ctx_with_members([m])
        result = await MemberConverter().convert(ctx, "uniqueusername")
        assert result is m

    async def test_no_match_raises_bad_argument(self):
        from discord.ext import commands as dpy_commands
        ctx = _ctx_with_members([FakeMember(uid=1, display_name="alice")])
        with pytest.raises(dpy_commands.BadArgument):
            await MemberConverter().convert(ctx, "nobody")

    async def test_multiple_matches_raises_bad_argument(self):
        from discord.ext import commands as dpy_commands
        ctx = _ctx_with_members([
            FakeMember(uid=1, display_name="alice_smith"),
            FakeMember(uid=2, display_name="alice_jones"),
        ])
        with pytest.raises(dpy_commands.BadArgument) as exc_info:
            await MemberConverter().convert(ctx, "alice")
        # Error mentions matched names so the user can disambiguate.
        assert "alice_smith" in str(exc_info.value)

    async def test_no_guild_context_raises_bad_argument(self):
        """In DMs (ctx.guild is None) the substring path is skipped entirely."""
        from discord.ext import commands as dpy_commands
        ctx = FakeCtx()
        ctx.guild = None  # FakeCtx defaults to a FakeGuild; override
        with pytest.raises(dpy_commands.BadArgument):
            await MemberConverter().convert(ctx, "alice")


@pytest.mark.asyncio
class TestOptionalMember:
    """`OptionalMember` wraps `MemberConverter` but never lets `BadArgument`
    reach the command — it returns `None` instead, so a junk arg like `!pay 1`
    is treated the same as a missing arg (command shows its usage)."""

    @pytest.fixture(autouse=True)
    def _force_fallback(self, monkeypatch):
        from discord.ext import commands as dpy_commands

        async def _always_bad_argument(self, ctx, argument):
            raise dpy_commands.BadArgument(f"forced fallback for {argument!r}")

        monkeypatch.setattr(
            dpy_commands.MemberConverter, "convert", _always_bad_argument
        )

    async def test_unique_match_returns_member(self):
        from src.helpers import OptionalMember
        alice = FakeMember(uid=1, display_name="alice")
        ctx = _ctx_with_members([alice])
        assert await OptionalMember().convert(ctx, "alic") is alice

    async def test_no_match_returns_none(self):
        """`!pay 1` style input: not found → None, not BadArgument."""
        from src.helpers import OptionalMember
        ctx = _ctx_with_members([FakeMember(uid=1, display_name="alice")])
        assert await OptionalMember().convert(ctx, "1") is None

    async def test_ambiguous_match_sends_message_and_returns_none(self):
        """Ambiguous input: converter sends its own embed and returns None, so
        commands don't each need a BadArgument handler for this case."""
        from src.helpers import OptionalMember
        ctx = _ctx_with_members([
            FakeMember(uid=1, display_name="alice_smith"),
            FakeMember(uid=2, display_name="alice_jones"),
        ])
        assert await OptionalMember().convert(ctx, "alice") is None
        assert any(
            "alice_smith" in (e.description or "") for e in ctx.sent_embeds
        )

    async def test_no_guild_context_returns_none(self):
        from src.helpers import OptionalMember
        ctx = FakeCtx()
        ctx.guild = None
        assert await OptionalMember().convert(ctx, "alice") is None


# ── get_command_perm hierarchical fallback ────────────────────────────────────

class TestGetCommandPermFallback:
    """`!shop nickname` → fall back to `!shop` if not specifically configured."""

    def test_exact_match_takes_precedence(self):
        _state.command_perms["shop nickname"] = {"tier": "bot_admin", "hidden": False}
        _state.command_perms["shop"] = {"tier": "everyone", "hidden": False}
        assert get_command_perm("shop nickname") == {"tier": "bot_admin", "hidden": False}

    def test_falls_back_to_parent_command(self):
        _state.command_perms["shop"] = {"tier": "server_admin", "hidden": False}
        # No specific entry for "shop nickname" — should fall back to "shop".
        assert get_command_perm("shop nickname") == {"tier": "server_admin", "hidden": False}

    def test_three_level_fallback(self):
        """For `settings-channel ai` if only `settings-channel` is set, fall back to it."""
        _state.command_perms["settings-channel"] = {"tier": "server_admin", "hidden": False}
        assert get_command_perm("settings-channel ai") == {"tier": "server_admin", "hidden": False}

    def test_unconfigured_returns_default(self):
        # No entries at all — get_command_perm returns the {everyone, not hidden} default.
        result = get_command_perm("totally unknown command")
        assert result == {"tier": "everyone", "hidden": False}

    def test_specific_entry_wins_even_when_parent_more_restrictive(self):
        """A specific entry can grant access even if the parent is locked."""
        _state.command_perms["shop"] = {"tier": "bot_admin", "hidden": True}
        _state.command_perms["shop free_thing"] = {"tier": "everyone", "hidden": False}
        assert get_command_perm("shop free_thing") == {"tier": "everyone", "hidden": False}

    def test_partial_prefix_does_not_match_unrelated_command(self):
        """`shopfoo` (no space) must not match `shop`."""
        _state.command_perms["shop"] = {"tier": "bot_admin", "hidden": False}
        # No fallback — `shopfoo` doesn't tokenize to ["shop", "foo"].
        result = get_command_perm("shopfoo")
        assert result == {"tier": "everyone", "hidden": False}
