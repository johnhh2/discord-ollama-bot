"""Registry of presence-status providers.

Feature cogs register a zero-arg callable that returns the status line to
show, or None to hide it (e.g. the Minecraft line hides when nobody is
online). StatusCog (src/cogs/status_cog.py) polls active_statuses() on a
timer and rotates the bot's presence through whatever is visible.

Providers must be synchronous and cheap — they run inside the rotation
loop on every tick. A provider that raises is logged and treated as hidden.
"""
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_providers: "dict[str, Callable[[], Optional[str]]]" = {}


def register(key: str, provider: "Callable[[], Optional[str]]") -> None:
    """Add (or replace) a status provider under a stable key."""
    _providers[key] = provider


def unregister(key: str) -> None:
    _providers.pop(key, None)


def active_statuses() -> "list[str]":
    """Current visible status lines, in stable (key-sorted) order."""
    out = []
    for key in sorted(_providers):
        try:
            text = _providers[key]()
        except Exception:
            logger.exception("[status] provider %r failed", key)
            continue
        if text:
            out.append(text)
    return out
