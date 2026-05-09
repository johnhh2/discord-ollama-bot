"""Tests for the reusable confirm_purchase / _ConfirmView primitive."""
from unittest.mock import AsyncMock

import pytest

from src.confirm_view import _ConfirmView, confirm_purchase
from tests.fakes.discord import FakeCtx, FakeMember


pytestmark = pytest.mark.asyncio


class _FakeInteraction:
    def __init__(self, user):
        self.user = user
        self.response = type("R", (), {})()
        self.response.send_message = AsyncMock()
        self.response.defer = AsyncMock()


def _confirm_coro(view: _ConfirmView):
    """Unwrap the real async callback function from discord.py's _ItemCallback shim."""
    return view.confirm.callback.callback


def _cancel_coro(view: _ConfirmView):
    return view.cancel.callback.callback


async def test_payer_confirm_sets_value_true():
    payer = FakeMember(uid=42, display_name="payer")
    view = _ConfirmView(payer_id=payer.id, timeout=5)
    await _confirm_coro(view)(view, _FakeInteraction(payer), view.confirm)

    assert view.value is True


async def test_payer_cancel_sets_value_false():
    payer = FakeMember(uid=43, display_name="payer")
    view = _ConfirmView(payer_id=payer.id, timeout=5)
    await _cancel_coro(view)(view, _FakeInteraction(payer), view.cancel)

    assert view.value is False


async def test_wrong_user_rejected_with_ephemeral():
    payer = FakeMember(uid=44, display_name="payer")
    intruder = FakeMember(uid=999, display_name="intruder")
    view = _ConfirmView(payer_id=payer.id, timeout=5)
    interaction = _FakeInteraction(intruder)

    await _confirm_coro(view)(view, interaction, view.confirm)

    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "Not your purchase" in args[0]
    assert kwargs.get("ephemeral") is True
    assert view.value is None


async def test_confirm_purchase_timeout_returns_false():
    """When the view's timeout fires (no clicks), confirm_purchase returns
    False and edits the message to a 'Timed Out' embed.

    discord.py only starts the timeout countdown after the View is attached
    to a real interaction message — in test we simulate the timeout by
    calling the private _dispatch_timeout hook on the View as soon as it's
    created."""
    import asyncio
    payer = FakeMember(uid=45, display_name="payer")
    ctx = FakeCtx(author=payer)

    async def _trigger_timeout_after_send():
        # Wait long enough for confirm_purchase to call ctx.send and reach view.wait().
        await asyncio.sleep(0.05)
        view = ctx.sent_views[0]
        view._dispatch_timeout()

    timeout_task = asyncio.create_task(_trigger_timeout_after_send())
    result = await confirm_purchase(
        ctx,
        title="Test",
        description="Sample purchase",
        cost=1_000,
        payer=payer,
        timeout=300,  # large; we're driving the timeout manually
    )
    await timeout_task

    assert result is False
    assert ctx.sent_views, "view should have been sent"
