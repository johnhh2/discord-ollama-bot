"""Per-guild feature toggles: which of the bot's big systems a server wants.

A feature is on unless the guild's config says otherwise (`cfg["features"]`,
written by the setup wizard and `!settings features`), so a server that
predates the toggles keeps everything. Children inherit an off switch from
their parent — gambling, savings, assets and the shop are all spent or
earned in coins, so they can't run without the economy, and artifacts are a
shop purchase. Their own stored value is kept, so turning the economy back
on restores exactly what the admin had picked.

The gate is `_feature_gate` in src/core.py, which asks `disabled_feature_for`
about every command; menus ask `feature_enabled` before listing one. Passive
paths that never see a command (shop effects on messages, the auto-daily,
@mention replies, the dailies buttons, the lottery loop) gate themselves the
same way — see CLAUDE.md: Feature toggles.
"""
from __future__ import annotations

from dataclasses import dataclass

from discord.ext import commands

from src.guild_config import get_guild_cfg


@dataclass(frozen=True)
class Feature:
    key: str
    label: str          # emoji + name, as the menus print it
    parent: str | None
    summary: str        # one line for the settings overview / toggle buttons
    question: str       # the setup wizard's description of what turns on


FEATURES: dict[str, Feature] = {
    "economy": Feature(
        "economy", "💰 Economy", None,
        "coins, dailies, crime, pay, leaderboard, lottery, bounties",
        "Coins power most of what I do: `!daily` coins, `!balance`, `!pay`, the leaderboard, "
        "crime (`!steal`, `!mug`, `!bankheist`), coin events, bounties and the dailies channel.\n\n"
        "**These need the economy and are asked about next if you enable it:** "
        "🎲 gambling, 🐷 savings, 🏠 assets, 🛒 shop, 🏺 artifacts.\n"
        "Chess, hangman, tic-tac-toe, Connect 4, races between players, the idle RPG and levels stay on either way.",
    ),
    "gambling": Feature(
        "gambling", "🎲 Gambling", "economy",
        "flip, slots, scratchoffs, blackjack, bot races, sessions, lottery",
        "`!flip`, `!slots`, `!scratchoff`, `!blackjack`, racing the bot for coins, `!session` tables, "
        "the monthly lottery and the Gamblers role.\n"
        "Regular games (chess, hangman, tic-tac-toe, Connect 4, races between players) stay on either way.",
    ),
    "savings": Feature(
        "savings", "🐷 Savings", "economy",
        "piggy bank with daily interest (and bank heists)",
        "`!savings` / `!deposit` / `!withdraw` — a piggy bank that earns daily interest — "
        "and `!bankheist`, which raids someone else's.",
    ),
    "assets": Feature(
        "assets", "🏠 Assets", "economy",
        "real-estate deeds that pay daily rent",
        "`!assets` — unique real-estate deeds that pay rent with the daily claim, with upgrades and a player marketplace.",
    ),
    "shop": Feature(
        "shop", "🛒 Shop", "economy",
        "nicknames, roles, channels, insurance, taxes, mocks, curses, XP",
        "`!shop` — spend coins on nicknames, roles, channels, insurance, taxes, mocks, curses, mutes, ragebait and XP.",
    ),
    "artifacts": Feature(
        "artifacts", "🏺 Artifacts", "shop",
        "permanent per-user upgrades bought with coins",
        "`!artifacts` — permanent per-user upgrades: cheaper bail, extra scratchoffs, better savings interest, "
        "more property income and the like.",
    ),
    "ai": Feature(
        "ai", "🤖 AI", None,
        "ask, stories, roleplay, recaps, AI puzzles, @mention replies",
        "`!ask` (and `/ask`), `!story`, `!roleplay`, `!rpg`, `!tldr`, `!recap`, AI-generated puzzles, "
        "and replies whenever I'm @mentioned. Needs a reachable Ollama server.",
    ),
}

