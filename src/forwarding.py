"""Running a command from a button or a panel as if it had been typed.

A pick on a panel or a button under a post has to end up in the same code
as the typed command — same checks, same side effects, same reply — so the
two can't drift. That means calling the command's callback directly, which
skips the `bot.check`s `process_commands` would have run (permission tier,
feature switch, level lock). `forward` applies those itself and returns a
refusal for the caller to show privately.
"""
from __future__ import annotations

from types import SimpleNamespace

from discord.ext import commands
from discord.ext.commands.view import StringView

from src.features import disabled_feature_for
from src.permissions import permitted_for


class CapturingContext:
    """The command's ctx with `send` swallowed: a forwarded command replies
    into the panel that forwarded it, not the channel. Everything else —
    author, guild, channel, `command` (which `forward` assigns) — is the
    real ctx's."""

    def __init__(self, ctx):
        object.__setattr__(self, "_ctx", ctx)
        object.__setattr__(self, "captured", [])

    def __getattr__(self, name):
        return getattr(self._ctx, name)

    def __setattr__(self, name, value):
        setattr(self._ctx, name, value)

    async def send(self, content=None, *, embed=None, **kwargs):
        self.captured.append(embed if embed is not None else content)
        return None

    @property
    def last(self):
        return self.captured[-1] if self.captured else None


def refusal_for(ctx, command, *, invoked_with: str | None = None, level: bool = True) -> str | None:
    """Why the invoker may not run `command` here, or None. The permission
    tier, the feature switch and (when `level`) the level lock — the gates
    `process_commands` applies and a direct callback call doesn't."""
    name = command.qualified_name
    if not permitted_for(ctx, name):
        return "❌ You can't use that command."
    gid = ctx.guild.id if ctx.guild else None
    feature = disabled_feature_for(name, gid)
    if feature is not None:
        return f"🚫 That needs **{feature.label}**, which is off in this server."
    if level and gid:
        from src.level_unlocks import is_locked_for
        required = is_locked_for(invoked_with or command.name, ctx.author.id, gid)
        if required is not None:
            return f"🔒 That unlocks at **level {required}** in this server."
    return None


async def forward(ctx, command, *args, cog=None, invoked_with: str | None = None, **kwargs) -> None:
    """Run `command` with `ctx` as if typed: `ctx.command` / `invoked_with`
    are set (the permission decorator and roleup/roledown read them), and
    the callback gets the command's own cog as `self` (or `cog`)."""
    ctx.command = command
    ctx.invoked_with = invoked_with or command.name
    await command.callback(command.cog or cog, ctx, *args, **kwargs)


def button_context(bot, interaction, command, *, content: str = "", mentions=()) -> commands.Context:
    """A Context for a button click, so a button can run a prefix command's
    callback for the clicker. The message is a stand-in — the click has no
    message — carrying what commands read off `ctx.message`: the author,
    channel, guild, `mentions` and a `content` for the audit log."""
    channel = interaction.channel
    message = SimpleNamespace(
        id=interaction.id, author=interaction.user, channel=channel, guild=interaction.guild,
        mentions=list(mentions), channel_mentions=[], content=content, attachments=[], reference=None,
        _state=getattr(interaction, "_state", None),
    )

    async def _noop(*a, **k):
        return None
    message.delete = _noop  # `!say` and friends delete the trigger message
    from src.core import SilentContext  # at call time: core imports the cogs' dependencies
    return SilentContext(message=message, bot=bot, view=StringView(""), prefix="!",
                         command=command, invoked_with=command.name)
