"""Button / dropdown prompts for the settings commands, so an admin can run a
bare `!settings channel game` and pick instead of remembering the syntax.

Like confirm_view, a prompt never writes anything: it returns what the
invoker chose and the command applies it through the same branch its typed
form uses. Only the invoker can operate a prompt.
"""
from __future__ import annotations

from dataclasses import dataclass

import discord
from discord import ui

from src.helpers import emb, C_GOLD, C_GREEN, C_GREY

PROMPT_TIMEOUT = 60.0
PANEL_TIMEOUT = 120.0
MAX_OPTIONS = 25  # Discord's cap on a select's options, and on its max_values


class _OwnedView(ui.View):
    """`result` stays None on Cancel / timeout. `closed_as` titles the closing edit."""

    def __init__(self, owner_id: int, timeout: float):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.result = None
        self.closed_as = "⌛ Timed Out"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Not your prompt.", ephemeral=True)
        return False

    async def finish(self, interaction: discord.Interaction, result, closed_as: str):
        self.result = result
        self.closed_as = closed_as
        await interaction.response.defer()
        self.stop()


class _CancelButton(ui.Button):
    def __init__(self, label: str = "Cancel", closed_as: str = "🚫 Cancelled"):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=4)
        self.closed_as = closed_as

    async def callback(self, interaction: discord.Interaction):
        await self.view.finish(interaction, None, self.closed_as)


async def _run(ctx, view: _OwnedView, title: str, description: str):
    """Send the prompt, wait it out, and retire the components."""
    msg = await ctx.send(embed=emb(title, description, C_GOLD), view=view)
    await view.wait()
    color = C_GREEN if view.result is not None else C_GREY
    try:
        await msg.edit(embed=emb(f"{title} — {view.closed_as}", description, color), view=None)
    except discord.HTTPException:
        pass  # cosmetic — the result stands either way
    return view.result


# ── channels ─────────────────────────────────────────────────────────────────

class _ChannelSelect(ui.ChannelSelect):
    def __init__(self, current_ids: list[int], multi: bool):
        super().__init__(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder="Pick channels…" if multi else "Pick a channel…",
            min_values=1,
            max_values=MAX_OPTIONS if multi else 1,
            default_values=[discord.Object(id=int(cid)) for cid in current_ids][:MAX_OPTIONS],
            row=0,
        )
        self.multi = multi

    async def callback(self, interaction: discord.Interaction):
        view: _ChannelView = self.view  # type: ignore[assignment]
        view.picked = list(self.values)
        if self.multi:
            await interaction.response.defer()  # wait for Save
        else:
            await view.finish(interaction, view.picked, "✅ Saved")


class _SaveButton(ui.Button):
    def __init__(self):
        super().__init__(label="Save", style=discord.ButtonStyle.success, row=4)

    async def callback(self, interaction: discord.Interaction):
        view: _ChannelView = self.view  # type: ignore[assignment]
        if view.picked is None:
            await interaction.response.send_message("Pick at least one channel first, or press Clear.", ephemeral=True)
            return
        await view.finish(interaction, view.picked, "✅ Saved")


class _ClearButton(ui.Button):
    def __init__(self):
        super().__init__(label="Clear", style=discord.ButtonStyle.danger, row=4)

    async def callback(self, interaction: discord.Interaction):
        await self.view.finish(interaction, [], "🧹 Cleared")


class _ChannelView(_OwnedView):
    def __init__(self, owner_id: int, current_ids: list[int], multi: bool, timeout: float):
        super().__init__(owner_id, timeout)
        self.picked = None
        self.add_item(_ChannelSelect(current_ids, multi))
        if multi:
            self.add_item(_SaveButton())
        self.add_item(_ClearButton())
        self.add_item(_CancelButton())


