"""The `!settings` panel: one message, a category dropdown, a setting
dropdown, and nothing typed after the command.

The panel never writes a setting itself. Every pick is turned into the
typed form of the matching settings command — `settings features economy
off`, `settings channel lottery <#id>`, `tax-aliases add rent 💰` — and
forwarded through `SettingsCog._forward`, so the subcommand's own
`@requires_perm` and side effects (the lottery seed, the dailies refresh,
the gambler role) run exactly as they would for a typed command. The
command's reply is captured (`CapturingContext`, src/forwarding.py) and shown in the panel
instead of posted to the channel.

Two kinds of pick: a *toggle* applies at once (on ↔ off, server ↔ global);
a *form* opens a modal — channel pickers, text boxes, dropdowns — whose
submit applies. The catalog below (`items_for`) is the one place that
lists what the panel offers; `!settings` and `/settings` both open it.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Callable

import discord
from discord import ui

from src import state
from src.ai import list_ollama_models
from src.config import OLLAMA_MODEL
from src.features import FEATURES, feature_enabled
from src.guild_config import get_guild_cfg
from src.helpers import emb, C_BLUE, C_GREY
from src.permissions import is_admin
from src.settings_views import Field, FormModal, MAX_OPTIONS, _OwnedView

HUB_TIMEOUT = 300.0
DESCRIPTION_MAX = 100  # a select option's description

# (label, cfg key, cog method, several channels?, text when unset, note) —
# every per-guild channel setting, as the overview prints it and the panel
# offers it. The subcommand is `!settings channel <name>`.
CHANNEL_SETTINGS = (
    ("✅ Whitelist", "command_whitelist", "settings_channel_whitelist", True, "none (all allowed)", "commands only work in these channels; `!settings` works everywhere"),
    ("❌ Blacklist", "command_blacklist", "settings_channel_blacklist", True, "none", "commands never work in these channels"),
    ("🤖 AI", "ai_channels", "settings_channel_ai", True, "all channels", "where @mentions and AI commands answer"),
    ("🎮 Games", "game_channels", "settings_channel_game", True, "all channels", "games and gambling"),
    ("♟️ Chess", "chess_channels", "settings_channel_chess", True, "game channels (or all)", None),
    ("🎰 Lottery", "lottery_channel", "settings_channel_lottery", False, "❌ off", "the monthly lottery runs here"),
    ("📊 Level-up posts", "levelup_channel", "settings_channel_levelup", False, "❌ off (levels still count)", "level-up announcements — XP, levels and rewards accrue silently without one"),
    ("🏆 Records", "records_channel", "settings_channel_records", False, "❌ off", "this server's new records, plus every new global-top record from any server"),
    ("📖 Feature requests", "feature_request_channel", "settings_channel_feature_request", False, "❌ off", "`!featurerequest` posts here"),
    ("🎯 Bounties", "bounty_channel", "settings_bounty_channel", False, "❌ off", "`!bounty` posts here"),
    ("⛏️ Minecraft", "minecraft_channel", "settings_minecraft_channel", False, "❌ off", "server up/down alerts and player-count notices"),
    ("🪙 Dailies", "dailies_channel", "settings_dailies_channel", False, "❌ off", "self-cleaning channel with the react-to-claim dailies embed"),
    ("⚔️ Idle RPG", "idle_channel", "settings_channel_idle", False, "❌ off", "idle RPG news and feed threads — the game is off without one"),
)
# Bot-wide, in `state.bot_settings` (as str ids) — bot admins only.
GLOBAL_CHANNEL_SETTINGS = (
    ("🛡️ Admin log (global)", "admin_log_channel", "settings_channel_admin_log", False, "❌ off", "admin command use and errors from every server"),
    ("⚠️ Error log (global)", "error_log_channel", "settings_channel_error_log", False, "❌ off", "command errors from every server"),
    ("🐛 Internal issues (global)", "internal_issue_channel", "settings_channel_internal_issue", False, "❌ off", "bug reports and internal issues from every server"),
)

SHOP_ITEMS = ("nickname", "role", "unassignrole", "roleup", "roledown", "ragebait", "buyxp")

CATEGORIES = (
    ("overview", "📋 Overview"),
    ("features", "🧩 Features"),
    ("channels", "📁 Channels"),
    ("shop", "🛒 Shop items"),
    ("games", "🎛️ Games & extras"),
    ("aliases", "🔤 Aliases & limits"),
    ("ai", "🤖 AI"),
)
_CATEGORY_LABELS = dict(CATEGORIES)


def mentions(value, empty: str) -> str:
    ids = value if isinstance(value, list) else ([value] if value else [])
    return " ".join(f"<#{c}>" for c in ids) if ids else empty


def channel_rows(cfg: dict, *, with_global: bool) -> list[tuple[str, str, str | None]]:
    """(label, current value, note) for the overview embeds."""
    rows = [(label, mentions(cfg.get(key), empty), note) for label, key, _, _, empty, note in CHANNEL_SETTINGS]
    if with_global:
        rows += [(label, mentions(state.bot_settings.get(key), empty), note) for label, key, _, _, empty, note in GLOBAL_CHANNEL_SETTINGS]
    return rows


def _on(value: bool) -> str:
    return "✅ on" if value else "❌ off"


@dataclass(frozen=True)
class Item:
    """One pick in the setting dropdown. A toggle forwards `args`; a form
    opens `fields` and forwards `to_args(values)`. `method` is a SettingsCog
    attribute, or `bot:<qualified name>` for another cog's command."""
    key: str
    label: str
    value: str
    method: str
    args: tuple = ()
    fields: tuple = ()
    to_args: Callable[[dict], tuple] | None = None
    title: str = ""
    kind: str = "toggle"  # toggle | form | action
    note: str = ""        # shown after the value in the category embed

    @property
    def is_form(self) -> bool:
        return bool(self.fields)


