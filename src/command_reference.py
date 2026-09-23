"""The command reference the AI is given when it answers as itself
(see CLAUDE.md: AI command reference).

Built from the live command tree on every request, so it can't drift from
the code: a command is listed because it is registered, and its line is its
own `help` / `usage` (tests/test_bot_startup.py refuses a visible command
without help text). Nothing here is hand-maintained.
"""

from discord.ext import commands

from src.features import FEATURES, disabled_feature_for, feature_enabled
from src.guild_config import get_guild_cfg
from src.level_unlocks import lookup as unlock_info
from src.permissions import get_command_perm

TIER_LABELS = {
    "server_admin": "server admins only",
    "bot_admin": "bot admins only",
    "global_admin": "bot admins only",
}

RULES = (
    "## Your commands\n"
    "You are a Discord bot. The list below is generated from your own code on every "
    "request, so it is complete and current: these are the ONLY commands you have. "
    "Users type them in chat with the ! prefix; you cannot run them yourself.\n"
    "Rules:\n"
    "- Never invent, guess or \"correct\" a command. If something is not listed, say you "
    "don't have that command and point to the closest listed one, or to !help.\n"
    "- Quote commands exactly as listed, with their arguments.\n"
    "- Commands marked [server admins only] or [bot admins only] refuse everyone else; "
    "say so instead of suggesting them to a regular user.\n"
    "- Commands marked [switched off in this server] don't work here.\n"
    "- <x> is required, [x] optional, a|b a choice. "
    "Line format: !command <arguments> (also !alias) — what it does [notes]\n"
)


def _visible(cmd: commands.Command) -> bool:
    # `hidden=True` on the command and `hidden` in command_perms.json both
    # mean "invisible to regular users" — the reference goes to everyone.
    return not cmd.hidden and not get_command_perm(cmd.qualified_name).get("hidden", False)


def _grouped(bot: commands.Bot) -> list[tuple[commands.Command, list[commands.Command]]]:
    """Visible commands, one entry per callback: the bare forms the idle and
    shop cogs register (`!map` for `!idle map`, `!mock` for `!shop mock`)
    share their subcommand's callback and are aliases, not commands of their
    own. The subcommand leads so the line sorts in with its group."""
    by_callback: dict = {}
    for cmd in bot.walk_commands():
        if not _visible(cmd):
            continue
        by_callback.setdefault(cmd.callback, []).append(cmd)
    groups = []
    for cmds in by_callback.values():
        cmds.sort(key=lambda c: (-len(c.qualified_name), c.qualified_name))
        groups.append((cmds[0], cmds[1:]))
    groups.sort(key=lambda g: g[0].qualified_name)
    return groups


def _notes(cmd: commands.Command, guild_id: int | None) -> list[str]:
    notes = []
    tier = get_command_perm(cmd.qualified_name).get("tier", "everyone")
    if tier in TIER_LABELS:
        notes.append(TIER_LABELS[tier])
    if guild_id:
        if disabled_feature_for(cmd.qualified_name, guild_id) is not None:
            notes.append("switched off in this server")
        elif cmd.cog_name == "IdleCog" and not get_guild_cfg(guild_id).get("idle_channel"):
            notes.append("off until an admin runs !settings channel idle")
    info = unlock_info(cmd.qualified_name)
    if info is not None:
        notes.append(f"unlocks at level {info['level']}")
    return notes


def command_line(cmd: commands.Command, others: list[commands.Command], guild_id: int | None) -> str:
    usage = cmd.usage if cmd.usage is not None else cmd.signature
    head = f"!{cmd.qualified_name}" + (f" {usage}" if usage else "")
    # A subcommand's alias lives under its parent: `!assets shop`, not `!shop`.
    parent = f"{cmd.full_parent_name} " if cmd.full_parent_name else ""
    also = [f"!{c.qualified_name}" for c in others] + [f"!{parent}{a}" for a in cmd.aliases]
    if also:
        head += " (also " + ", ".join(also) + ")"
    desc = (cmd.help or "").strip().splitlines()
    line = f"{head} — {desc[0]}" if desc else head
    notes = _notes(cmd, guild_id)
    if notes:
        line += " [" + "; ".join(notes) + "]"
    return line


def build_command_reference(bot: commands.Bot | None, guild_id: int | None = None) -> str:
    """The rules plus one line per command, for the guild the answer goes to
    (`None` in a DM: no feature switches, no idle channel). Empty when there
    is no bot to read — a cog built without one in tests."""
    if bot is None:
        return ""
    lines = [command_line(cmd, others, guild_id) for cmd, others in _grouped(bot)]
    if guild_id and (off := _switched_off(guild_id)):
        lines.append("Switched off in this server by its admins: " + ", ".join(off) + ".")
    return RULES + "\n".join(lines)


def _switched_off(guild_id: int) -> list[str]:
    return [f.label for f in FEATURES.values() if not feature_enabled(guild_id, f.key)]


def guild_reference(bot: commands.Bot | None):
    """The provider `src.ai` calls: a guild id (or None) to the reference."""
    return lambda guild_id: build_command_reference(bot, guild_id)