# Which feature(s) a command needs, by qualified name. Looked up with the same
# longest-prefix walk as command_perms ("shop artifacts" before "shop"), so a
# group's subcommands inherit its feature unless listed on their own. Anything
# absent is always on: games, levels, the idle RPG, moderation, settings.
COMMAND_FEATURES: dict[str, tuple[str, ...]] = {
    # economy
    "daily": ("economy",), "balance": ("economy",), "pay": ("economy",),
    "leaderboard": ("economy",), "economy": ("economy",), "records": ("economy",),
    "crime": ("economy",), "steal": ("economy",), "mug": ("economy",), "jail": ("economy",),
    "jailbreak": ("economy",), "bail": ("economy",), "adminjailbreak": ("economy",),
    "event": ("economy",), "admingive": ("economy",),
    "bounty": ("economy",), "bounties": ("economy",), "shop bounty": ("economy",),
    "graph balance": ("economy",), "graph economy": ("economy",), "graph wallet": ("economy",),
    "graph total": ("economy",), "graph crime": ("economy",), "graph admin": ("economy",),
    # gambling
    "flip": ("gambling",), "slots": ("gambling",), "slotsrewards": ("gambling",),
    "scratchoff": ("gambling",), "scratches": ("gambling",), "streak": ("gambling",),
    "scratchoffrewards": ("gambling",), "blackjack": ("gambling",), "session": ("gambling",),
    "gambler-role": ("gambling",), "rig": ("gambling",), "unrig": ("gambling",),
    "lottery": ("gambling",), "graph gambling": ("gambling",),
    # savings
    "savings": ("savings",), "save": ("savings",), "deposit": ("savings",), "withdraw": ("savings",),
    "bankheist": ("savings",), "graph savings": ("savings",), "graph admin savings": ("savings",),
    # assets
    "assets": ("assets",), "daily property": ("assets",),
    "graph assets": ("assets",), "graph admin assets": ("assets",),
    # shop
    "shop": ("shop",), "effects": ("shop",), "roles": ("shop",), "tax": ("shop",),
    "adminragebait": ("shop", "ai"),
    # artifacts
    "artifacts": ("artifacts",), "shop artifacts": ("artifacts",),
    # ai
    "ask": ("ai",), "story": ("ai",), "continue": ("ai",), "tldr": ("ai",),
    "roleplay": ("ai",), "rpg": ("ai",), "invite": ("ai",), "closeall": ("ai",),
    "reverse": ("ai",), "recap": ("ai",), "searchquote": ("ai",), "ai": ("ai",),
    "graph ai": ("ai",),
}


class FeatureDisabled(commands.CheckFailure):
    """Raised by the global feature gate after it has sent its own reply;
    on_command_error swallows it like PermissionDenied."""


def _stored(guild_id: int, key: str) -> bool:
    return bool(get_guild_cfg(guild_id).get("features", {}).get(key, True))


def feature_enabled(guild_id: int | None, key: str) -> bool:
    """Effective state: the feature's own switch and every ancestor's. DMs
    (no guild) have nothing to configure, so everything is on there."""
    if not guild_id:
        return True
    feature = FEATURES[key]
    while feature is not None:
        if not _stored(guild_id, feature.key):
            return False
        feature = FEATURES[feature.parent] if feature.parent else None
    return True


def set_feature(guild_id: int, key: str, enabled: bool) -> None:
    """Write the switch into the guild config. The caller saves."""
    get_guild_cfg(guild_id).setdefault("features", {})[FEATURES[key].key] = bool(enabled)


def feature_states(guild_id: int | None) -> dict[str, bool]:
    """Effective on/off for every feature, in catalog order."""
    return {key: feature_enabled(guild_id, key) for key in FEATURES}


def command_features(qualified_name: str) -> tuple[str, ...]:
    parts = qualified_name.split(" ")
    for i in range(len(parts), 0, -1):
        hit = COMMAND_FEATURES.get(" ".join(parts[:i]))
        if hit is not None:
            return hit
    return ()


def disabled_feature_for(qualified_name: str, guild_id: int | None) -> Feature | None:
    """The feature that keeps `qualified_name` off in this guild, or None.
    Reports the outermost switch — `!slots` with the economy off says
    "Economy", not "Gambling" — so the admin fixes the right toggle."""
    if not guild_id:
        return None
    for key in command_features(qualified_name):
        chain = []
        feature: Feature | None = FEATURES[key]
        while feature is not None:
            chain.append(feature)
            feature = FEATURES[feature.parent] if feature.parent else None
        for feature in reversed(chain):
            if not _stored(guild_id, feature.key):
                return feature
    return None


def features_overview(guild_id: int) -> str:
    """The settings overview's feature line: `💰 Economy ✅ · 🎲 Gambling ❌ …`."""
    states = feature_states(guild_id)
    return " · ".join(f"{FEATURES[key].label} {'✅' if on else '❌'}" for key, on in states.items())