def _channel_args(values: dict) -> tuple:
    ids = values.get("channels") or []
    return ("clear",) if not ids else tuple(f"<#{cid}>" for cid in ids)


def _channel_item(label, cfg_key, method, multi, empty, note, *, current) -> Item:
    ids = current if isinstance(current, list) else ([int(current)] if current else [])
    return Item(
        key=f"channel:{cfg_key}", label=label, value=mentions(ids, empty), method=method, note=note or "",
        title=f"{label} channel{'s' if multi else ''}",
        fields=(Field(
            "channels", "Pick channels" if multi else "Pick a channel", kind="channels", required=False,
            max_values=MAX_OPTIONS if multi else 1, defaults=tuple(ids),
            description=(note or "")[:DESCRIPTION_MAX] or None, placeholder="Leave empty to clear",
        ),),
        to_args=_channel_args, kind="form",
    )


def _features(gid: int) -> list[Item]:
    stored = get_guild_cfg(gid).get("features", {})
    items = []
    for key, f in FEATURES.items():
        own = stored.get(key, True)
        value = _on(own)
        if own and not feature_enabled(gid, key):
            value += f" · waiting on {FEATURES[f.parent].label}"
        items.append(Item(key=f"feature:{key}", label=f"{f.label} — {f.summary}", value=value,
                          method="settings_features", args=(key, "off" if own else "on")))
    items.append(Item(key="setup", label="🧭 Ask the setup questions again", value="posts them in this channel",
                      method="settings_setup", kind="action"))
    return items


def _channels(cfg: dict, *, with_global: bool) -> list[Item]:
    items = [_channel_item(*row, current=cfg.get(row[1])) for row in CHANNEL_SETTINGS]
    if with_global:
        items += [_channel_item(*row, current=state.bot_settings.get(row[1])) for row in GLOBAL_CHANNEL_SETTINGS]
    return items


def _shop(cfg: dict) -> list[Item]:
    shop_items = cfg.get("shop_items", {})
    return [Item(key=f"shop:{name}", label=f"🛒 {name}", value=_on(shop_items.get(name, True)),
                 method="settings_shop", args=(name, "off" if shop_items.get(name, True) else "on"))
            for name in SHOP_ITEMS]


