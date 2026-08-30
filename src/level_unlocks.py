"""
Level-gated command catalog.

Single source of truth for which commands unlock at which level, plus the
per-server enabled predicate (e.g. channelcreate only counts as a "reward"
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

from src.guild_config import get_guild_cfg
from src.economy import SAVINGS_DAILY_PCT
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
    # Level 3 — savings (advertised: savings)
    "savings":     {"level": 3, "enabled": _always,                       "usage": f"`!deposit` / `!withdraw <amount>` — piggy bank with {SAVINGS_DAILY_PCT} daily interest", "reward": True},

    # Level 5 — role family (advertised: rolecreate). rolelock/roleunlock gated separately at 8.
    # !roles is the role leaderboard — never gated.
    # Keys are the canonical (renamed) command names; the _shop_item_enabled
    # arg keeps the original shop_items toggle key (a persisted config name).
    "rolecreate":  {"level": 5, "enabled": _shop_item_enabled("createrole"), "usage": "`!rolecreate @user <name>` — create a role", "reward": True},
    "roleassign":  {"level": 5, "enabled": _shop_item_enabled("assignrole"), "usage": "`!roleassign @user <name>` — assign an existing role"},
    "roleunassign":  {"level": 5, "enabled": _shop_item_enabled("unassignrole"), "usage": "`!roleunassign [@user] <name>` — remove a role"},
    "roledelete":  {"level": 5, "enabled": _shop_item_enabled("deleterole"), "usage": "`!roledelete <name>` — permanently delete a role"},
    "rolerename":  {"level": 5, "enabled": _always,                       "usage": "`!rolerename <old> <new>` — rename a role"},
    "rolecolor":   {"level": 5, "enabled": _shop_item_enabled("rolecolor"),  "usage": "`!rolecolor @role <hex>` — change a role's color"},
    "roleup":      {"level": 5, "enabled": _shop_item_enabled("roleup"),     "usage": "`!roleup <name>` — move role up one position"},
    "roledown":    {"level": 5, "enabled": _shop_item_enabled("roledown"),   "usage": "`!roledown <name>` — move role down one position"},

    # Level 8 — role locking
    "rolelock":    {"level": 8, "enabled": _always,                      "usage": "`!rolelock <name>` — lock a role against changes", "reward": True},
    "roleunlock":  {"level": 8, "enabled": _always,                      "usage": "`!roleunlock <name>` — unlock a role (lock owner only)"},

    # Level 10 — steal (advertised: steal)
    "steal":       {"level": 10, "enabled": _always,                       "usage": "`!steal @user [tier]` — pick a pocket", "reward": True},

    # Level 13 — mug (advertised: mug)
    "mug":         {"level": 13, "enabled": _always,                      "usage": "`!mug @user <amount>` — pay muggers to take an exact amount from a target (muggers keep it)", "reward": True},

    # Level 15 — bankheist (advertised: bankheist)
    "bankheist":   {"level": 15, "enabled": _always,                       "usage": "`!bankheist @user` — open a 4-slot lobby and split a cut of their savings", "reward": True},

    # Level 18 — unoreverse (advertised: unoreverse)
    "unoreverse":  {"level": 18, "enabled": _shop_item_enabled("unoreverse"), "usage": "`!unoreverse @user` — redirect mock/ragebait/curse to someone else", "reward": True},

    # Level 20 — channel family (advertised: channelcreate). channellock/channelunlock gated separately at 25.
    "channelcreate": {"level": 20, "enabled": _shop_item_enabled("createchannel"), "usage": "`!channelcreate <name>` — create a channel", "reward": True},
    "channeldelete": {"level": 20, "enabled": _shop_item_enabled("deletechannel"), "usage": "`!channeldelete <name>` — delete a bot-created channel"},
    "channelrename": {"level": 20, "enabled": _shop_item_enabled("renamechannel"), "usage": "`!channelrename <old> <new>` — rename a channel"},
    "rolechannel":   {"level": 20, "enabled": _shop_item_enabled("rolechannel"), "usage": "`!rolechannel @role <name>` — create a role-locked channel"},

    # Level 25 — channel locking
    "channellock":   {"level": 25, "enabled": _always,                    "usage": "`!channellock <name>` — lock a channel against changes", "reward": True},
    "channelunlock": {"level": 25, "enabled": _always,                    "usage": "`!channelunlock <name>` — unlock a channel (lock owner only)"},

    # Real-estate tier unlocks (levels 15/20/25). The keys are deliberately
    # NOT command names — !assets itself is never level-gated (per-property
    # gates live in src/properties.py); these entries exist purely so the
    # tiers show up in level-up announcements and the !level next-unlocks
    # list. lookup() matches on qualified_name, so they gate nothing.
    # Tiers 1–2 (levels 5/10) are deliberately left out — low-level players
    # already have property access advertised via the catalog itself.
    "assets tier 3": {"level": 15, "enabled": _always, "usage": "`!assets browse 3` — Tier 3 properties (120k–300k 🪙)", "reward": True},
    "assets tier 4": {"level": 20, "enabled": _always, "usage": "`!assets browse 4` — Tier 4 properties (400k–1m 🪙)", "reward": True},
    "assets tier 5": {"level": 25, "enabled": _always, "usage": "`!assets browse 5` — Tier 5 properties (1.3m–2m 🪙)", "reward": True},
}

# Also gate the !shop <subcommand> form. Maps "shop X" → same entry as "X".
# Uses canonical subcommand names (qualified_name); legacy aliases like
# "shop createrole" resolve to the same command, so qualified_name is already
# the new name by the time the gate runs.
_SHOP_SUBCOMMANDS = {
    "rolecreate", "roleassign", "roleunassign", "roledelete", "rolerename",
    "rolecolor", "rolelock", "roleunlock", "roleup", "roledown",
    "nickname", "removenickname",
    "channelcreate", "channeldelete", "channelrename", "channellock",
    "channelunlock", "rolechannel", "unoreverse",
}


# Top-level shorthand commands that share another command's gate. Kept out of
# UNLOCKS itself so they don't multiply the level-up announcement lines.
_GATE_ALIASES: dict[str, str] = {
    "save": "savings",
    "deposit": "savings",
    "withdraw": "savings",
}


def lookup(qualified_name: str) -> Optional[dict]:
    """Return the unlock entry for a command, or None if it isn't level-gated.

    Handles `!shop rolecreate` and `!rolecreate` as the same gate, and
    shorthand commands (`!deposit`) as their parent's gate (`savings`).
    """
    if qualified_name in UNLOCKS:
        return UNLOCKS[qualified_name]
    if qualified_name in _GATE_ALIASES:
        return UNLOCKS.get(_GATE_ALIASES[qualified_name])
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
    info = lookup(cmd)
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
