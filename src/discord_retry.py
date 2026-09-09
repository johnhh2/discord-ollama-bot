"""Retry Discord REST sends that fail with a transient 503.

discord.py 2.7 retries 500/502/504/524 up to five times inside
``HTTPClient.request`` but raises ``DiscordServerError`` on the very first
503 (``discord/http.py``, the ``status in {500, 502, 504, 524}`` branch).
Discord's edge proxy answers 503 when it can't reach one of its own backends
("upstream connect error or disconnect/reset before headers ... immediate
connect error"), which clears within a second or two — so one blip on one
send used to surface as a full "⚠️ Command Error" report against the
command that happened to be replying.

Only the send paths the bot owns go through here: ``SilentContext.send``
(every ``ctx.send``) and ``send_dm`` (the proactive ``user.send`` sites).
Two deliberate limits:

* **503 only.** The library already retries the other 5xx codes; a 4xx is
  the bot's own fault and retrying it just repeats the mistake.
* **No attachments.** discord.py closes every ``File`` when a send returns
  (``MultipartParameters.__exit__``), success or failure, so a second call
  with the same ``File`` objects fails on closed handles. Sends with
  ``file=``/``files=`` go straight through and keep the old behaviour.

The schedule is short on purpose: the user is waiting in chat, and a command
that sleeps for long holds whatever it holds (a ``_crime_active`` slot, a
blackjack hand, a chess placeholder) — see the concurrency section of
CLAUDE.md. Increasing delays because a 503 from an overloaded upstream is
more likely to clear if we back off than if we hammer it.
"""
import asyncio
import logging
from typing import Any, Awaitable, Callable

import discord

# Sleep before retry N (1-indexed). Three attempts in total, ~4 s worst case.
RETRY_DELAYS_SECS: tuple[float, ...] = (1.0, 3.0)
TRANSIENT_STATUSES: frozenset[int] = frozenset({503})

_sleep = asyncio.sleep  # module attribute so tests can stub the wait


def is_transient_server_error(exc: BaseException) -> bool:
    """True for a ``DiscordServerError`` whose status is a transient blip
    (currently just 503) — the errors the retry covers, and the ones
    ``on_command_error`` reports as a Discord hiccup rather than a bug."""
    return isinstance(exc, discord.DiscordServerError) and exc.status in TRANSIENT_STATUSES


async def retry_on_503(op: Callable[[], Awaitable[Any]], *, what: str = "discord request") -> Any:
    """Await ``op()``; on a transient 503 sleep per ``RETRY_DELAYS_SECS`` and
    call it again. ``op`` must return a *fresh* awaitable each call (a
    coroutine object can only be awaited once). Any other error, and the
    last 503 after the final attempt, propagate unchanged."""
    attempts = len(RETRY_DELAYS_SECS) + 1
    for attempt in range(attempts):
        try:
            return await op()
        except discord.DiscordServerError as exc:
            if not is_transient_server_error(exc) or attempt == attempts - 1:
                raise
            delay = RETRY_DELAYS_SECS[attempt]
            logging.warning(
                "discord_503_retry what=%s attempt=%d/%d sleep_s=%s: %s",
                what, attempt + 1, attempts, delay, exc,
            )
            await _sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


def _has_attachment(kwargs: dict) -> bool:
    return kwargs.get("file") is not None or bool(kwargs.get("files"))


def _call(sender: Callable[..., Awaitable[Any]], content, kwargs: dict) -> Awaitable[Any]:
    # Reproduce the caller's call shape: ``send(embed=...)`` stays keyword-only
    # rather than becoming ``send(None, embed=...)`` — test doubles (and any
    # Messageable-like with a keyword-only send) reject the positional None.
    if content is None:
        return sender(**kwargs)
    return sender(content, **kwargs)


async def send_with_retry(sender: Callable[..., Awaitable[Any]], content=None, *, what: str, **kwargs) -> Any:
    """``sender(content, **kwargs)`` with the 503 retry, unless an attachment
    is present (see module docstring)."""
    if _has_attachment(kwargs):
        return await _call(sender, content, kwargs)
    return await retry_on_503(lambda: _call(sender, content, kwargs), what=what)


async def send_dm(user: discord.abc.Messageable, content=None, **kwargs) -> Any:
    """``user.send(...)`` with the 503 retry. ``Forbidden`` (DMs closed) and
    every other error propagate exactly as ``user.send`` would raise them."""
    what = f"dm uid={getattr(user, 'id', '?')}"
    return await send_with_retry(user.send, content, what=what, **kwargs)