def _games(cfg: dict) -> list[Item]:
    gambler = cfg.get("gambler_role_enabled", False)
    quote = cfg.get("quote_bypass_restrictions", False)
    scope = cfg.get("leaderboard_default_scope", "global")
    pace = cfg.get("idle_pace") or "lively"
    enroll = bool(cfg.get("idle_enroll"))
    nsfw = cfg.get("nsfw_enabled", False)
    nsfw_channels = [int(c) for c in cfg.get("nsfw_channels", [])]
    banned = list(cfg.get("nsfw_banned_tags", []))
    items = [
        Item(key="gambler-role", label="🎲 Gambler role — auto-assign for scratchoff streaks", value=_on(gambler),
             method="settings_gambler_role", args=("off" if gambler else "on",)),
        Item(key="quote", label="💬 Quote bypass — !quote works in any channel", value=_on(quote),
             method="settings_quote", args=("bypass", "off" if quote else "on")),
        Item(key="leaderboard", label="🪙 Leaderboard default scope — server or global", value=scope,
             method="settings_leaderboard", args=("global" if scope == "server" else "server",)),
        Item(key="idle-pace", label="⚔️ Idle RPG pace — lively or classic", value=pace,
             method="settings_idle_pace", args=("classic" if pace == "lively" else "lively",)),
        Item(key="idle-enroll", label="⚔️ Idle RPG auto-enroll — characters for members with my roles", value=_on(enroll),
             method="settings_idle_enroll", args=("off" if enroll else "on",)),
        Item(key="nsfw", label="🔞 NSFW commands", value=_on(nsfw),
             method="settings_nsfw", args=("off" if nsfw else "on",)),
        Item(key="nsfw-channels", label="🔞 NSFW channels — where !nsfw may answer", value=mentions(nsfw_channels, "all channels"),
             method="settings_nsfw", title="NSFW channels", kind="form",
             fields=(Field("channels", "Pick channels", kind="channels", required=False, max_values=MAX_OPTIONS,
                           defaults=tuple(nsfw_channels), placeholder="Leave empty for all channels"),),
             to_args=lambda v: ("channels", "clear") if not v.get("channels") else ("channels", "set", *(f"<#{c}>" for c in v["channels"]))),
        Item(key="nsfw-ban", label="🔞 Ban an NSFW tag", value=", ".join(banned) or "none banned",
             method="settings_nsfw", title="Ban an NSFW tag", kind="form",
             fields=(Field("tag", "Tag", placeholder="one tag", max_length=100),),
             to_args=lambda v: ("ban", v["tag"])),
    ]
    if banned:
        items.append(Item(key="nsfw-unban", label="🔞 Unban NSFW tags", value=", ".join(banned),
                          method="settings_nsfw", title="Unban NSFW tags", kind="form",
                          fields=(Field("tags", "Tags to unban", kind="choices", max_values=MAX_OPTIONS,
                                        options=tuple((t, t) for t in banned)),),
                          to_args=lambda v: ("unban", *v["tags"])))
    return items


def _alias_items(key: str, label: str, aliases: dict, method: str, add_fields: tuple, to_add, *, shown) -> list[Item]:
    items = [Item(key=f"{key}-add", label=f"{label} — add one", value=shown or "none", method=method,
                  title=f"Add a {label.split(' ', 1)[1].lower()}", kind="form", fields=add_fields, to_args=to_add)]
    if aliases:
        items.append(Item(key=f"{key}-remove", label=f"{label} — remove", value=shown, method=method,
                          title=f"Remove {label.split(' ', 1)[1].lower()}es", kind="form",
                          fields=(Field("words", "Aliases to remove", kind="choices", max_values=MAX_OPTIONS,
                                        options=tuple((f"!{w}", w) for w in list(aliases)[:MAX_OPTIONS])),),
                          to_args=lambda v: ("remove", *v["words"])))
    return items