async def pick_channels(ctx, *, title: str, current_ids, multi: bool, typed_usage: str,
                        timeout: float = PROMPT_TIMEOUT) -> "list | None":
    """Channel dropdown. Returns the picked channels, `[]` for Clear, or None
    on Cancel / timeout. A single-channel prompt saves on selection; a
    multi-channel one waits for Save so the list can be edited first."""
    current_ids = [int(c) for c in current_ids if c]
    current = " ".join(f"<#{cid}>" for cid in current_ids) or "none"
    action = "Pick the channels, then **Save**" if multi else "Pick a channel"
    description = f"**Current:** {current}\n\n{action} — or **Clear** to unset.\nUsage: {typed_usage}"
    picked = await _run(ctx, _ChannelView(ctx.author.id, current_ids, multi, timeout), title, description)
    if not picked:
        return picked
    # Select values are partial AppCommandChannels; callers post to these.
    guild = ctx.guild
    return [(guild.get_channel(c.id) if guild else None) or c for c in picked]


# ── toggles ──────────────────────────────────────────────────────────────────

class _ToggleButton(ui.Button):
    def __init__(self, key: str, label: str, enabled: bool):
        super().__init__(label=label)
        self.key = key
        self.enabled = enabled
        self._paint()

    def _paint(self):
        self.style = discord.ButtonStyle.success if self.enabled else discord.ButtonStyle.secondary
        self.emoji = "✅" if self.enabled else "❌"

    async def callback(self, interaction: discord.Interaction):
        view: _ToggleView = self.view  # type: ignore[assignment]
        self.enabled = not self.enabled
        self._paint()
        await view.on_toggle(self.key, self.enabled)
        await interaction.response.edit_message(view=view)


class _ToggleView(_OwnedView):
    def __init__(self, owner_id: int, items: dict, on_toggle, timeout: float):
        super().__init__(owner_id, timeout)
        self.on_toggle = on_toggle
        for key, (label, enabled) in items.items():
            self.add_item(_ToggleButton(key, label, enabled))
        done = _CancelButton(label="Done", closed_as="✅ Done")
        self.add_item(done)


async def toggle_panel(ctx, *, title: str, description: str, items: dict, on_toggle,
                       timeout: float = PANEL_TIMEOUT) -> None:
    """One on/off button per item (`{key: (label, enabled)}`, at most 20).
    Each click awaits `on_toggle(key, enabled)` — which saves — before the
    button repaints, so the panel never shows a state that wasn't stored."""
    await _run(ctx, _ToggleView(ctx.author.id, items, on_toggle, timeout), title, description)


# ── pick from a list / pick users ────────────────────────────────────────────

class _StringSelect(ui.Select):
    def __init__(self, options: list[tuple[str, str]], placeholder: str, multi: bool, row: int = 0):
        shown = options[:MAX_OPTIONS]
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=len(shown) if multi else 1,
            options=[discord.SelectOption(label=label[:100], value=value) for label, value in shown],
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.finish(interaction, list(self.values), "✅ Done")


async def pick_from_list(ctx, *, title: str, description: str, options: list[tuple[str, str]],
                         placeholder: str = "Pick…", multi: bool = True,
                         timeout: float = PROMPT_TIMEOUT) -> "list[str] | None":
    """Dropdown over `(label, value)` pairs; returns the chosen values, or
    None on Cancel / timeout. Only the first 25 options fit in a select."""
    if len(options) > MAX_OPTIONS:
        description += f"\n*Showing the first {MAX_OPTIONS} of {len(options)} — the typed command reaches the rest.*"
    view = _OwnedView(ctx.author.id, timeout)
    view.add_item(_StringSelect(options, placeholder, multi))
    view.add_item(_CancelButton())
    return await _run(ctx, view, title, description)


