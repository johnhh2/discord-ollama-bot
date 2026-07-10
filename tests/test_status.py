"""Status manager: provider registry, presence rotation, scratchoff provider.

The rotation loop is never started as a tasks.Loop — Loop.start is patched
out during cog construction and ticks are driven by awaiting the coro
directly, mirroring the minecraft monitor tests.
"""
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.status_manager as status_manager
import src.cogs.status_cog as status_cog_mod
from src.economy import _ct_today
from src.gambling.scratchoff import scratchoff_status_text, scratchoffs_today
from src.cogs.lottery_cog import lottery_status_text, record_tickets_purchased

pytestmark = pytest.mark.asyncio


# ── Registry ──────────────────────────────────────────────────────────────────

async def test_active_statuses_key_sorted_and_none_hidden():
    status_manager.register("b", lambda: "B line")
    status_manager.register("a", lambda: "A line")
    status_manager.register("c", lambda: None)
    assert status_manager.active_statuses() == ["A line", "B line"]


async def test_raising_provider_is_skipped():
    def boom():
        raise RuntimeError("provider broke")
    status_manager.register("boom", boom)
    status_manager.register("ok", lambda: "still here")
    assert status_manager.active_statuses() == ["still here"]


async def test_unregister_removes_provider():
    status_manager.register("x", lambda: "x line")
    status_manager.unregister("x")
    assert status_manager.active_statuses() == []
    status_manager.unregister("x")  # idempotent


# ── Rotation cog ──────────────────────────────────────────────────────────────

class _FakeBot:
    def __init__(self):
        self.change_presence = AsyncMock()


def _make_status_cog(monkeypatch):
    monkeypatch.setattr("discord.ext.tasks.Loop.start", lambda self, *a, **k: None)
    bot = _FakeBot()
    return bot, status_cog_mod.StatusCog(bot)


async def _tick(cog):
    await cog.rotate_status.coro(cog)


async def test_rotation_cycles_through_active_statuses(monkeypatch):
    bot, cog = _make_status_cog(monkeypatch)
    status_manager.register("a", lambda: "first")
    status_manager.register("b", lambda: "second")

    for _ in range(4):
        await _tick(cog)

    names = [c.kwargs["activity"].name for c in bot.change_presence.call_args_list]
    assert names == ["first", "second", "first", "second"]


async def test_single_status_sent_only_once(monkeypatch):
    bot, cog = _make_status_cog(monkeypatch)
    status_manager.register("a", lambda: "only line")

    await _tick(cog)
    await _tick(cog)

    bot.change_presence.assert_called_once()
    assert bot.change_presence.call_args.kwargs["activity"].name == "only line"


async def test_presence_cleared_when_last_status_hides(monkeypatch):
    bot, cog = _make_status_cog(monkeypatch)
    line = {"text": "visible"}
    status_manager.register("a", lambda: line["text"])

    await _tick(cog)
    line["text"] = None
    await _tick(cog)

    assert bot.change_presence.call_count == 2
    assert bot.change_presence.call_args.kwargs["activity"] is None


async def test_no_presence_call_while_nothing_visible(monkeypatch):
    bot, cog = _make_status_cog(monkeypatch)
    status_manager.register("a", lambda: None)
    await _tick(cog)
    await _tick(cog)
    bot.change_presence.assert_not_called()


async def test_presence_failure_is_swallowed_and_retried(monkeypatch):
    bot, cog = _make_status_cog(monkeypatch)
    bot.change_presence = AsyncMock(side_effect=[RuntimeError("gateway hiccup"), None])
    status_manager.register("a", lambda: "line")

    await _tick(cog)          # send fails; _current must stay unset
    assert cog._current is None
    await _tick(cog)          # retried on the next tick
    assert cog._current == "line"


# ── Scratchoff status provider ────────────────────────────────────────────────

def _scratch_user(used, date):
    return {"scratch_used": used, "scratch_date": date}


async def test_scratchoffs_today_counts_only_current_gameplay_day():
    today = _ct_today()
    _state.economy["users"].update({
        "1": _scratch_user(3, today),
        "2": _scratch_user(2, today),
        "3": _scratch_user(3, "2020-01-01"),  # pre-reset — must not count
        "4": {},                              # never scratched
    })
    assert scratchoffs_today() == 5


async def test_scratchoff_status_hidden_at_or_below_three():
    _state.economy["users"]["1"] = _scratch_user(3, _ct_today())
    assert scratchoff_status_text() is None


async def test_scratchoff_status_visible_above_three():
    today = _ct_today()
    _state.economy["users"].update({
        "1": _scratch_user(3, today),
        "2": _scratch_user(1, today),
    })
    assert scratchoff_status_text() == "4x 🎫 scratched today"


# ── Lottery status provider ───────────────────────────────────────────────────

async def test_lottery_status_hidden_at_zero():
    assert lottery_status_text() is None


async def test_lottery_status_visible_after_purchase():
    await record_tickets_purchased(1)
    assert lottery_status_text() == "1x 🎟️ sold today"
    await record_tickets_purchased(24)
    assert lottery_status_text() == "25x 🎟️ sold today"


async def test_lottery_status_hidden_when_count_is_stale():
    _state.lottery_tickets_today.update({"date": "2020-01-01", "count": 40})
    assert lottery_status_text() is None
    # A purchase on the new day resets the stale count instead of adding to it.
    await record_tickets_purchased(2)
    assert lottery_status_text() == "2x 🎟️ sold today"


async def test_lottery_counter_persists_and_restores(db):
    """Purchases accumulate in daily_counters and survive a 'reboot'
    (init_db_state restoring the count into fresh state)."""
    from src.persistence import bump_daily_counter, load_daily_counter, init_db_state

    await record_tickets_purchased(3)
    await record_tickets_purchased(7)
    today = _ct_today()
    assert await load_daily_counter(today, "lottery_tickets") == 10
    # Yesterday's row is a separate key and doesn't bleed into today.
    await bump_daily_counter("2020-01-01", "lottery_tickets", 99)
    assert await load_daily_counter(today, "lottery_tickets") == 10

    _state.lottery_tickets_today.update({"date": None, "count": 0})  # "reboot"
    await init_db_state()
    assert _state.lottery_tickets_today == {"date": today, "count": 10}
    assert lottery_status_text() == "10x 🎟️ sold today"


async def test_prune_daily_counters_drops_old_rows_only(db):
    from src.persistence import bump_daily_counter, load_daily_counter, prune_daily_counters

    today = _ct_today()
    await bump_daily_counter(today, "lottery_tickets", 5)
    await bump_daily_counter("2020-01-01", "lottery_tickets", 8)
    await prune_daily_counters(before_date="2021-01-01")
    assert await load_daily_counter("2020-01-01", "lottery_tickets") == 0
    assert await load_daily_counter(today, "lottery_tickets") == 5
