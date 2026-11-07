"""The `!steal` panel: one message, Wallet or Property, a pick, a preview,
then Submit.

The panel only chooses and shows the numbers. Submit calls the cog's
`_run_steal` / `_run_property_theft`, which run every gate again, so a press
can't do what the typed command can't. A typed tier or property name opens
the panel with that pick made. Bare `!steal` opens it with no target and a
Choose target button; the pick runs `_steal_preflight` before the menu
appears. Submit re-reads the numbers first and redraws
instead of robbing when they've moved since the preview was drawn (a wallet
drained, a deed robbed, insurance bought).
"""
from __future__ import annotations

import discord
from discord import ui

from src import property_theft, state
from src.features import feature_enabled
from src.helpers import C_GOLD, C_GREY, emb, format_duration
from src.permissions import is_silenced
from src.properties import PROPERTIES_BY_ID
from src.settings_views import Field, FormModal

STEAL_PANEL_TIMEOUT = 120.0
MAX_DEEDS = 25  # a Discord select's option limit


def _pct(x: float) -> str:
    return f"{round(x * 100, 1):g}%"


def _deed_name(pid: str) -> str:
    row = state.property_owners.get(pid) or {}
    return row.get("custom_name") or PROPERTIES_BY_ID[pid]["name"]


class StealView(ui.View):
    def __init__(self, cog, ctx, target, *, tier: int | None = None, pid: str | None = None,
                 timeout: float = STEAL_PANEL_TIMEOUT):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.ctx = ctx
        self.target = target
        self.tier = tier
        self.pid = pid
        self.choosable = target is None  # opened bare: the target can be (re)chosen
        self.refusal = None              # why the last chosen target can't be robbed
        self.deeds = self._deeds() if target is not None else []
        if tier:
            self.mode = "wallet"
        elif pid:
            self.mode = "property"
        else:
            self.mode = None if self.deeds else "wallet"
        self.shown = None     # the numbers the preview last showed
        self.notice = None
        self.message = None
        self.done = False

    # ── numbers ──────────────────────────────────────────────────────────
    def _deeds(self) -> list[str]:
        gid = self.ctx.guild.id if self.ctx.guild else 0
        if not feature_enabled(gid, "assets"):
            return []
        pids = [pid for pid, row in state.property_owners.items()
                if row["owner_id"] == self.target.id and pid in PROPERTIES_BY_ID]
        pids.sort(key=lambda p: (-property_theft.theft_take(p, state.property_owners[p]), p))
        return pids[:MAX_DEEDS]

    def _balance(self) -> int:
        return int(state.economy["users"].get(str(self.target.id), {}).get("balance", 0))

    def _odds(self) -> dict | None:
        from src.cogs import economy_cog
        thief = self.ctx.author.id
        if self.mode == "wallet" and self.tier:
            return economy_cog.wallet_steal_odds(thief, self._balance(), self.tier)
        if self.mode == "property" and self.pid:
            row = state.property_owners.get(self.pid)
            if row is None or row["owner_id"] != self.target.id:
                return None
            return economy_cog.property_theft_odds(thief, self.pid, row)
        return None

    def _blocked(self) -> str | None:
        """Why the current property pick can't be robbed right now."""
        if self.mode != "property" or not self.pid:
            return None
        row = state.property_owners.get(self.pid)
        if row is None or row["owner_id"] != self.target.id:
            return f"**{_deed_name(self.pid)}** changed hands. Pick another."
        if property_theft.is_damaged(row):
            return f"🔧 **{_deed_name(self.pid)}** was robbed already and can't be hit again until it's repaired."
        if property_theft.stolen_today(self.ctx.author.id):
            return "🕵️ You've already pulled off a property theft today. Try again after the 5am reset."
        return None

    # ── drawing ──────────────────────────────────────────────────────────
    def render(self) -> discord.Embed:
        """Rebuild the components and return the embed, remembering the
        numbers shown for Submit's re-check."""
        odds = self._odds()
        blocked = self._blocked()
        self.shown = odds
        self._build(ready=odds is not None and blocked is None)
        if self.target is None:
            e = emb("🦹 Steal", "Choose who to rob. You'll see the odds, the take and the jail time "
                    "before anything happens.", C_GOLD)
            if self.refusal:
                e.add_field(name="🚫 Not them", value=self.refusal, inline=False)
            return e
        lines = []
        if self.mode is None:
            lines.append(f"Rob **{self.target.display_name}**'s **wallet**, or one of their **properties**?")
        elif self.mode == "wallet":
            lines.append(f"**{self.target.display_name}**'s wallet: **{self._balance():,} 🪙**.")
            if odds is None:
                lines.append("Pick a tier below. Higher tiers take more, but the fine and the jail chance rise too.")
            else:
                lines += self._wallet_lines(odds)
        elif self.pid is None:
            lines.append(f"Pick one of **{self.target.display_name}**'s properties below.")
        elif odds is not None:
            lines += self._property_lines(odds)
        if blocked:
            lines.append(f"\n{blocked}")
        e = emb(f"🦹 Rob {self.target.display_name}", "\n".join(lines), C_GOLD)
        if self.notice:
            e.add_field(name="⚠️ Check again", value=self.notice, inline=False)
        if odds is not None:
            e.set_footer(text="Nothing happens until you press Submit.")
        return e

    def _caught_line(self, odds: dict, fail: float) -> str:
        return (f"🚔 **{_pct(fail)} caught**: fined **{odds['fine']:,} 🪙**, with a **{_pct(odds['jail'])}** "
                f"chance of jail for **{format_duration(odds['sentence'])}** (bail **{odds['bail']:,} 🪙**).")

    def _wallet_lines(self, odds: dict) -> list[str]:
        return [
            f"**Tier {self.tier}** takes **{_pct(odds['pct'])}** of it.",
            f"✅ **{_pct(odds['success'])} success**: you take **{odds['take']:,} 🪙**.",
            self._caught_line(odds, 1 - odds["success"]),
        ]

    def _property_lines(self, odds: dict) -> list[str]:
        prop = PROPERTIES_BY_ID[self.pid]
        guard = " · 🛡️ secured" if odds["secured"] else ""
        lines = [
            f"{prop['emoji']} **{_deed_name(self.pid)}**, tier {prop['tier']}, owned by "
            f"**{self.target.display_name}**{guard}.",
            f"✅ **{_pct(odds['success'])} success**: you take **{odds['take']:,} 🪙** and its income "
            f"drops {property_theft.DAMAGE_PCT}% until it's repaired.",
        ]
        if odds["arrest"]:
            lines.append(f"🕵️ Investigators are on it: a **{_pct(odds['arrest'])}** chance you're arrested "
                         "even then (you keep the loot), and double the jail chance if you're caught.")
        lines.append(self._caught_line(odds, 1 - odds["success"]))
        lines.append("One successful property theft a day.")
        return lines

    def _build(self, *, ready: bool) -> None:
        self.clear_items()
        if self.target is None:
            self.add_item(_TargetButton("Choose target"))
            self.add_item(_CancelButton())
            return
        if self.deeds:
            done_today = property_theft.stolen_today(self.ctx.author.id)
            self.add_item(_ModeButton("wallet", "Wallet", "👛", self.mode == "wallet"))
            self.add_item(_ModeButton("property", "Property", "🏚️", self.mode == "property", disabled=done_today))
        if self.mode == "wallet":
            self.add_item(_TierSelect(self))
        elif self.mode == "property":
            self.add_item(_DeedSelect(self))
        if self.mode is not None:
            submit = _SubmitButton()
            submit.disabled = not ready
            self.add_item(submit)
        if self.choosable:
            self.add_item(_TargetButton("Change target"))
        self.add_item(_CancelButton())

    async def redraw(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=self.render(), view=self)

    # ── presses ──────────────────────────────────────────────────────────
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("Not your robbery.", ephemeral=True)
            return False
        if is_silenced(interaction.user.id, interaction.guild.id if interaction.guild else None):
            await interaction.response.defer()
            return False
        return True

    async def choose(self, interaction: discord.Interaction, values: dict) -> None:
        """The Choose target modal's submit: the steal gates, then the menu
        for that player. A refused pick keeps the previous target."""
        ids = values.get("user") or []
        member = interaction.guild.get_member(int(ids[0])) if ids and interaction.guild else None
        if member is None:
            gate = "Pick someone in this server."
        else:
            refused = await self.cog._steal_preflight(self.ctx, member)
            gate = refused.description if refused is not None else None
        if gate is not None:
            self.refusal = gate
            if self.target is not None:
                self.notice = gate
            await self.redraw(interaction)
            return
        self.target, self.tier, self.pid = member, None, None
        self.refusal = self.notice = None
        self.deeds = self._deeds()
        self.mode = None if self.deeds else "wallet"
        await self.redraw(interaction)

    async def submit(self, interaction: discord.Interaction) -> None:
        odds, blocked = self._odds(), self._blocked()
        if odds is None or blocked or odds != self.shown:
            self.notice = None if blocked else "The numbers changed since you looked. Check them and press Submit again."
            await self.redraw(interaction)
            return
        self.done = True
        self.stop()
        await interaction.response.edit_message(view=None)
        if self.mode == "wallet":
            await self.cog._run_steal(self.ctx, self.target, self.tier)
        else:
            await self.cog._run_property_theft(self.ctx, PROPERTIES_BY_ID[self.pid])

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.done = True
        self.stop()
        await interaction.response.edit_message(
            embed=emb("🦹 Called Off", "You walked away. Nothing happened.", C_GREY), view=None)

    async def on_timeout(self) -> None:
        if self.done or self.message is None:
            return
        try:
            await self.message.edit(view=None)
        except discord.HTTPException:
            pass  # best-effort: the panel may be gone


