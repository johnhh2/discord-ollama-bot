"""
Level-gated command catalog.

Single source of truth for which commands unlock at which level, plus the
per-server enabled predicate (e.g. createchannel only counts as a "reward"
if the shop_items.createchannel toggle is on).

Used by:
  - the global pre-invoke check that blocks under-level users
  - !level   (shows next 3 unlocks)
  - the level-up announcement (lists newly unlocked commands with usage)
  - !help / !shop / !games  (greys out locked entries)
"""
from __future__ import annotations

from typing import Callable, Optional

from discord.ext import commands

from src.persistence import get_guild_cfg
from src import state


class LevelLocked(commands.CheckFailure):
    """Raised by the global level-gate when the author hasn't unlocked the command yet.
    The gate sends its own user-facing message; on_command_error should swallow this."""


# ── Per-family server-enabled predicates ──────────────────────────────────────

def _shop_item_enabled(item_key: str) -> Callable[[int], bool]:
    """True if the named shop_items toggle is on for the guild (default True)."""
    def _pred(guild_id: int) -> bool:
        if not guild_id:
            return False
        return bool(get_guild_cfg(guild_id).get("shop_items", {}).get(item_key, True))
    return _pred


def _always(_: int) -> bool:
    return True


# ── Catalog ───────────────────────────────────────────────────────────────────
# Each entry: command name (matches discord.py qualified_name) →
#   level:    1-based display level required
#   enabled:  guild_id → bool, only "advertised" if True (also gates greying)
#   usage:    short one-liner shown in !level / level-up message
#   reward:   True if this is the headline reward shown in !level next-unlocks
#             (one per level — the "advertised" command for that tier)
#
# Same level for a whole family is fine; only entries with reward=True
# appear in the next-3 unlocks list.

UNLOCKS: dict[str, dict] = {
    # Level 2 — savings (advertised: savings)
    "savings":     {"level": 2, "enabled": _always,                       "usage": "`!savings add|remove <amount>` — piggy bank with 1% daily interest", "reward": True},

    # Level 5 — steal (advertised: steal)
    "steal":       {"level": 5, "enabled": _always,                       "usage": "`!steal @user [tier]` — pick a pocket", "reward": True},

    # Level 7 — role family (advertised: createrole). lockrole/unlockrole gated separately at 12.
    # !roles is the role leaderboard — never gated.
    "createrole":  {"level": 7, "enabled": _shop_item_enabled("createrole"), "usage": "`!createrole @user <name> <hex>` — create a custom role", "reward": True},
    "assignrole":  {"level": 7, "enabled": _shop_item_enabled("assignrole"), "usage": "`!assignrole @user <name>` — assign an existing role"},
    "removerole":  {"level": 7, "enabled": _shop_item_enabled("removerole"), "usage": "`!removerole [@user] <name>` — remove a role"},
    "deleterole":  {"level": 7, "enabled": _shop_item_enabled("deleterole"), "usage": "`!deleterole <name>` — permanently delete a role"},
    "renamerole":  {"level": 7, "enabled": _always,                       "usage": "`!renamerole <old> <new>` — rename a role"},
    "rolecolor":   {"level": 7, "enabled": _shop_item_enabled("rolecolor"),  "usage": "`!rolecolor @role <hex>` — change a role's color"},
    "roleup":      {"level": 7, "enabled": _shop_item_enabled("roleup"),     "usage": "`!roleup <name>` — move role up one position"},
    "roledown":    {"level": 7, "enabled": _shop_item_enabled("roledown"),   "usage": "`!roledown <name>` — move role down one position"},

    # Level 10 — mug (advertised: mug)
    "mug":         {"level": 10, "enabled": _always,                      "usage": "`!mug @user <amount>` — pay muggers to take an exact amount from a target (muggers keep it)", "reward": True},

    # Level 12 — role locking
    "lockrole":    {"level": 12, "enabled": _always,                      "usage": "`!lockrole <name>` — lock a role against changes", "reward": True},
    "unlockrole":  {"level": 12, "enabled": _always,                      "usage": "`!unlockrole <name>` — unlock a role (lock owner only)"},

    # Level 15 — unoreverse (advertised: unoreverse)
    "unoreverse":  {"level": 15, "enabled": _shop_item_enabled("unoreverse"), "usage": "`!unoreverse @user` — redirect mock/ragebait/curse to someone else", "reward": True},

    # Level 20 — channel family (advertised: createchannel). lockchannel/unlockchannel gated separately at 25.
    "createchannel": {"level": 20, "enabled": _shop_item_enabled("createchannel"), "usage": "`!createchannel <name>` — create a channel", "reward": True},
    "deletechannel": {"level": 20, "enabled": _shop_item_enabled("deletechannel"), "usage": "`!deletechannel <name>` — delete a bot-created channel"},
    "renamechannel": {"level": 20, "enabled": _shop_item_enabled("renamechannel"), "usage": "`!renamechannel <old> <new>` — rename a channel"},
    "rolechannel":   {"level": 20, "enabled": _shop_item_enabled("rolechannel"), "usage": "`!rolechannel @role <name>` — create a role-locked channel"},

    # Level 25 — channel locking
    "lockchannel":   {"level": 25, "enabled": _always,                    "usage": "`!lockchannel <name>` — lock a channel against changes", "reward": True},
    "unlockchannel": {"level": 25, "enabled": _always,                    "usage": "`!unlockchannel <name>` — unlock a channel (lock owner only)"},
}

