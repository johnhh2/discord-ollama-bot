"""The first-run questions: which features a server wants.

Runs after the hello message when the bot joins a guild, and again on
`!settings setup`. Every question is a pair of buttons that *any* server
admin or bot admin may press (`can_configure`) — the person who invited the
bot isn't necessarily the one watching the channel. Each answer is saved as
it lands, so a wizard that times out halfway keeps what was answered and
leaves the rest on; nothing is ever switched off by silence.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import ui

import src.persistence as persistence
from src.features import FEATURES, feature_enabled, feature_states, set_feature
from src.helpers import emb, C_GOLD, C_GREEN, C_GREY, C_BLUE
from src.permissions import can_configure

# Long enough for an admin who invited the bot and went to make coffee; the
# questions sit in the channel with their buttons live until then.
WIZARD_TIMEOUT = 900.0

SHOP_ITEMS_HINT = (
    "Individual shop items (nicknames, roles, channels, ragebait, XP…) are toggled separately "
    "with `!settings shop` — do that whenever you like."
)


class _AdminAnswer(ui.View):
    """Enable / Disable buttons. `value` is True/False once an admin has
    clicked, None on timeout."""

    def __init__(self, guild_id: int, current: bool, timeout: float):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.value: bool | None = None
        self.answered_by = None
        self.add_item(_AnswerButton("Enable", True, current))
        self.add_item(_AnswerButton("Disable", False, not current))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if can_configure(interaction.user, self.guild_id):
            return True
        await interaction.response.send_message(
            "Only a server admin or bot admin can answer this.", ephemeral=True,
        )
        return False

    async def answer(self, interaction: discord.Interaction, value: bool):
        self.value = value
        self.answered_by = interaction.user
        await interaction.response.defer()
        self.stop()


class _AnswerButton(ui.Button):
    def __init__(self, label: str, value: bool, highlighted: bool):
        style = discord.ButtonStyle.success if value else discord.ButtonStyle.danger
        super().__init__(label=label, style=style if highlighted else discord.ButtonStyle.secondary)
        self.value = value

    async def callback(self, interaction: discord.Interaction):
        await self.view.answer(interaction, self.value)


async def _ask(channel, guild_id: int, key: str, step: str, *, timeout: float) -> "bool | None":
    """One question. Returns the answer, or None when nobody answered."""
    feature = FEATURES[key]
    current = feature_enabled(guild_id, key)
    title = f"{step} {feature.label}"
    body = f"{feature.question}\n\n**Currently:** {'✅ on' if current else '❌ off'}"
    view = _AdminAnswer(guild_id, current, timeout)
    msg = await channel.send(embed=emb(title, body, C_GOLD), view=view, silent=True)
    # Bounded here, not only by the view's own clock: discord.py starts that
    # clock when the client dispatches the view, which a stubbed send never does.
    try:
        await asyncio.wait_for(view.wait(), timeout)
    except asyncio.TimeoutError:
        view.stop()
    if view.value is None:
        closing = emb(f"⌛ {title} — no answer", body, C_GREY)
    else:
        who = getattr(view.answered_by, "display_name", "an admin")
        closing = emb(f"{'✅' if view.value else '❌'} {title} — {'enabled' if view.value else 'disabled'} by {who}", body, C_GREEN if view.value else C_GREY)
    try:
        await msg.edit(embed=closing, view=None)
    except discord.HTTPException:
        pass  # cosmetic — the answer stands either way
    return view.value


async def run_setup_wizard(guild, channel, *, timeout: float = WIZARD_TIMEOUT) -> bool:
    """Ask the feature questions in `channel` and save each answer. Returns
    True when every question was answered, False when one timed out."""
    gid = guild.id
    asked = 0

    async def ask(key: str) -> "bool | None":
        nonlocal asked
        asked += 1
        answer = await _ask(channel, gid, key, f"Setup {asked} ·", timeout=timeout)
        if answer is not None:
            set_feature(gid, key, answer)
            await persistence.save_guild_settings()
        return answer

    economy = await ask("economy")
    if economy is None:
        return await _paused(channel, gid)
    if economy:
        # The extensions, each its own question — an admin who wants coins
        # but no casino says so here.
        for key in ("gambling", "savings", "assets"):
            if await ask(key) is None:
                return await _paused(channel, gid)
        shop = await ask("shop")
        if shop is None:
            return await _paused(channel, gid)
        if shop and await ask("artifacts") is None:
            return await _paused(channel, gid)
        shop_note = SHOP_ITEMS_HINT
    else:
        shop_note = (
            "🛒 Shop and 🏺 artifacts are off with the economy (they're bought with coins). "
            + SHOP_ITEMS_HINT
        )
    await channel.send(embed=emb("🛒 Shop items", shop_note, C_BLUE), silent=True)

    if await ask("ai") is None:
        return await _paused(channel, gid)

    await channel.send(embed=emb("✅ Setup complete", _summary(gid), C_GREEN), silent=True)
    return True


def _summary(gid: int) -> str:
    lines = [f"{FEATURES[key].label} {'✅' if on else '❌'}" for key, on in feature_states(gid).items()]
    return (
        "\n".join(lines)
        + "\n\nChange any of these with `!settings features`, set channels with `!settings channel`, "
        "and see everything with `!settings`. `!settings setup` runs these questions again."
    )


async def _paused(channel, gid: int) -> bool:
    await channel.send(embed=emb(
        "⌛ Setup paused",
        "Nobody answered, so I've kept what was answered so far and left the rest on.\n\n"
        + _summary(gid),
        C_GREY,
    ), silent=True)
    return False


async def start_setup_after_join(guild, channel) -> None:
    """The on_guild_join entry point: best-effort, never raises into the
    listener."""
    try:
        await run_setup_wizard(guild, channel)
    except (discord.Forbidden, discord.HTTPException) as e:
        logging.warning("setup_wizard_aborted guild=%s error=%s", guild.id, type(e).__name__)