def _aliases(cfg: dict, guild) -> list[Item]:
    tax, story, nsfw = cfg.get("tax_aliases", {}), cfg.get("story_aliases", {}), cfg.get("nsfw_aliases", {})
    items = []
    items += _alias_items(
        "tax", "🏷️ Tax alias", tax, "settings_tax_aliases",
        (Field("word", "Alias word", placeholder="rent", max_length=32),
         Field("emoji", "Emoji shown in the tax message", required=False, placeholder="💰", max_length=8)),
        lambda v: ("add", v["word"]) + ((v["emoji"],) if v.get("emoji") else ()),
        shown=", ".join(f"{e} !{w}" for w, e in tax.items()),
    )
    items += _alias_items(
        "story", "📖 Story alias", story, "settings_story_alias",
        (Field("word", "Alias word", placeholder="scifi", max_length=32),
         Field("prompt", "System prompt", kind="paragraph", placeholder="You write hard science fiction…", max_length=2000)),
        lambda v: ("add", v["word"], v["prompt"]),
        shown=", ".join(f"!{w}" for w in story),
    )
    items += _alias_items(
        "nsfw", "🔞 NSFW alias", nsfw, "settings_nsfw_alias",
        (Field("word", "Alias word", placeholder="pics", max_length=32),
         Field("tags", "Tags it pre-fills", required=False, placeholder="optional", max_length=200)),
        lambda v: ("add", v["word"]) + ((v["tags"],) if v.get("tags") else ()),
        shown=", ".join(f"!{w}" for w in nsfw),
    )
    limited = [int(u) for u in cfg.get("soundboard_ratelimit", [])]

    def _name(uid: int) -> str:
        member = guild.get_member(uid) if guild is not None else None
        return member.display_name if member else str(uid)
    names = ", ".join(_name(u) for u in limited)
    items.append(Item(key="soundboard-add", label="🔇 Soundboard rate-limit — add users", value=names or "none",
                      method="settings_soundboard_ratelimit", title="Rate-limit soundboard users", kind="form",
                      fields=(Field("users", "Users to rate-limit", kind="users", max_values=MAX_OPTIONS),),
                      to_args=lambda v: ("add", *(str(u) for u in v["users"]))))
    if limited:
        items.append(Item(key="soundboard-remove", label="🔇 Soundboard rate-limit — remove users", value=names,
                          method="settings_soundboard_ratelimit", title="Lift soundboard rate-limits", kind="form",
                          fields=(Field("users", "Users to free", kind="choices", max_values=MAX_OPTIONS,
                                        options=tuple((_name(u), str(u)) for u in limited[:MAX_OPTIONS])),),
                          to_args=lambda v: ("remove", *v["users"])))
    return items


def _model_field(current: str, models: list[str] | None) -> Field:
    """A dropdown of installed models when Ollama answered, else a text box."""
    if models:
        options = [(m, m) for m in models]
        if current not in models:
            options.insert(0, (f"{current} (current, not installed)", current))
        return Field("model", "Model", kind="choices", options=tuple(options[:MAX_OPTIONS]), defaults=(current,),
                     description="the models Ollama has installed")
    return Field("model", "Model name", default=current, placeholder="e.g. dolphin3:8b", max_length=100,
                 description="Ollama didn't answer, so type the name")


