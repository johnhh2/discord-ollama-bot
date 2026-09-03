"""Play-again buttons under a hand-typed gambling result.

`!slots` and `!flip` attach a `PlayAgainView` to their result embed: one or
more buttons that re-run the game for the same player at a given stake
("Roll Again", "Flip Again", "2x"). The buttons vanish from the message
after PLAY_AGAIN_TIMEOUT seconds, or as soon as one is clicked — the replay
posts a fresh result with its own buttons, so the chain continues until the
player stops, runs dry, or lets a set expire.

Only the original player can click. The double-click guard flips before any
await so two fast clicks can't both replay, and every click consults
`is_silenced` (see CLAUDE.md: an interaction never passes on_message, and a
ban may have landed since the result was posted). The channel and level
gates ran on the original command; a replay reuses that verdict — same
user, same channel, within the timeout.

The dailies flip/slots claims never attach one: a daily stake is a one-shot.
"""
from __future__ import annotations

from typing import Awaitable, Callable

import discord

from src.permissions import is_silenced

PLAY_AGAIN_TIMEOUT = 15.0

# Button styles in option order: the same-stake replay, then the raise.
_STYLES = (discord.ButtonStyle.primary, discord.ButtonStyle.success, discord.ButtonStyle.danger)


class PlayAgainView(discord.ui.View):
    def __init__(
        self, author, guild, *, replay: Callable[[int], Awaitable[None]],
        options: list[tuple[str, int]], not_yours: str,
        timeout: float = PLAY_AGAIN_TIMEOUT,
    ):
        """`replay(stake)` runs the game once more at that stake (and attaches
        its own PlayAgainView to the new result). `options` are (label, stake)
        pairs — one button each, styled in order. `not_yours` is the ephemeral
        reply to anyone else who clicks."""
        assert 1 <= len(options) <= len(_STYLES)
        super().__init__(timeout=timeout)
        self.author = author
        self.guild = guild
        self.replay = replay
        self.not_yours = not_yours
        self.message: discord.Message | None = None
        self._fired = False
        for (label, stake), style in zip(options, _STYLES):
            self.add_item(_PlayAgainButton(label=label, stake=stake, style=style))

    async def on_timeout(self):
        if self._fired or self.message is None:
            return
        try:
            await self.message.edit(view=None)
        except discord.HTTPException:
            pass


class _PlayAgainButton(discord.ui.Button):
    def __init__(self, *, label: str, stake: int, style: discord.ButtonStyle):
        super().__init__(label=label, style=style)
        self.stake = stake

    async def callback(self, interaction: discord.Interaction):
        view: PlayAgainView = self.view  # type: ignore[assignment]
        if interaction.user.id != view.author.id:
            await interaction.response.send_message(view.not_yours, ephemeral=True)
            return
        gid = view.guild.id if view.guild else None
        if view._fired or is_silenced(interaction.user.id, gid):
            await interaction.response.defer()
            return
        view._fired = True
        await interaction.response.edit_message(view=None)
        view.stop()
        await view.replay(self.stake)
