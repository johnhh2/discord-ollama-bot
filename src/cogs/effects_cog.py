"""!effects — view and (for admins) manage a user's active shop effects.

All shop effects are scoped per server: state dicts are keyed by a
(guild_id, user_id) tuple. This cog reads/writes those dicts and persists via
the same save_* helpers the shop uses.

  !effects [@user]            — show your (or @user's) active effects + remaining time
  !effects list              — (server admin) list every effect type
  !effects @user add <effect> [duration]   — (server admin) grant a duration-based effect
  !effects @user remove <effect>           — (server admin) clear an effect

Aliases: !state, !effect.

Admin add/remove is limited to the time-based effects (spellcheck, tax,
insurance); counter-based effects (mock, curse, ragebait) are view-only.
"""
import time

import discord
from discord.ext import commands

from src.helpers import (
    emb, C_PURPLE, C_RED, C_GREEN, C_GREY,
    MemberConverter, parse_duration, format_duration, _effect_expired,
)
from src.permissions import requires_perm, is_admin, is_server_admin
from src.persistence import (
    save_mock, save_curse, save_tax, save_spellcheck, save_ragebait, save_insurance,
)
from src.config import (
    SHOP_MOCK_MESSAGES, SHOP_CURSE_MESSAGES, SHOP_RAGEBAIT_MESSAGES,
)
from src import state


# Every effect type, with the state dict it lives in, its save fn, whether an
# admin may add/remove it (duration-based), and a short description for !effects list.
_EFFECTS = {
    "spellcheck": {
        "store": "active_spellchecks", "save": save_spellcheck, "admin_settable": True,
        "emoji": "📝", "desc": "AI corrects the target's messages",
    },
    "tax": {
        "store": "active_taxes", "save": save_tax, "admin_settable": True,
        "emoji": "💰", "desc": "Target pays coins per message to the master",
    },
    "insurance": {
        "store": "insurance", "save": save_insurance, "admin_settable": True,
        "emoji": "🛡️", "desc": "Protects the user from being targeted",
    },
    "mock": {
        "store": "active_mocks", "save": save_mock, "admin_settable": False,
        "emoji": "🎭", "desc": f"Mocks the target's next {SHOP_MOCK_MESSAGES} messages",
    },
    "curse": {
        "store": "active_curses", "save": save_curse, "admin_settable": False,
        "emoji": "🔮", "desc": f"Curses the target's next {SHOP_CURSE_MESSAGES} messages",
    },
    "ragebait": {
        "store": "active_ragebaits", "save": save_ragebait, "admin_settable": False,
        "emoji": "😡", "desc": f"AI ragebaits the target for {SHOP_RAGEBAIT_MESSAGES + 1} messages",
    },
}

# Effects whose save fn takes the (rebuilt) dict as an argument rather than
# reading state directly (curse/tax keep that older signature).
_SAVE_TAKES_DICT = {"curse", "tax"}


async def _persist(effect: str):
    """Call the right save_* helper for `effect`, handling the two save signatures."""
    spec = _EFFECTS[effect]
    if effect in _SAVE_TAKES_DICT:
        await spec["save"](getattr(state, spec["store"]))
    else:
        await spec["save"]()


def _describe_entry(effect: str, data: dict) -> str:
    """One-line status for an active effect, including remaining duration/count."""
    spec = _EFFECTS[effect]
    bits = [f"{spec['emoji']} **{effect}**"]
    # Time-based: show remaining time if an expiry is set (omit if permanent).
    exp = data.get("expires_at")
    if exp:
        remaining = exp - time.time()
        if remaining > 0:
            bits.append(f"— {format_duration(remaining)} left")
    # Counter-based: show remaining messages.
    if "remaining" in data and data.get("remaining") is not None and effect != "tax":
        bits.append(f"— {data['remaining']} message(s) left")
    return " ".join(bits)


class EffectsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _active_effects_for(self, guild_id: int, uid: int) -> "list[tuple[str, dict]]":
        """Return [(effect_name, data), ...] currently active on (guild_id, uid),
        pruning anything whose time-based expiry has passed."""
        out = []
        for name, spec in _EFFECTS.items():
            store = getattr(state, spec["store"])
            data = store.get((guild_id, uid))
            if data is None:
                continue
            if _effect_expired(data):
                continue
            out.append((name, data))
        return out

    @commands.command(name="effects", aliases=["state", "effect"])
    @requires_perm
    async def cmd_effects(self, ctx: commands.Context, *args):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "Effects only exist in servers.", C_RED))
            return

        # !effects list  — admin-only listing of available effect types.
        if len(args) == 1 and args[0].lower() == "list":
            if not (is_admin(ctx) or is_server_admin(ctx)):
                await ctx.send(embed=emb("❌ No Permission", "Only server admins can list effects.", C_RED))
                return
            await self._show_list(ctx)
            return

        # Resolve a leading @user (defaults to the author). Everything after it
        # is an optional admin subcommand: add <effect> [dur] | remove <effect>.
        target = ctx.author
        rest = list(args)
        if rest:
            try:
                target = await MemberConverter().convert(ctx, rest[0])
                rest = rest[1:]
            except commands.BadArgument:
                # First token isn't a user — only valid form left is none.
                target = ctx.author

        if rest:
            # Admin subcommand: add / remove.
            await self._handle_admin_action(ctx, target, rest)
            return

        await self._show_user(ctx, target)

    async def _show_user(self, ctx: commands.Context, target: discord.Member):
        active = self._active_effects_for(ctx.guild.id, target.id)
        if not active:
            who = "You have" if target.id == ctx.author.id else f"**{target.display_name}** has"
            await ctx.send(embed=emb("✨ Effects", f"{who} no active effects.", C_GREY))
            return
        lines = [_describe_entry(name, data) for name, data in active]
        title = "✨ Your Effects" if target.id == ctx.author.id else f"✨ {target.display_name}'s Effects"
        await ctx.send(embed=emb(title, "\n".join(lines), C_PURPLE))

    async def _show_list(self, ctx: commands.Context):
        lines = []
        for name, spec in _EFFECTS.items():
            tag = "settable" if spec["admin_settable"] else "view-only"
            lines.append(f"{spec['emoji']} **{name}** — {spec['desc']} *({tag})*")
        body = (
            "\n".join(lines)
            + "\n\n*Admins: `!effects @user add <effect> [1s|5m|2h|3d|1w|6mo|1y]` "
            "(no duration = permanent) · `!effects @user remove <effect>`*"
        )
        await ctx.send(embed=emb("✨ Available Effects", body, C_PURPLE))

    async def _handle_admin_action(self, ctx: commands.Context, target: discord.Member, rest: "list[str]"):
        if not (is_admin(ctx) or is_server_admin(ctx)):
            await ctx.send(embed=emb("❌ No Permission", "Only server admins can change effects.", C_RED))
            return

        action = rest[0].lower()
        if action not in ("add", "remove"):
            await ctx.send(embed=emb(
                "✨ Effects",
                "Usage: `!effects @user add <effect> [duration]` or `!effects @user remove <effect>`",
                C_PURPLE,
            ))
            return
        if len(rest) < 2:
            await ctx.send(embed=emb("✨ Effects", f"Which effect? `!effects @user {action} <effect>`", C_PURPLE))
            return

        effect = rest[1].lower()
        spec = _EFFECTS.get(effect)
        if spec is None:
            await ctx.send(embed=emb("❌ Unknown Effect", f"`{effect}` isn't a real effect. Try `!effects list`.", C_RED))
            return
        if not spec["admin_settable"]:
            await ctx.send(embed=emb(
                "❌ Not Settable",
                f"`{effect}` is counter-based and can't be set by duration. "
                "Admin add/remove only supports: spellcheck, tax, insurance.",
                C_RED,
            ))
            return
        if self.bot and self.bot.user and target.id == self.bot.user.id:
            await ctx.send(embed=emb("❌ Invalid Target", "You can't put effects on the bot.", C_RED))
            return

        gid = ctx.guild.id
        store = getattr(state, spec["store"])
        key = (gid, target.id)

        if action == "remove":
            if key not in store:
                await ctx.send(embed=emb("✨ Effects", f"**{target.display_name}** doesn't have `{effect}`.", C_GREY))
                return
            del store[key]
            await _persist(effect)
            await ctx.send(embed=emb("🗑️ Effect Removed", f"Removed `{effect}` from **{target.display_name}**.", C_GREEN))
            return

        # action == "add". Optional trailing duration; no duration = permanent.
        expires_at = None
        dur_label = "permanent"
        if len(rest) >= 3:
            secs = parse_duration(rest[2])
            if secs is None:
                await ctx.send(embed=emb(
                    "❌ Bad Duration",
                    f"Couldn't parse `{rest[2]}`. Use forms like `30s`, `5m`, `2h`, `3d`, `1w`, `6mo`, `1y`.",
                    C_RED,
                ))
                return
            expires_at = time.time() + secs
            dur_label = format_duration(secs)

        store[key] = self._build_effect_entry(effect, ctx, target, expires_at)
        await _persist(effect)
        exp_clause = "permanently" if expires_at is None else f"for **{dur_label}**"
        await ctx.send(embed=emb(
            f"{spec['emoji']} Effect Added",
            f"Gave `{effect}` to **{target.display_name}** {exp_clause}.",
            C_GREEN,
        ))

    def _build_effect_entry(self, effect: str, ctx, target, expires_at):
        """Construct the state dict for an admin-granted effect."""
        now = time.time()
        if effect == "insurance":
            return {
                "expires_at": expires_at if expires_at is not None else now + 10 * 365 * 86_400,
                "protected_from": ["ragebait", "mock", "nickname", "role", "steal", "tax", "spellcheck"],
            }
        if effect == "tax":
            # Admin grant: the admin becomes the master (receives the coins).
            return {
                "master": ctx.author.id, "type": "tax", "emoji": "💰",
                "channel_id": ctx.channel.id, "activated_at": now, "expires_at": expires_at,
            }
        if effect == "spellcheck":
            return {
                "started_by": ctx.author.id, "days": None,
                "channel_id": ctx.channel.id, "activated_at": now, "expires_at": expires_at,
            }
        raise ValueError(f"not admin-settable: {effect}")


async def setup(bot):
    await bot.add_cog(EffectsCog(bot))