def _ai(cfg: dict, ctx, models: list[str] | None, *, bot_admin: bool, bot) -> list[Item]:
    if not bot_admin:
        return []
    items = []
    for key, label, method in (("ask_model", "🤖 Ask model — !ask and @mentions", "cmd_model"),
                               ("roleplay_model", "🎭 Roleplay model — !roleplay / !rpg", "cmd_roleplaymodel"),
                               ("coding_model", "🧩 Coding model — AI puzzles", "cmd_codingmodel")):
        current = cfg.get(key, OLLAMA_MODEL)
        items.append(Item(key=f"model:{key}", label=label, value=current, method=method, title=label.split(" — ")[0],
                          kind="form", fields=(_model_field(current, models),),
                          to_args=lambda v: (v["model"][0] if isinstance(v["model"], list) else v["model"],)))
    prompt = state.channel_prompts.get(ctx.channel.id)
    items.append(Item(key="prompt", label="📝 This channel's system prompt", value=(prompt[:DESCRIPTION_MAX] if prompt else "default"),
                      method="cmd_setprompt", title="This channel's system prompt", kind="form",
                      fields=(Field("prompt", "System prompt", kind="paragraph", required=False, default=prompt,
                                    placeholder="Leave empty for the default prompt", max_length=2000),),
                      to_args=lambda v: (v["prompt"],)))
    vram = state.bot_settings.get("vram_text", "16GB")
    items.append(Item(key="vramtext", label="💾 vRAM text shown in !stats (global)", value=vram,
                      method="cmd_vramtext", title="vRAM text", kind="form",
                      fields=(Field("text", "Text", default=vram, max_length=100),),
                      to_args=lambda v: (v["text"],)))
    passive = state.bot_settings.get("ai_enabled", True)
    if bot is not None and bot.get_command("ai on") is not None:
        items.append(Item(key="ai-passive", label="🤖 Passive AI replies (global) — @mentions and AI channels", value=_on(passive),
                          method="bot:ai off" if passive else "bot:ai on"))
    return items


def items_for(category: str, ctx, *, models: list[str] | None = None, bot=None) -> list[Item]:
    """What the setting dropdown offers in `category`, with current values."""
    gid = ctx.guild.id
    cfg = get_guild_cfg(gid)
    bot_admin = is_admin(ctx)
    if category == "features":
        return _features(gid)
    if category == "channels":
        return _channels(cfg, with_global=bot_admin)
    if category == "shop":
        return _shop(cfg)
    if category == "games":
        return _games(cfg)
    if category == "aliases":
        return _aliases(cfg, ctx.guild)
    if category == "ai":
        return _ai(cfg, ctx, models, bot_admin=bot_admin, bot=bot)
    return []


# ── the view ─────────────────────────────────────────────────────────────────

def _describe(reply) -> str:
    if isinstance(reply, discord.Embed):
        text = f"**{reply.title}**" if reply.title else ""
        if reply.description:
            text += ("\n" if text else "") + reply.description
        return text[:1024]
    return str(reply)[:1024] if reply else ""


