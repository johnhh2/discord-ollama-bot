"""Dropdowns for a typed name that didn't land on exactly one thing.

`resolve_member` and `VoiceChannelConverter` (src/helpers.py) call these when a
name matches several members / voice channels — a dropdown of the candidates
plus "None of the above" — or none at all — Discord's own searchable user or
channel picker, since a guild can have far more than the 25 options a plain
select holds. Only the invoker can answer; a prompt that times out or is
declined returns None and the converter raises with `handled = True` so the
command (and the global error handler) stay quiet: the closed prompt already
says what happened.

Tests: conftest dismisses all four prompts by default; patch `src.pickers.<name>`
to make a pick.
"""
from __future__ import annotations

import discord
from discord import ui

from src.settings_views import _OwnedView, _CancelButton, _run, PROMPT_TIMEOUT, MAX_OPTIONS

NONE_VALUE = "__none__"
NONE_LABEL = "None of the above"


class _OneOfSelect(ui.Select):
    """Single pick over `(label, value, description)` rows, "None of the above" last."""

    def __init__(self, options, placeholder: str):
        shown = options[:MAX_OPTIONS - 1]  # leave the last slot for "None of the above"
        opts = [
            discord.SelectOption(label=label[:100], value=value, description=(desc or None) and desc[:100])
            for label, value, desc in shown
        ]
        opts.append(discord.SelectOption(label=NONE_LABEL, value=NONE_VALUE, emoji="🚫"))
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=opts, row=0)

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        if value == NONE_VALUE:
            await self.view.finish(interaction, None, "🚫 None of the above")
        else:
            await self.view.finish(interaction, value, "✅ Done")


async def pick_one(ctx, *, title: str, description: str, options, placeholder: str = "Pick the one you meant…",
                   timeout: float = PROMPT_TIMEOUT) -> "str | None":
    """Dropdown over `(label, value, description)` rows; the chosen value, or
    None for "None of the above" / timeout."""
    view = _OwnedView(ctx.author.id, timeout)
    view.add_item(_OneOfSelect(options, placeholder))
    return await _run(ctx, view, title, description)


class _OneUserSelect(ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Pick a user…", min_values=1, max_values=1, row=0)

    async def callback(self, interaction: discord.Interaction):
        await self.view.finish(interaction, self.values[0], "✅ Done")


async def pick_one_user(ctx, *, title: str, description: str, timeout: float = PROMPT_TIMEOUT):
    """Discord's user picker (searchable, any guild size); the picked member, or None."""
    view = _OwnedView(ctx.author.id, timeout)
    view.add_item(_OneUserSelect())
    view.add_item(_CancelButton())
    return await _run(ctx, view, title, description)


class _OneVoiceChannelSelect(ui.ChannelSelect):
    def __init__(self):
        super().__init__(channel_types=[discord.ChannelType.voice], placeholder="Pick a voice channel…",
                         min_values=1, max_values=1, row=0)

    async def callback(self, interaction: discord.Interaction):
        await self.view.finish(interaction, self.values[0], "✅ Done")


async def pick_one_voice_channel(ctx, *, title: str, description: str, timeout: float = PROMPT_TIMEOUT):
    """Discord's channel picker, voice channels only; the picked channel, or None."""
    view = _OwnedView(ctx.author.id, timeout)
    view.add_item(_OneVoiceChannelSelect())
    view.add_item(_CancelButton())
    picked = await _run(ctx, view, title, description)
    if picked is None:
        return None
    # The select hands back a partial AppCommandChannel; callers want the real one.
    return (ctx.guild.get_channel(picked.id) if ctx.guild else None) or picked


# ── the prompts the converters use ───────────────────────────────────────────

async def choose_member(ctx, argument: str, matches):
    """Several members matched `argument`: ask which. None if declined."""
    options = [
        (m.display_name, str(m.id), "" if m.name == m.display_name else f"@{m.name}")
        for m in matches
    ]
    picked = await pick_one(
        ctx, title="❓ Which user?",
        description=f"**{argument}** matches more than one person here. Pick the one you meant.",
        options=options,
    )
    if picked is None:
        return None
    return next((m for m in matches if str(m.id) == picked), None)


async def find_member(ctx, argument: str):
    """Nobody matched `argument`: offer the full user list. None if declined."""
    return await pick_one_user(
        ctx, title="❓ User Not Found",
        description=f"Nobody here matches **{argument}**. Pick them from the list instead.",
    )


async def choose_voice_channel(ctx, argument: str, matches):
    """Several voice channels matched `argument`: ask which. None if declined."""
    options = [(ch.name, str(ch.id), "") for ch in matches]
    picked = await pick_one(
        ctx, title="❓ Which voice channel?",
        description=f"**{argument}** matches more than one voice channel. Pick the one you meant.",
        options=options,
    )
    if picked is None:
        return None
    return next((ch for ch in matches if str(ch.id) == picked), None)


async def find_voice_channel(ctx, argument: str):
    """No voice channel matched `argument`: offer the channel picker. None if declined."""
    return await pick_one_voice_channel(
        ctx, title="❓ Channel Not Found",
        description=f"No voice channel here matches **{argument}**. Pick it from the list instead.",
    )
