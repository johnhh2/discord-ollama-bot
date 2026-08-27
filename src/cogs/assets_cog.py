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
    PROPERTY_UPGRADES, PROPERTY_BANK_BUYBACK_PCT,
    find_property, owned_properties, owned_property_count,
    portfolio_value, portfolio_daily_revenue, accrual_cap, PROPERTY_DAILY_REVENUE_PCT,
    pending_property_revenue, property_value, property_daily_revenue,
    bank_buyback_offer,
)
from src.confirm_view import confirm_prompt
from src.persistence import save_property_owner, delete_property_owner, try_set_record
from src import state


def _owner_row(pid: str) -> dict | None:
    return state.property_owners.get(pid)


def _fmt_prop(p: dict, row: dict = None) -> str:
    """Display label for a property. A renamed business shows its custom
    name with the catalog name in parentheses so it stays recognizable."""
    if row is None:
        row = state.property_owners.get(p["id"])
    custom = row.get("custom_name") if row else None
    if custom:
        return f"{p['emoji']} **{custom}** ({p['name']})"
    return f"{p['emoji']} **{p['name']}**"


# Shown on both !assets portfolio states so the subcommands are always
# discoverable from the bare command.
_SUBCOMMANDS_HELP = (
    "**Subcommands:**\n"
    "`!assets browse [tier]` — full catalog + player listings (cross-server)\n"
    "`!assets buy <name>` — buy from the bank or a player listing\n"
    "`!assets upgrade [name]` — see your properties' upgrades, or buy one\n"
    "`!assets sell <name> <price>` — list on the cross-server market "
    f"(at ≤{PROPERTY_BANK_BUYBACK_PCT}% of value, the bank offers an instant buyback)\n"
    "`!assets unlist <name>` — remove your listing\n"
    "`!assets rename <name> <new name>` — rename your business\n"
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
                f"*Properties pay **{PROPERTY_DAILY_REVENUE_PCT} of their price per day**, banked automatically "
                f"with your daily claim. Own up to **{PROPERTY_MAX_OWNED}**.*"
            )
            await send_ephemeral(ctx, embed=emb("🏘️ Real Estate", desc, C_PURPLE))
            return

        lines = []
        for p in props:
            pid = p["id"]
            row = _owner_row(pid)
            listed = f" • 🏷️ listed at **{row['list_price']:,} 🪙**" if row.get("list_price") else ""
            # Every property has exactly one upgrade — (1/1) means bought.
            # Upgrade names/costs live in `!assets upgrade`, not here.
            up_count = f"({1 if row.get('upgraded') else 0}/1)"
            lines.append(
                f"{_fmt_prop(p, row)} {up_count} — {property_value(pid, row):,} 🪙 • "
                f"{property_daily_revenue(pid, row):,} 🪙/day{listed}"
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
            rev = property_daily_revenue(p["id"], row)
            star = " ⭐" if row and row.get("upgraded") else ""
            if row is None:
                entry = f"{_fmt_prop(p)} — **{p['cost']:,} 🪙** • {rev:,} 🪙/day"
                if lvl < p["level"]:
                    entry = f"~~{entry}~~ 🔒"
            elif row["owner_id"] == uid:
                entry = f"{_fmt_prop(p, row)} — ✅ **Yours**{star} • {rev:,} 🪙/day"
            elif row.get("list_price"):
                entry = (
                    f"{_fmt_prop(p, row)} — 🏷️ **{row['list_price']:,} 🪙**{star} • {rev:,} 🪙/day"
                    f" • seller: <@{row['owner_id']}>"
                )
            else:
                entry = f"{_fmt_prop(p, row)} — 🔒 owned{star}"
            lines.append(entry)
        if not lines:
            await ctx.send(embed=emb("🏘️ Real Estate", f"No tier `{tier}` — tiers are 1–5.", C_RED))
            return
        lines.append("")
        lines.append(
            f"Every property is unique — one owner across all servers; 🏷️ marks "
            f"player listings, buyable from any server. "
            f"Own up to **{PROPERTY_MAX_OWNED}**; revenue is {PROPERTY_DAILY_REVENUE_PCT} of price per day, "
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
        row = {
            "owner_id": uid, "acquired_at": now, "list_price": None, "listed_at": None,
            "upgraded": False, "custom_name": None,
        }
        state.property_owners[pid] = row
        if not await shop_charge(ctx, uid, cost):
            state.property_owners.pop(pid, None)
            return
        await save_property_owner(pid, row)
        await ctx.send(embed=emb(
            "🏘️ Property Acquired",
            f"**{ctx.author.display_name}** bought {_fmt_prop(prop)} for **{cost:,} 🪙**!\n"
            f"It earns **{property_daily_revenue(pid, row):,} 🪙/day**, banked with your daily claim.",
            C_GREEN,
        ))
        await self._offer_records(ctx)

    async def _buy_from_market(self, ctx, prop: dict, row: dict, now: float):
        uid = ctx.author.id
        pid = prop["id"]
        price = int(row["list_price"])
        seller_id = int(row["owner_id"])
        prior = dict(row)
        # Claim the deed synchronously (transfer + delist; the upgrade and
        # custom name travel with the business), then charge the buyer;
        # restore the seller's row if the charge fails.
        row.update(owner_id=uid, acquired_at=now, list_price=None, listed_at=None)
        if not await shop_charge(ctx, uid, price, cost_label=f"{price:,}"):
            row.update(prior)
            return
        fee = price * PROPERTY_SALE_FEE_PCT // 100
        payout = price - fee
        await add_balance(seller_id, payout)
        if ctx.guild and fee:
            await add_guild_house(ctx.guild.id, fee)
        await save_property_owner(pid, row)
        upgraded_note = " (⭐ upgraded)" if row.get("upgraded") else ""
        await ctx.send(embed=emb(
            "🏘️ Property Acquired",
            f"**{ctx.author.display_name}** bought {_fmt_prop(prop, row)}{upgraded_note} "
            f"from <@{seller_id}> for **{price:,} 🪙**!\n"
            f"It earns **{property_daily_revenue(pid, row):,} 🪙/day**, banked with your daily claim.",
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
        pid = prop["id"]
        row = _owner_row(pid)
        if row is None or row["owner_id"] != uid:
            await ctx.send(embed=emb("❌ Not Yours", f"You don't own {_fmt_prop(prop)}.", C_RED))
            return

        # A lowball listing (≤75% of the property's value, upgrade included)
        # triggers an instant bank buyback offer at 75% of value — a
        # guaranteed exit with no market fee. Declining lists as normal.
        value = property_value(pid, row)
        offer = bank_buyback_offer(pid, row)
        if int(price) <= value * PROPERTY_BANK_BUYBACK_PCT // 100:
            accepted = await confirm_prompt(
                ctx,
                title="🏦 Bank Offer",
                description=(
                    f"You're listing {_fmt_prop(prop, row)} at **{price:,} 🪙** — at or below "
                    f"**{PROPERTY_BANK_BUYBACK_PCT}%** of its **{value:,} 🪙** value.\n"
                    f"The bank offers **{offer:,} 🪙** ({PROPERTY_BANK_BUYBACK_PCT}% of value) "
                    "to buy it back instantly, no market fee.\n\n"
                    "**Confirm** to sell to the bank now • **Cancel** to list on the market instead."
                ),
                payer=ctx.author,
            )
            if accepted:
                # The confirm wait is a long await — re-validate the deed is
                # still ours and unsold, then claim synchronously.
                row = _owner_row(pid)
                if row is None or row["owner_id"] != uid:
                    await ctx.send(embed=emb("❌ Sale Failed", f"You no longer own {_fmt_prop(prop)}.", C_RED))
                    return
                state.property_owners.pop(pid, None)     # claim, sync
                try:
                    await add_balance(uid, offer)
                    await delete_property_owner(pid)
                except Exception:
                    state.property_owners[pid] = row     # roll back on failure
                    raise
                await ctx.send(embed=emb(
                    "🏦 Sold to the Bank",
                    f"The bank bought {_fmt_prop(prop, row)} for **{offer:,} 🪙**. "
                    "It's back on the open market (`!assets browse`).",
                    C_GREEN,
                ))
                return
            # Declined/timed out — fall through to the normal listing.

        now = time.time()
        prior = dict(row)
        row["list_price"] = int(price)
        row["listed_at"] = now
        try:
            await save_property_owner(pid, row)
        except Exception:
            row.update(prior)
            raise
        fee = int(price) * PROPERTY_SALE_FEE_PCT // 100
        await ctx.send(embed=emb(
            "🏷️ Listed For Sale",
            f"{_fmt_prop(prop, row)} is now listed at **{price:,} 🪙** on the cross-server market.\n"
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
            await save_property_owner(prop["id"], row)
        except Exception:
            row.update(prior)
            raise
        await ctx.send(embed=emb("🏷️ Unlisted", f"{_fmt_prop(prop, row)} is no longer for sale.", C_GREEN))

    # ── !assets upgrade ───────────────────────────────────────────────────
    @cmd_assets.command(name="upgrade", aliases=["upgrades"])
    async def assets_upgrade(self, ctx: commands.Context, *, name: str = None):
        uid = ctx.author.id
        if not name:
            props = owned_properties(uid)
            if not props:
                await ctx.send(embed=emb(
                    "⭐ Property Upgrades",
                    "You don't own any properties yet — see `!assets browse`.\n"
                    "Usage: `!assets upgrade <property name>`",
                    C_PURPLE,
                ))
                return
            lines = []
            for p in props:
                row = _owner_row(p["id"])
                up_name, up_cost, up_boost = PROPERTY_UPGRADES[p["id"]]
                if row.get("upgraded"):
                    lines.append(f"{_fmt_prop(p, row)} — ⭐ **{up_name}** owned (+{up_boost}% revenue)")
                else:
                    lines.append(f"{_fmt_prop(p, row)} — **{up_name}**: {up_cost:,} 🪙 for +{up_boost}% revenue")
            lines.append("")
            lines.append("Each property has one upgrade; its cost adds to the property's value. `!assets upgrade <name>` to buy.")
            await send_ephemeral(ctx, embed=emb("⭐ Property Upgrades", "\n".join(lines), C_PURPLE))
            return

        prop = find_property(name)
        if prop is None:
            await ctx.send(embed=emb("❌ Unknown Property", f"No property called `{name}` — see `!assets browse`.", C_RED))
            return
        pid = prop["id"]
        row = _owner_row(pid)
        if row is None or row["owner_id"] != uid:
            await ctx.send(embed=emb("❌ Not Yours", f"You don't own {_fmt_prop(prop)}.", C_RED))
            return
        up_name, up_cost, up_boost = PROPERTY_UPGRADES[pid]
        if row.get("upgraded"):
            await ctx.send(embed=emb("⭐ Already Upgraded", f"{_fmt_prop(prop, row)} already has its **{up_name}**.", C_PURPLE))
            return
        # Gate-and-claim: mark upgraded synchronously before the charge so a
        # concurrent second !assets upgrade sees the claim and bails instead
        # of double-charging.
        row["upgraded"] = True
        if not await shop_charge(ctx, uid, up_cost):
            row["upgraded"] = False
            return
        await save_property_owner(pid, row)
        await ctx.send(embed=emb(
            "⭐ Upgrade Built",
            f"{_fmt_prop(prop, row)} now has a **{up_name}**!\n"
            f"Revenue: **{property_daily_revenue(pid, row):,} 🪙/day** (+{up_boost}%) • "
            f"Value: **{property_value(pid, row):,} 🪙**",
            C_GREEN,
        ))
        # The upgrade cost folds into portfolio value — offer the records.
        await self._offer_records(ctx)

    # ── !assets rename ────────────────────────────────────────────────────
    @cmd_assets.command(name="rename")
    async def assets_rename(self, ctx: commands.Context, *args):
        if len(args) < 2:
            await ctx.send(embed=emb(
                "✏️ Rename a Business",
                "Usage: `!assets rename <property name> <new name>` — e.g. "
                "`!assets rename Tattoo Parlor Inkwell Studio`.",
                C_PURPLE,
            ))
            return
        uid = ctx.author.id
        # Greedy prefix match: the longest leading token span that resolves
        # to a property the caller owns; the rest is the new name. Longest
        # first so a new name that echoes the old one can't split too early.
        prop = row = None
        new_name = ""
        for split in range(len(args) - 1, 0, -1):
            cand = find_property(" ".join(args[:split]))
            if cand is None:
                continue
            cand_row = _owner_row(cand["id"])
            if cand_row is not None and cand_row["owner_id"] == uid:
                prop, row = cand, cand_row
                new_name = " ".join(args[split:]).strip()
                break
        if prop is None:
            await ctx.send(embed=emb(
                "❌ Unknown Property",
                "Couldn't match a property you own at the start of that — "
                "usage: `!assets rename <property name> <new name>`.",
                C_RED,
            ))
            return
        if not 1 <= len(new_name) <= 48:
            await ctx.send(embed=emb("❌ Invalid Name", "The new name must be 1–48 characters.", C_RED))
            return
        # Keep names resolvable: a custom name may not collide with a catalog
        # name/id or another business's custom name (case-insensitive).
        t = new_name.lower()
        taken = {p["name"].lower() for p in PROPERTIES} | {p["id"].replace("_", " ") for p in PROPERTIES}
        for other_pid, other_row in state.property_owners.items():
            if other_pid != prop["id"] and other_row.get("custom_name"):
                taken.add(other_row["custom_name"].lower())
        if t in taken:
            await ctx.send(embed=emb("❌ Name Taken", f"`{new_name}` already names another business or property.", C_RED))
            return
        prior = row.get("custom_name")
        row["custom_name"] = new_name
        try:
            await save_property_owner(prop["id"], row)
        except Exception:
            row["custom_name"] = prior
            raise
        await ctx.send(embed=emb(
            "✏️ Business Renamed",
            f"{prop['emoji']} **{prior or prop['name']}** is now {_fmt_prop(prop, row)}.",
            C_GREEN,
        ))


async def setup(bot):
    await bot.add_cog(AssetsCog(bot))
