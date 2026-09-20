"""Names a guild can turn into `!<word>`: nsfw / story / tax aliases and
counters. Each is served by its own CommandNotFound listener, so a word held
by two of them fires both, and one that matches a real command never fires
at all. Every command that creates such a name checks `name_conflict` first.
"""
from src import state
from src.guild_config import get_guild_cfg

# cfg key → what to call it in the refusal.
ALIAS_FAMILIES = {
    "nsfw_aliases": "an NSFW alias",
    "story_aliases": "a story alias",
    "tax_aliases": "a tax alias",
}
COUNTERS = "counters"


def custom_name_holder(guild_id: int, word: str, *, own: "str | None" = None) -> "str | None":
    """Which custom-name family already holds `word` in this guild, as a
    phrase ("a story alias"), or None. `own` is the family being added to —
    its duplicates are the caller's to report."""
    cfg = get_guild_cfg(guild_id)
    for key, label in ALIAS_FAMILIES.items():
        if key != own and word in cfg.get(key, {}):
            return label
    if own != COUNTERS and word in state.counters.get(guild_id, {}):
        return "a counter"
    return None


def name_conflict(bot, guild_id: int, word: str, *, own: "str | None" = None) -> "str | None":
    """`custom_name_holder`, plus the bot's real commands and their aliases."""
    if bot is not None and bot.get_command(word) is not None:
        return "a bot command"
    return custom_name_holder(guild_id, word, own=own)