class _ModeButton(ui.Button):
    def __init__(self, mode: str, label: str, emoji: str, current: bool, *, disabled: bool = False):
        style = discord.ButtonStyle.primary if current else discord.ButtonStyle.secondary
        if disabled:
            label += " (done today)"
        super().__init__(label=label, emoji=emoji, style=style, disabled=disabled, row=0)
        self.mode = mode

    async def callback(self, interaction: discord.Interaction):
        view: StealView = self.view
        view.mode = self.mode
        view.notice = None
        await view.redraw(interaction)


class _TierSelect(ui.Select):
    def __init__(self, view: StealView):
        from src.cogs import economy_cog
        bal = view._balance()
        options = []
        for tier in range(1, len(economy_cog.STEAL_TIERS) + 1):
            odds = economy_cog.wallet_steal_odds(view.ctx.author.id, bal, tier)
            options.append(discord.SelectOption(
                label=f"Tier {tier}: {odds['take']:,} coins ({_pct(odds['pct'])})",
                description=f"{_pct(odds['success'])} success · {_pct(odds['jail'])} jail if caught",
                value=str(tier), default=tier == view.tier,
            ))
        super().__init__(placeholder="Pick a tier…", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: StealView = self.view
        view.tier = int(self.values[0])
        view.notice = None
        await view.redraw(interaction)


class _DeedSelect(ui.Select):
    def __init__(self, view: StealView):
        from src.cogs import economy_cog
        options = []
        for pid in view.deeds:
            row = state.property_owners[pid]
            prop = PROPERTIES_BY_ID[pid]
            if property_theft.is_damaged(row):
                desc = "🔧 damaged: can't be robbed until it's repaired"
            else:
                odds = economy_cog.property_theft_odds(view.ctx.author.id, pid, row)
                desc = (f"Tier {prop['tier']} · {_pct(odds['success'])} · {odds['take']:,} coins"
                        + (" · 🛡️" if odds["secured"] else "") + (" · 🕵️" if odds["arrest"] else ""))
            options.append(discord.SelectOption(
                label=_deed_name(pid)[:100], description=desc[:100], emoji=prop["emoji"],
                value=pid, default=pid == view.pid,
            ))
        super().__init__(placeholder="Pick a property…", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: StealView = self.view
        view.pid = self.values[0]
        view.notice = None
        await view.redraw(interaction)


class _TargetButton(ui.Button):
    def __init__(self, label: str):
        super().__init__(label=label, emoji="🎯", style=discord.ButtonStyle.primary, row=2)

    async def callback(self, interaction: discord.Interaction):
        view: StealView = self.view
        await interaction.response.send_modal(FormModal(
            "Choose target", (Field("user", "Who do you want to rob?", kind="users"),), on_submit=view.choose))


class _SubmitButton(ui.Button):
    def __init__(self):
        super().__init__(label="Submit", emoji="🦹", style=discord.ButtonStyle.danger, row=2)

    async def callback(self, interaction: discord.Interaction):
        await self.view.submit(interaction)


class _CancelButton(ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction):
        await self.view.cancel(interaction)


async def open_steal_panel(cog, ctx, target, *, tier: int | None = None, pid: str | None = None) -> None:
    view = StealView(cog, ctx, target, tier=tier, pid=pid)
    view.message = await ctx.send(embed=view.render(), view=view)
