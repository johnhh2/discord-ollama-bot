"""SilentContext.send defaults silent=True unless caller overrides."""
from unittest.mock import AsyncMock, patch

import pytest

from src.core import SilentContext


_aio = pytest.mark.asyncio


@_aio
async def test_ctx_send_defaults_to_silent_true():
    """ctx.send(...) with no silent kwarg flows through with silent=True."""
    ctx = SilentContext.__new__(SilentContext)  # bypass __init__ which needs real Discord state
    with patch("discord.ext.commands.Context.send", new=AsyncMock()) as send:
        await SilentContext.send(ctx, "hello")
    send.assert_awaited_once()
    _, kwargs = send.call_args
    assert kwargs.get("silent") is True


@_aio
async def test_ctx_send_silent_false_is_preserved():
    """Callers that explicitly need to ping pass silent=False — not overridden."""
    ctx = SilentContext.__new__(SilentContext)
    with patch("discord.ext.commands.Context.send", new=AsyncMock()) as send:
        await SilentContext.send(ctx, "hello", silent=False)
    send.assert_awaited_once()
    _, kwargs = send.call_args
    assert kwargs.get("silent") is False


@_aio
async def test_ctx_send_silent_true_is_preserved():
    """Explicit silent=True is also a no-op override (same as default)."""
    ctx = SilentContext.__new__(SilentContext)
    with patch("discord.ext.commands.Context.send", new=AsyncMock()) as send:
        await SilentContext.send(ctx, "hello", silent=True)
    send.assert_awaited_once()
    _, kwargs = send.call_args
    assert kwargs.get("silent") is True


@_aio
async def test_ctx_send_other_kwargs_pass_through():
    """embed/file/view/etc. all reach the underlying send unchanged."""
    from unittest.mock import MagicMock
    ctx = SilentContext.__new__(SilentContext)
    fake_embed = MagicMock()
    fake_file = MagicMock()
    with patch("discord.ext.commands.Context.send", new=AsyncMock()) as send:
        await SilentContext.send(ctx, embed=fake_embed, file=fake_file)
    _, kwargs = send.call_args
    assert kwargs["embed"] is fake_embed
    assert kwargs["file"] is fake_file
    assert kwargs["silent"] is True