class _CategorySelect(ui.Select):
    def __init__(self, current: str):
        super().__init__(
            placeholder="Category…", min_values=1, max_values=1, row=0,
            options=[discord.SelectOption(label=label, value=key, default=key == current) for key, label in CATEGORIES],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.view.show(interaction, self.values[0])


class _ItemSelect(ui.Select):
    def __init__(self, items: list[Item]):
        self.items = {item.key: item for item in items[:MAX_OPTIONS]}
        super().__init__(
            placeholder="Change a setting…", min_values=1, max_values=1, row=1,
            options=[discord.SelectOption(label=item.label[:100], value=item.key, description=item.value[:DESCRIPTION_MAX] or None)
                     for item in self.items.values()],
        )

    async def callback(self, interaction: discord.Interaction):
        hub: SettingsHub = self.view  # type: ignore[assignment]
        item = self.items[self.values[0]]
        if item.is_form:
            async def _submitted(submit: discord.Interaction, values: dict):
                await hub.apply(submit, item, item.to_args(values))
            # A modal is the only reply a pick can open, so no defer first.
            await interaction.response.send_modal(FormModal(item.title or item.label, item.fields, on_submit=_submitted))
            return
        await hub.apply(interaction, item, item.args)


class _CloseButton(ui.Button):
    def __init__(self):
        super().__init__(label="Close", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction):
        await self.view.finish(interaction, True, "closed")


class SettingsHub(_OwnedView):
    """Invoker-only. `overview` renders the read-only overview embed for the
    first category; the others list their items and take picks."""

    def __init__(self, cog, ctx, category: str, overview, *, timeout: float = HUB_TIMEOUT):
        super().__init__(ctx.author.id, timeout)
        self.cog, self.ctx, self.category, self.overview = cog, ctx, category, overview
        self.models: list[str] | None = None  # installed Ollama models, fetched on the first AI visit
        self.last = None                        # the latest forwarded command's reply
        self._build()

    def items(self) -> list[Item]:
        return items_for(self.category, self.ctx, models=self.models, bot=getattr(self.cog, "bot", None))

    def _build(self) -> None:
        self.clear_items()
        self.add_item(_CategorySelect(self.category))
        items = self.items()
        if items:
            self.add_item(_ItemSelect(items))
        self.add_item(_CloseButton())

    def embed(self) -> discord.Embed:
        if self.category == "overview":
            embed = self.overview(self.ctx)
        else:
            lines = [f"{item.label.split(' — ')[0]}: {item.value}" + (f" — *{item.note}*" if item.note else "")
                     for item in self.items()]
            embed = emb(f"⚙️ Settings — {_CATEGORY_LABELS[self.category]}", "\n".join(lines) or "Nothing here for you.", C_BLUE)
            if self.category == "ai" and not lines:
                embed.description = "The AI settings (models, prompts) are bot-admin only."
        if self.last:
            embed.add_field(name="Last change", value=_describe(self.last) or "—", inline=False)
        embed.set_footer(text="Pick a category, then a setting. Toggles apply at once; the rest open a form.")
        return embed

    async def show(self, interaction: discord.Interaction, category: str) -> None:
        self.category = category
        if category == "ai" and self.models is None:
            self.models = await list_ollama_models()
        await self.refresh(interaction)

    async def apply(self, interaction: discord.Interaction, item: Item, args: tuple) -> None:
        """Run the item's command in its typed form and show its reply."""
        if not interaction.response.is_done():
            await interaction.response.defer()
        if item.kind == "action":
            # The setup wizard waits on answers for up to WIZARD_TIMEOUT —
            # longer than an interaction token lives — so it runs on its own
            # and the panel comes back at once.
            asyncio.ensure_future(self.cog.run_captured(self.ctx, item.method, *args))
            self.last = emb("🧭 Setup", "The setup questions are posted in this channel — any admin can answer them.", C_BLUE)
        else:
            self.last = await self.cog.run_captured(self.ctx, item.method, *args)
        await self.refresh(interaction)

    async def refresh(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.edit_original_response(embed=self.embed(), view=self)


async def open_settings_hub(ctx, cog, *, category: str = "overview", overview, ephemeral: bool = False) -> None:
    """Post the panel and wait it out. The message is deleted when it
    closes — settings are read once, and the panel lists everything."""
    hub = SettingsHub(cog, ctx, category, overview)
    kwargs = {"ephemeral": True} if ephemeral else {}
    msg = await ctx.send(embed=hub.embed(), view=hub, **kwargs)
    await hub.wait()
    try:
        await msg.delete()
    except discord.HTTPException:
        try:
            await msg.edit(embed=emb("⚙️ Settings — closed", "Run `!settings` to open the panel again.", C_GREY), view=None)
        except discord.HTTPException:
            pass  # cosmetic — every change was saved as it was made


_CHANNEL_TOKEN = re.compile(r"<#(\d+)>|(\d{15,21})")


def channels_in(ctx, args) -> list:
    """The channels a settings command names: #mentions in the message, plus
    `<#id>` / bare ids typed as arguments — which is how the panel forwards
    a picker's choice. Unknown ids are skipped."""
    found = list(ctx.message.channel_mentions)
    seen = {c.id for c in found}
    for token in args:
        m = _CHANNEL_TOKEN.fullmatch(str(token))
        if not m:
            continue
        cid = int(m.group(1) or m.group(2))
        channel = ctx.guild.get_channel(cid) if ctx.guild is not None else None
        if channel is not None and cid not in seen:
            found.append(channel)
            seen.add(cid)
    return found
