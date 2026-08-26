"""!assets — the real-estate property system.

Browse the catalog, buy unique bot-wide deeds from the bank, list them for
sale at any price on the global marketplace (cross-server: a deed listed in
one guild can be bought from any other), and earn daily revenue that is
banked automatically with the owner's daily claim (see src/properties.py
and the property-revenue hook in events._auto_daily).

Race safety: buying claims the deed in state.property_owners synchronously
BEFORE the charge await and rolls back on failure — two concurrent buyers
of one unique deed is exactly the race CLAUDE.md warns about.
"""
import logging
import time

import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_PURPLE,
    parse_amount, send_ephemeral, shop_charge,
    announce_record,
)
from src.economy import add_balance, add_guild_house, _ensure_user
from src.properties import (
    PROPERTIES, PROPERTY_MAX_OWNED, PROPERTY_SALE_FEE_PCT,
    find_property, daily_revenue, owned_properties, owned_property_count,
    portfolio_value, portfolio_daily_revenue, accrual_cap,
    pending_property_revenue,
)
from src.persistence import save_property_owner, try_set_record
from src import state


def _owner_row(pid: str) -> dict | None:
    return state.property_owners.get(pid)


def _fmt_prop(p: dict) -> str:
    return f"{p['emoji']} **{p['name']}**"


# Shown on both !assets portfolio states so the subcommands are always
# discoverable from the bare command.
_SUBCOMMANDS_HELP = (
    "**Subcommands:**\n"
    "`!assets browse [tier]` — full catalog + player listings (cross-server)\n"
    "`!assets buy <name>` — buy from the bank or a player listing\n"
    "`!assets sell <name> <price>` — list on the cross-server market\n"
    "`!assets unlist <name>` — remove your listing\n"
    "`!assets @user` — someone else's portfolio"
)


class AssetsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── !assets (group) ───────────────────────────────────────────────────
    @commands.group(
        name="assets",
        aliases=["property", "properties", "asset", "realestate", "investment", "investments"],
        invoke_without_command=True,
    )
    async def cmd_assets(self, ctx: commands.Context, target: discord.Member = None):
        """Portfolio view: own by default, someone else's via !assets @user."""
        member = target or ctx.author
        await _ensure_user(member.id)
        await self._send_portfolio(ctx, member)

    async def _send_portfolio(self, ctx: commands.Context, member: discord.Member):
        uid = member.id
        props = owned_properties(uid)
        if not props:
            desc = (
                f"**{member.display_name}** doesn't own any properties yet.\n\n"
                f"{_SUBCOMMANDS_HELP}\n\n"
                f"*Properties pay **2× their price per year**, banked automatically "
                f"with your daily claim. Own up to **{PROPERTY_MAX_OWNED}**.*"
            )
            await send_ephemeral(ctx, embed=emb("🏘️ Real Estate", desc, C_PURPLE))
            return

        lines = []
        for p in props:
            row = _owner_row(p["id"])
            listed = f" • 🏷️ listed at **{row['list_price']:,} 🪙**" if row.get("list_price") else ""
            lines.append(
                f"{_fmt_prop(p)} — {p['cost']:,} 🪙 • {daily_revenue(p['cost']):,} 🪙/day{listed}"
            )
        pending = pending_property_revenue(uid)
        cap = accrual_cap(uid)
        banked = int(state.economy["users"].get(str(uid), {}).get("property_revenue_total", 0) or 0)
        lines.append("")
        lines.append(f"**Portfolio value:** {portfolio_value(uid):,} 🪙 ({len(props)}/{PROPERTY_MAX_OWNED} properties)")
        lines.append(f"**Daily revenue:** {portfolio_daily_revenue(uid):,} 🪙/day")
        lines.append(f"**Unredeemed revenue:** {pending:,} / {cap:,} 🪙 — banked with your daily claim")
        lines.append(f"**Revenue banked (lifetime):** {banked:,} 🪙")
        lines.append("")
        lines.append(_SUBCOMMANDS_HELP)
        await send_ephemeral(ctx, embed=emb(f"🏘️ {member.display_name}'s Real Estate", "\n".join(lines), C_PURPLE))

    # ── !assets browse / market (combined catalog + listings view) ────────
    @cmd_assets.command(name="browse", aliases=["market", "catalog", "shop", "listings", "forsale"])
    async def assets_browse(self, ctx: commands.Context, tier: int = None):
        from src.level_unlocks import user_display_level
        uid = ctx.author.id
        gid = ctx.guild.id if ctx.guild else 0
        lvl = user_display_level(uid, gid)

        lines = []
        cur_tier = None
        for p in PROPERTIES:
            if tier is not None and p["tier"] != tier:
                continue
            if p["tier"] != cur_tier:
                cur_tier = p["tier"]
                if lines:
                    lines.append("")
                lines.append(f"**Tier {cur_tier}** *(level {p['level']}+)*")
            row = _owner_row(p["id"])
            rev = daily_revenue(p["cost"])
            if row is None:
                entry = f"{_fmt_prop(p)} — **{p['cost']:,} 🪙** • {rev:,} 🪙/day"
                if lvl < p["level"]:
                    entry = f"~~{entry}~~ 🔒"
            elif row["owner_id"] == uid:
                entry = f"{_fmt_prop(p)} — ✅ **Yours** • {rev:,} 🪙/day"
            elif row.get("list_price"):
                entry = (
                    f"{_fmt_prop(p)} — 🏷️ **{row['list_price']:,} 🪙** • {rev:,} 🪙/day"
                    f" • seller: <@{row['owner_id']}>"
                )
            else:
                entry = f"{_fmt_prop(p)} — 🔒 owned"
            lines.append(entry)
        if not lines:
            await ctx.send(embed=emb("🏘️ Real Estate", f"No tier `{tier}` — tiers are 1–5.", C_RED))
            return
        lines.append("")
        lines.append(
            f"Every property is unique — one owner across all servers; 🏷️ marks "
            f"player listings, buyable from any server. "
            f"Own up to **{PROPERTY_MAX_OWNED}**; revenue is 2× price per year, "
            "banked with your daily claim.\n"
            "`!assets buy <name>` • `!assets sell <name> <price>`"
        )
        await send_ephemeral(ctx, embed=emb("🏘️ Real Estate — Catalog & Market", "\n".join(lines), C_PURPLE))

    # ── !assets buy ───────────────────────────────────────────────────────
    @cmd_assets.command(name="buy", aliases=["purchase"])
    async def assets_buy(self, ctx: commands.Context, *, name: str = None):
        from src.level_unlocks import user_display_level
        if not name:
            await ctx.send(embed=emb("🏘️ Real Estate", "Usage: `!assets buy <property name>` — see `!assets browse`.", C_PURPLE))
            return
        prop = find_property(name)
        if prop is None:
            await ctx.send(embed=emb("❌ Unknown Property", f"No property called `{name}` — see `!assets browse`.", C_RED))
            return
        uid = ctx.author.id
        await _ensure_user(uid)
        gid = ctx.guild.id if ctx.guild else 0
        lvl = user_display_level(uid, gid)
        if lvl < prop["level"]:
            await ctx.send(embed=emb("🔒 Level Locked", f"{_fmt_prop(prop)} unlocks at **Level {prop['level']}** — you're Level {lvl}.", C_RED))
            return

        pid = prop["id"]
        row = _owner_row(pid)
        if row is not None and row["owner_id"] == uid:
            await ctx.send(embed=emb("🏘️ Already Yours", f"You already own {_fmt_prop(prop)}.", C_PURPLE))
            return
        if row is not None and not row.get("list_price"):
            await ctx.send(embed=emb("🔒 Not For Sale", f"{_fmt_prop(prop)} is owned and not listed for sale.", C_RED))
            return
        # Gate-and-claim: the ownership-cap check and the deed claim run
        # synchronously before any await, so a concurrent second buy (same
        # user or another user racing for the same deed) sees the claim and
        # bails instead of double-selling a unique property.
        if owned_property_count(uid) >= PROPERTY_MAX_OWNED:
            await ctx.send(embed=emb(
                "🏘️ Portfolio Full",
                f"You already own **{PROPERTY_MAX_OWNED}** properties — sell one first (`!assets sell <name> <price>`).",
                C_RED,
            ))
            return

        now = time.time()
        if row is None:
            await self._buy_from_bank(ctx, prop, now)
        else:
            await self._buy_from_market(ctx, prop, row, now)

    async def _buy_from_bank(self, ctx, prop: dict, now: float):
        uid = ctx.author.id
        pid = prop["id"]
        cost = prop["cost"]
        # Claim the deed synchronously, then charge; roll back on failure.
        state.property_owners[pid] = {
            "owner_id": uid, "acquired_at": now, "list_price": None, "listed_at": None,
        }
        if not await shop_charge(ctx, uid, cost):
            state.property_owners.pop(pid, None)
            return
        await save_property_owner(pid, uid, now)
        await ctx.send(embed=emb(
            "🏘️ Property Acquired",
            f"**{ctx.author.display_name}** bought {_fmt_prop(prop)} for **{cost:,} 🪙**!\n"
            f"It earns **{daily_revenue(cost):,} 🪙/day**, banked with your daily claim.",
            C_GREEN,
        ))
        await self._offer_records(ctx)

    async def _buy_from_market(self, ctx, prop: dict, row: dict, now: float):
        uid = ctx.author.id
        pid = prop["id"]
        price = int(row["list_price"])
        seller_id = int(row["owner_id"])
        prior = dict(row)
        # Claim the deed synchronously (transfer + delist), then charge the
        # buyer; restore the seller's row if the charge fails.
        row.update(owner_id=uid, acquired_at=now, list_price=None, listed_at=None)
        if not await shop_charge(ctx, uid, price, cost_label=f"{price:,}"):
            row.update(prior)
            return
        fee = price * PROPERTY_SALE_FEE_PCT // 100
        payout = price - fee
        await add_balance(seller_id, payout)
        if ctx.guild and fee:
            await add_guild_house(ctx.guild.id, fee)
        await save_property_owner(pid, uid, now)
        await ctx.send(embed=emb(
            "🏘️ Property Acquired",
            f"**{ctx.author.display_name}** bought {_fmt_prop(prop)} from <@{seller_id}> "
            f"for **{price:,} 🪙**!\n"
            f"It earns **{daily_revenue(prop['cost']):,} 🪙/day**, banked with your daily claim.",
            C_GREEN,
        ))
        # Cross-server sale: the seller may not be in this guild, so tell
        # them by DM. Best-effort — a closed-DM seller still gets the coins.
        try:
            seller = self.bot.get_user(seller_id) or await self.bot.fetch_user(seller_id)
            await seller.send(embed=emb(
                "🏷️ Property Sold",
                f"Your {_fmt_prop(prop)} sold for **{price:,} 🪙** — you received "
                f"**{payout:,} 🪙** after the {PROPERTY_SALE_FEE_PCT}% market fee.",
                C_GOLD,
            ))
        except Exception:
            logging.info("[assets] could not DM seller %s about sale of %s", seller_id, pid)
        await self._offer_records(ctx)

    async def _offer_records(self, ctx):
        """Offer the buyer's new totals to the property records (guild only)."""
        if ctx.guild is None:
            return
        uid = ctx.author.id
        count = owned_property_count(uid)
        if await try_set_record(ctx.guild.id, "total_assets", count, uid, ctx.author.display_name):
            await announce_record(ctx.channel, "total_assets", ctx.author.display_name, count, holder_id=uid)
        value = portfolio_value(uid)
        if await try_set_record(ctx.guild.id, "highest_property_value", value, uid, ctx.author.display_name):
            await announce_record(ctx.channel, "highest_property_value", ctx.author.display_name, value, holder_id=uid)

    # ── !assets sell / unlist ─────────────────────────────────────────────
    @cmd_assets.command(name="sell", aliases=["list"])
    async def assets_sell(self, ctx: commands.Context, *args):
        if len(args) < 2:
            await ctx.send(embed=emb(
                "🏷️ Sell a Property",
                "Usage: `!assets sell <property name> <price>` — lists it on the "
                "cross-server market. `!assets unlist <name>` removes the listing.",
                C_PURPLE,
            ))
            return
        name, price_str = " ".join(args[:-1]), args[-1]
        prop = find_property(name)
        if prop is None:
            await ctx.send(embed=emb("❌ Unknown Property", f"No property called `{name}` — see `!assets browse`.", C_RED))
            return
        price = await parse_amount(ctx, price_str)
        if price is None:
            return
        if price <= 0:
            await ctx.send(embed=emb("❌ Invalid Price", "Price must be positive.", C_RED))
            return
        uid = ctx.author.id
        row = _owner_row(prop["id"])
        if row is None or row["owner_id"] != uid:
            await ctx.send(embed=emb("❌ Not Yours", f"You don't own {_fmt_prop(prop)}.", C_RED))
            return
        now = time.time()
        prior = dict(row)
        row["list_price"] = int(price)
        row["listed_at"] = now
        try:
            await save_property_owner(prop["id"], uid, row["acquired_at"], list_price=int(price), listed_at=now)
        except Exception:
            row.update(prior)
            raise
        fee = int(price) * PROPERTY_SALE_FEE_PCT // 100
        await ctx.send(embed=emb(
            "🏷️ Listed For Sale",
            f"{_fmt_prop(prop)} is now listed at **{price:,} 🪙** on the cross-server market.\n"
            f"You'll receive **{price - fee:,} 🪙** after the {PROPERTY_SALE_FEE_PCT}% market fee. "
            "It keeps earning revenue for you until it sells.",
            C_GREEN,
        ))

    @cmd_assets.command(name="unlist", aliases=["delist"])
    async def assets_unlist(self, ctx: commands.Context, *, name: str = None):
        if not name:
            await ctx.send(embed=emb("🏷️ Unlist", "Usage: `!assets unlist <property name>`", C_PURPLE))
            return
        prop = find_property(name)
        if prop is None:
            await ctx.send(embed=emb("❌ Unknown Property", f"No property called `{name}` — see `!assets browse`.", C_RED))
            return
        uid = ctx.author.id
        row = _owner_row(prop["id"])
        if row is None or row["owner_id"] != uid:
            await ctx.send(embed=emb("❌ Not Yours", f"You don't own {_fmt_prop(prop)}.", C_RED))
            return
        if not row.get("list_price"):
            await ctx.send(embed=emb("🏷️ Not Listed", f"{_fmt_prop(prop)} isn't listed for sale.", C_PURPLE))
            return
        prior = dict(row)
        row["list_price"] = None
        row["listed_at"] = None
        try:
            await save_property_owner(prop["id"], uid, row["acquired_at"])
        except Exception:
            row.update(prior)
            raise
        await ctx.send(embed=emb("🏷️ Unlisted", f"{_fmt_prop(prop)} is no longer for sale.", C_GREEN))


async def setup(bot):
    await bot.add_cog(AssetsCog(bot))