class _UserSelect(ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Pick users…", min_values=1, max_values=MAX_OPTIONS, row=0)

    async def callback(self, interaction: discord.Interaction):
        await self.view.finish(interaction, list(self.values), "✅ Done")


async def pick_users(ctx, *, title: str, description: str, timeout: float = PROMPT_TIMEOUT) -> "list | None":
    """User dropdown; returns the picked users, or None on Cancel / timeout."""
    view = _OwnedView(ctx.author.id, timeout)
    view.add_item(_UserSelect())
    view.add_item(_CancelButton())
    return await _run(ctx, view, title, description)


# ── forms (modals) ───────────────────────────────────────────────────────────

MODAL_FIELDS = 5  # Discord's cap on top-level modal components


@dataclass(frozen=True)
class Field:
    """One modal field. `kind` is `text` / `paragraph` (a text box),
    `choices` (a dropdown over `options`), or `channels` / `users` / `roles`
    (Discord's own pickers). Text kinds submit a stripped string; select
    kinds a list — of option values, or of channel / user / role ids. An
    optional select submits `[]`, which is how a channel setting is cleared
    from a form."""
    key: str
    label: str                                 # ≤45 chars
    kind: str = "text"
    required: bool = True
    placeholder: str | None = None
    default: str | None = None                 # text kinds
    max_length: int | None = None
    options: tuple[tuple[str, str], ...] = ()  # choices: (label, value)
    defaults: tuple = ()                       # preselected values / ids
    max_values: int = 1
    description: str | None = None             # ≤100 chars, under the label


def _component(f: Field):
    if f.kind in ("text", "paragraph"):
        return ui.TextInput(
            style=discord.TextStyle.paragraph if f.kind == "paragraph" else discord.TextStyle.short,
            placeholder=f.placeholder, default=f.default, required=f.required, max_length=f.max_length,
        )
    min_values = 1 if f.required else 0
    if f.kind == "choices":
        shown = f.options[:MAX_OPTIONS]
        wanted = {str(v) for v in f.defaults}
        return ui.Select(
            placeholder=f.placeholder, min_values=min_values, max_values=max(1, min(f.max_values, len(shown))),
            options=[discord.SelectOption(label=label[:100], value=value, default=value in wanted) for label, value in shown],
            required=f.required,
        )
    defaults = [discord.Object(id=int(v)) for v in f.defaults][:MAX_OPTIONS]
    if f.kind == "channels":
        return ui.ChannelSelect(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder=f.placeholder, min_values=min_values, max_values=f.max_values,
            default_values=defaults, required=f.required,
        )
    if f.kind == "users":
        return ui.UserSelect(
            placeholder=f.placeholder, min_values=min_values, max_values=f.max_values,
            default_values=defaults, required=f.required,
        )
    if f.kind == "roles":
        return ui.RoleSelect(
            placeholder=f.placeholder, min_values=min_values, max_values=f.max_values,
            default_values=defaults, required=f.required,
        )
    raise ValueError(f"unknown field kind {f.kind!r}")


class FormModal(ui.Modal):
    """A modal built from `Field`s. `result` holds `{key: value}` after a
    submit (None until then). `on_submit` — `async (interaction, values)` —
    answers the submit interaction itself when given; otherwise the modal
    just acknowledges it, for a caller that only waits on `result`."""

    def __init__(self, title: str, fields, *, on_submit=None, timeout: float = PROMPT_TIMEOUT):
        super().__init__(title=title[:45], timeout=timeout)
        self.fields = tuple(fields)[:MODAL_FIELDS]
        self.result = None
        self._after = on_submit
        self._inputs = {}
        for f in self.fields:
            component = _component(f)
            self._inputs[f.key] = component
            self.add_item(ui.Label(text=f.label[:45], description=f.description, component=component))

    def values(self) -> dict:
        out = {}
        for f in self.fields:
            component = self._inputs[f.key]
            if f.kind in ("text", "paragraph"):
                out[f.key] = (component.value or "").strip()
            elif f.kind == "choices":
                out[f.key] = list(component.values)
            else:
                out[f.key] = [v.id for v in component.values]
        return out

    async def on_submit(self, interaction: discord.Interaction):
        self.result = self.values()
        if self._after is not None:
            await self._after(interaction, self.result)
        else:
            await interaction.response.defer()
        self.stop()


class _FormButton(ui.Button):
    """Opens the form. A modal can only answer a click, which is why a bare
    text command shows this button first."""

    def __init__(self, label: str, title: str, fields, closed_as: str = "✅ Saved", wrap=lambda values: values):
        super().__init__(label=label, style=discord.ButtonStyle.primary, row=4)
        self.title, self.fields, self.closed_as, self.wrap = title, fields, closed_as, wrap

    async def callback(self, interaction: discord.Interaction):
        view = self.view

        async def _submitted(submit: discord.Interaction, values: dict):
            await view.finish(submit, self.wrap(values), self.closed_as)
        await interaction.response.send_modal(FormModal(self.title, self.fields, on_submit=_submitted))


async def open_form(ctx, *, title: str, description: str, fields, button: str = "Open form…",
                    timeout: float = PROMPT_TIMEOUT) -> "dict | None":
    """A button that opens a modal of `fields`. Returns the submitted values,
    or None on Cancel / timeout."""
    view = _OwnedView(ctx.author.id, timeout)
    view.add_item(_FormButton(button, title, fields))
    view.add_item(_CancelButton())
    return await _run(ctx, view, title, description)


# ── list editor: entries with Add / Remove / Clear ───────────────────────────

class _EntrySelect(ui.Select):
    def __init__(self, entries: list[tuple[str, str]]):
        shown = entries[:MAX_OPTIONS]
        super().__init__(
            placeholder="Pick entries to remove…", min_values=1, max_values=len(shown),
            options=[discord.SelectOption(label=label[:100], value=value) for label, value in shown], row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.picked = list(self.values)
        await interaction.response.defer()  # wait for Remove


class _RemoveButton(ui.Button):
    def __init__(self, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.danger, row=4)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not view.picked:
            await interaction.response.send_message("Pick the entries to remove first.", ephemeral=True)
            return
        await view.finish(interaction, ("remove", view.picked), "🗑️ Removed")


class _ClearAllButton(ui.Button):
    def __init__(self):
        super().__init__(label="Clear all", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction):
        await self.view.finish(interaction, ("clear", None), "🧹 Clear all")


class _ListEditorView(_OwnedView):
    def __init__(self, owner_id: int, entries, add_title, add_fields, *, remove_label: str, clear: bool, timeout: float):
        super().__init__(owner_id, timeout)
        self.picked = None
        if entries:
            self.add_item(_EntrySelect(entries))
        self.add_item(_FormButton("Add…", add_title, add_fields, closed_as="➕ Added", wrap=lambda values: ("add", values)))
        if entries:
            self.add_item(_RemoveButton(remove_label))
            if clear:
                self.add_item(_ClearAllButton())
        self.add_item(_CancelButton(label="Done", closed_as="✅ Done"))


async def list_editor(ctx, *, title: str, description: str, entries: list[tuple[str, str]],
                      add_title: str, add_fields, remove_label: str = "Remove selected",
                      clear: bool = True, timeout: float = PANEL_TIMEOUT) -> "tuple | None":
    """The entries of a list setting (aliases, banned tags, rate-limited
    users) with Add… (a form), Remove selected and Clear all. Returns one
    action — `("add", values)`, `("remove", [values])` or `("clear", None)`
    — for the command to apply through its typed branch, or None on Done /
    timeout."""
    if len(entries) > MAX_OPTIONS:
        description += f"\n*Showing the first {MAX_OPTIONS} of {len(entries)} — the typed command reaches the rest.*"
    view = _ListEditorView(ctx.author.id, entries, add_title, add_fields, remove_label=remove_label, clear=clear, timeout=timeout)
    return await _run(ctx, view, title, description)