# Also gate the !shop <subcommand> form. Maps "shop X" → same entry as "X".
_SHOP_SUBCOMMANDS = {
    "createrole", "assignrole", "removerole", "deleterole", "renamerole",
    "rolecolor", "lockrole", "unlockrole", "roleup", "roledown",
    "nickname", "removenickname",
    "createchannel", "deletechannel", "renamechannel", "lockchannel",
    "unlockchannel", "rolechannel", "unoreverse",
}


def lookup(qualified_name: str) -> Optional[dict]:
    """Return the unlock entry for a command, or None if it isn't level-gated.

    Handles `!shop createrole` and `!createrole` as the same gate.
    """
    if qualified_name in UNLOCKS:
        return UNLOCKS[qualified_name]
    parts = qualified_name.split(" ")
    if len(parts) == 2 and parts[0] == "shop" and parts[1] in _SHOP_SUBCOMMANDS:
        return UNLOCKS.get(parts[1])
    return None


def user_display_level(user_id: int, guild_id: int) -> int:
    """Return the user's 1-based display level, defaulting to 1."""
    if not guild_id:
        return 1
    rec = state.leveling.get(str(guild_id), {}).get(str(user_id))
    if not rec:
        return 1
    return rec.get("level", 0) + 1  # internal 0-based → display 1-based


def next_unlocks(user_id: int, guild_id: int, count: int = 3) -> list[tuple[int, str, dict]]:
    """Return up to *count* upcoming unlocks above the user's current level.

    Only returns entries with reward=True (the headline command per level)
    AND that are enabled on this guild. Sorted by level ascending.
    """
    cur = user_display_level(user_id, guild_id)
    out: list[tuple[int, str, dict]] = []
    for cmd, info in UNLOCKS.items():
        if not info.get("reward"):
            continue
        if info["level"] <= cur:
            continue
        if not info["enabled"](guild_id):
            continue
        out.append((info["level"], cmd, info))
    out.sort(key=lambda x: x[0])
    return out[:count]


def unlocks_at_level(level: int, guild_id: int) -> list[tuple[str, dict]]:
    """Return all commands unlocked exactly at *level* on this guild (enabled only)."""
    out: list[tuple[str, dict]] = []
    for cmd, info in UNLOCKS.items():
        if info["level"] != level:
            continue
        if not info["enabled"](guild_id):
            continue
        out.append((cmd, info))
    return out


def is_locked_for(cmd: str, user_id: int, guild_id: int) -> Optional[int]:
    """If *cmd* is gated and the user is below the gate, return the required level.
    Returns None if the command is unlocked (or not level-gated)."""
    info = UNLOCKS.get(cmd)
    if not info:
        return None
    required = info["level"]
    return required if user_display_level(user_id, guild_id) < required else None


def required_level(cmd: str) -> Optional[int]:
    """Return the level required for *cmd*, or None if not gated."""
    info = UNLOCKS.get(cmd)
    return info["level"] if info else None


def lock_marker(cmd: str, user_id: int, guild_id: int) -> str:
    """Return ' 🔒 Lvl N' if *cmd* is locked for the user, else ''.
    Use as a suffix on help-menu lines (Discord embeds don't support greying text)."""
    req = is_locked_for(cmd, user_id, guild_id)
    return f" 🔒 **Lvl {req}**" if req is not None else ""


def fmt_line(cmd: str, line: str, user_id: int, guild_id: int) -> str:
    """Wrap a help line in ~~strikethrough~~ + 🔒 marker if the command is locked.
    Returns *line* unchanged if not gated or already unlocked."""
    req = is_locked_for(cmd, user_id, guild_id)
    if req is None:
        return line
    return f"~~{line}~~ 🔒 **Lvl {req}**"
