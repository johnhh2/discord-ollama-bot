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
from src.economy import add_balance, add_guild_house, _ensure_user, get_insurance_tier, get_insurance_expiry
from src.properties import (
    PROPERTIES, PROPERTIES_BY_ID, PROPERTY_SALE_FEE_PCT, max_owned, released_properties,
    PROPERTY_UPGRADES, PROPERTY_SECURITY_UPGRADES, SECURITY_MIN_TIER, PROPERTY_BANK_BUYBACK_PCT,
    UPGRADE_TYPES, live_upgrade_types, upgrade_types_for,
    find_property, owned_properties, owned_property_count,
    portfolio_value, portfolio_daily_revenue, accrual_cap,
    pending_property_revenue, property_value, property_daily_revenue,
    bank_buyback_offer,
)
from src.artifacts import property_upgrade_cost
from src.rate_curves import property_pct_text
from src.confirm_view import confirm_choice, confirm_prompt, confirm_purchase
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


def buyable_properties(uid: int, tier: int | None = None) -> list[tuple[str, str, str, str]]:
    """What the market dropdown offers: (pid, label, detail, name) for every
    bank-owned deed and player listing, in catalog order."""
    from src.level_unlocks import user_global_display_level
    lvl = user_global_display_level(uid)
    out = []
    for p in released_properties():
        if tier is not None and p["tier"] != tier:
            continue
        row = _owner_row(p["id"])
        if row is None:
            detail = f"{p['cost']:,} coins"
        elif row.get("list_price"):
            detail = f"Listed at {row['list_price']:,} coins"
        else:
            continue
        detail += f" • {property_daily_revenue(p['id'], row, current=True):,}/day • Tier {p['tier']}"
        if lvl < p["level"]:
            detail += f" • 🔒 level {p['level']}"
        out.append((p["id"], f"{p['emoji']} {p['name']}", detail, p["name"]))
    return out


def owned_rows(uid: int) -> list[tuple[str, str, str]]:
    """The manage dropdown: (pid, label, detail) for each deed `uid` owns."""
    out = []
    for p in owned_properties(uid):
        row = _owner_row(p["id"])
        detail = f"{property_value(p['id'], row):,} coins • {property_daily_revenue(p['id'], row, current=True):,}/day"
        if row.get("list_price"):
            detail += f" • listed at {row['list_price']:,}"
        out.append((p["id"], f"{p['emoji']} {row.get('custom_name') or p['name']}", detail))
    return out


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
        from src.assets_view import AssetsView
        member = target or ctx.author
        await _ensure_user(member.id)
        if member.id != ctx.author.id:
            await send_ephemeral(ctx, embed=self.portfolio_embed(member))
            return
        view = AssetsView(self, ctx.author, mode="home", home=True)
        view.message = await send_ephemeral(ctx, embed=self.portfolio_embed(member), view=view)

    @staticmethod
    def owner_row(pid: str) -> dict | None:
        return _owner_row(pid)

    def portfolio_embed(self, member) -> discord.Embed:
        uid = member.id
        props = owned_properties(uid)
        if not props:
            desc = (
                f"**{member.display_name}** doesn't own any properties yet.\n\n"
                f"*Properties pay **{property_pct_text()} of their price per day**, banked automatically "
                f"with your daily claim. Own up to **{max_owned()}**.*"
            )
            return emb("🏘️ Real Estate", desc, C_PURPLE)

        lines = []
        for p in props:
            pid = p["id"]
            row = _owner_row(pid)
            listed = f" • 🏷️ listed at **{row['list_price']:,} 🪙**" if row.get("list_price") else ""
            # One upgrade per type on sale — (n/n) means all bought.
            # Upgrade names/costs live in `!assets upgrade`, not here.
            kinds = upgrade_types_for(pid)
            built = sum(1 for k in kinds if row.get(self._UPGRADE_FLAGS[k]))
            up_count = f"({built}/{len(kinds)})"
            lines.append(
                f"{_fmt_prop(p, row)} {up_count} — {property_value(pid, row):,} 🪙 • "
                f"{property_daily_revenue(pid, row, current=True):,} 🪙/day{listed}"
            )
        pending = pending_property_revenue(uid)
        cap = accrual_cap(uid)
        banked = int(state.economy["users"].get(str(uid), {}).get("property_revenue_total", 0) or 0)
        lines.append("")
        lines.append(f"**Portfolio value:** {portfolio_value(uid):,} 🪙 ({len(props)}/{max_owned()} properties)")
        lines.append(f"**Daily revenue:** {portfolio_daily_revenue(uid):,} 🪙/day — one full day with every daily claim")
        lines.append(
            f"**Missed-day bank:** {pending:,} / {cap:,} 🪙 — a day's revenue per skipped claim, "
            "paid on top of your next one"
        )
        lines.append(f"**Revenue banked (lifetime):** {banked:,} 🪙")
        return emb(f"🏘️ {member.display_name}'s Real Estate", "\n".join(lines), C_PURPLE)

    def manage_embed(self, member, pid: str | None) -> discord.Embed:
        if not owned_properties(member.id):
            return emb("🛠️ Manage Properties", "You don't own any properties.", C_PURPLE)
        if pid is None or _owner_row(pid) is None or _owner_row(pid)["owner_id"] != member.id:
            return emb("🛠️ Manage Properties", "Pick one of your properties below.", C_PURPLE)
        prop = PROPERTIES_BY_ID[pid]
        row = _owner_row(pid)
        up_name, up_cost, up_boost = PROPERTY_UPGRADES[pid]
        upgrade = (f"⭐ **{up_name}** built (+{up_boost}% revenue)" if row.get("upgraded") else
                   f"**{up_name}**: {property_upgrade_cost(member.id, up_cost):,} 🪙 for +{up_boost}% revenue")
        listing = f"🏷️ Listed at **{row['list_price']:,} 🪙**" if row.get("list_price") else "Not listed"
        lines = [
            f"**Value:** {property_value(pid, row):,} 🪙",
            f"**Revenue:** {property_daily_revenue(pid, row, current=True):,} 🪙/day",
            f"**Revenue upgrade:** {upgrade}",
        ]
        if "security" in upgrade_types_for(pid):
            lines.append(f"**Security:** {self._upgrade_line(member.id, pid, row, 'security')[0]}")
        lines.append(f"**Market:** {listing}")
        return emb(f"🛠️ {_fmt_prop(prop, row)}", "\n".join(lines), C_PURPLE)

    # ── !assets browse / market (combined catalog + listings view) ────────
    @cmd_assets.command(name="browse", aliases=["market", "catalog", "shop", "listings", "forsale"],
                        help="Properties for sale from the bank and from players, optionally one tier (1-5)")
    async def assets_browse(self, ctx: commands.Context, tier: int = None):
        from src.assets_view import AssetsView
        embed = self.browse_embed(ctx.author.id, tier)
        if embed is None:
            await ctx.send(embed=emb("🏘️ Real Estate", f"No tier `{tier}` — tiers are 1–5.", C_RED))
            return
        view = AssetsView(self, ctx.author, mode="browse", tier=tier)
        view.message = await send_ephemeral(ctx, embed=embed, view=view)

    def browse_embed(self, uid: int, tier: int | None = None) -> discord.Embed | None:
        """The market list, or None for a tier with nothing in the catalog."""
        from src.level_unlocks import user_global_display_level
        # Deeds are bot-wide, so the gate is the global level too.
        lvl = user_global_display_level(uid)
        # Only what can be bought (bank-owned deeds and player listings), under
        # the catalog name: no owners, custom names or upgrade stars.
        tiers: dict[int, list[str]] = {}
        levels: dict[int, int] = {}
        for p in released_properties():
            if tier is not None and p["tier"] != tier:
                continue
            levels[p["tier"]] = p["level"]
            entries = tiers.setdefault(p["tier"], [])
            row = _owner_row(p["id"])
            label = f"{p['emoji']} **{p['name']}**"
            rev = property_daily_revenue(p["id"], row, current=True)
            if row is None:
                entry = f"{label} — **{p['cost']:,} 🪙** • {rev:,} 🪙/day • 1 available"
                if lvl < p["level"]:
                    entry = f"~~{entry}~~ 🔒"
            elif row.get("list_price"):
                entry = f"{label} — 🏷️ **{row['list_price']:,} 🪙** • {rev:,} 🪙/day • 1 available"
            else:
                continue
            entries.append(entry)
        lines = []
        for t, entries in tiers.items():
            if lines:
                lines.append("")
            lines.append(f"**Tier {t}** *(level {levels[t]}+)*")
            lines += entries
        if not tiers:
            return None
        footer = (
            f"\n\nEvery property is unique — one owner across all servers; 🏷️ marks "
            f"player listings, buyable from any server. "
            f"Own up to **{max_owned()}**; revenue is {property_pct_text()} of price per day, "
            "banked with your daily claim. Pick one below and press **Buy**."
        )
        # Discord caps a description at 4096; the released pool can outgrow it.
        cut = 0
        while len("\n".join(lines)) + len(footer) > 3900:
            lines.pop()
            cut += 1
        if cut:
            lines.append(f"*…and {cut} more lines. `!assets browse <tier>` shows one tier.*")
        return emb("🏘️ Real Estate — Catalog & Market", "\n".join(lines) + footer, C_PURPLE)

    # ── !assets buy ───────────────────────────────────────────────────────
    @cmd_assets.command(name="buy", aliases=["purchase"],
                        help="Buy a property from the bank or a player listing", usage="<name>")
    async def assets_buy(self, ctx: commands.Context, *, name: str = None):
        from src.assets_view import AssetsView
        from src.level_unlocks import user_global_display_level
        prop = find_property(name) if name else None
        if prop is None:
            # No name, or one that matched nothing: offer the market dropdown.
            notice = (emb("❌ Unknown Property", f"No property called `{name}`. Pick one below.", C_RED) if name
                      else emb("🏘️ Buy a Property", "Pick a property below and press **Buy**.", C_PURPLE))
            view = AssetsView(self, ctx.author, mode="browse", notice=notice)
            view.message = await ctx.send(embed=notice, view=view)
            return
        uid = ctx.author.id
        await _ensure_user(uid)
        lvl = user_global_display_level(uid)
        if lvl < prop["level"]:
            await ctx.send(embed=emb("🔒 Level Locked", f"{_fmt_prop(prop)} unlocks at **global Level {prop['level']}** (your highest level in any server) — you're Level {lvl}.", C_RED))
            return

        pid = prop["id"]
        row = _owner_row(pid)
        if row is not None and row["owner_id"] == uid:
            # Deeds are unique, so "a second one" is always this deed; the
            # refusal is dressed up as antitrust law rather than a game rule.
            await ctx.send(embed=emb(
                "⚖️ Monopoly Laws",
                f"You already own {_fmt_prop(prop)}. Antitrust regulators won't approve a second one: "
                "monopoly laws limit every owner to one of each property.",
                C_RED,
            ))
            return
        if row is not None and not row.get("list_price"):
            await ctx.send(embed=emb("🔒 Not For Sale", f"{_fmt_prop(prop)} is owned and not listed for sale.", C_RED))
            return
        # Early refusal only. The race-safe gate — cap re-check plus the
        # synchronous deed claim — runs after the confirm wait, in
        # _buy_from_bank / _buy_from_market.
        if owned_property_count(uid) >= max_owned():
            await ctx.send(embed=emb(
                "🏘️ Portfolio Full",
                f"You already own **{max_owned()}** properties — sell one first (`!assets sell <name> <price>`).",
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
        preview_row = {"upgraded": False, "secured": False}
        if not await confirm_purchase(
            ctx, title="🏘️ Buy Property",
            description=(
                f"Buy {_fmt_prop(prop)} from the bank.\n"
                f"It earns **{property_daily_revenue(pid, preview_row, current=True):,} 🪙/day**, banked with your daily claim."
            ),
            cost=cost, payer=ctx.author,
        ):
            return
        # The confirm wait is a long await — re-check the deed and the
        # ownership cap, since a concurrent buyer may have moved first.
        if _owner_row(pid) is not None:
            await ctx.send(embed=emb("🔒 Not For Sale", f"{_fmt_prop(prop)} was bought while you were confirming. You were not charged.", C_RED))
            return
        if owned_property_count(uid) >= max_owned():
            await ctx.send(embed=emb(
                "🏘️ Portfolio Full",
                f"You already own **{max_owned()}** properties — sell one first (`!assets sell <name> <price>`).",
                C_RED,
            ))
            return
        # Claim the deed synchronously, then charge; roll back on failure.
        row = {
            "owner_id": uid, "acquired_at": now, "list_price": None, "listed_at": None,
            "upgraded": False, "secured": False, "custom_name": None,
        }
        state.property_owners[pid] = row
        if not await shop_charge(ctx, uid, cost):
            state.property_owners.pop(pid, None)
            return
        await save_property_owner(pid, row)
        await ctx.send(embed=emb(
            "🏘️ Property Acquired",
            f"**{ctx.author.display_name}** bought {_fmt_prop(prop)} for **{cost:,} 🪙**!\n"
            f"It earns **{property_daily_revenue(pid, row, current=True):,} 🪙/day**, banked with your daily claim.",
            C_GREEN,
        ))
        await self._offer_records(ctx)

    async def _buy_from_market(self, ctx, prop: dict, row: dict, now: float):
        uid = ctx.author.id
        pid = prop["id"]
        price = int(row["list_price"])
        seller_id = int(row["owner_id"])
        upgraded_note = " (⭐ upgraded)" if row.get("upgraded") else ""
        if not await confirm_purchase(
            ctx, title="🏘️ Buy Property",
            description=(
                f"Buy {_fmt_prop(prop, row)}{upgraded_note} from <@{seller_id}>.\n"
                f"It earns **{property_daily_revenue(pid, row, current=True):,} 🪙/day**, banked with your daily claim."
            ),
            cost=price, payer=ctx.author,
        ):
            return
        # The confirm wait is a long await — re-check the listing, since the
        # seller may have delisted/repriced or another buyer may have taken it.
        if row.get("owner_id") != seller_id or not row.get("list_price") or int(row["list_price"]) != price \
                or state.property_owners.get(pid) is not row:
            await ctx.send(embed=emb("🏷️ Listing Changed", f"The listing for {_fmt_prop(prop)} changed while you were confirming. You were not charged.", C_RED))
            return
        if owned_property_count(uid) >= max_owned():
            await ctx.send(embed=emb(
                "🏘️ Portfolio Full",
                f"You already own **{max_owned()}** properties — sell one first (`!assets sell <name> <price>`).",
                C_RED,
            ))
            return
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
            f"It earns **{property_daily_revenue(pid, row, current=True):,} 🪙/day**, banked with your daily claim.",
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
    @cmd_assets.command(name="sell", aliases=["list"],
                        help="List a property on the cross-server market; at 75% of value or less the bank buys it instantly", usage="<name> <price>")
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

    @cmd_assets.command(name="unlist", aliases=["delist"],
                        help="Remove your property's market listing", usage="<name>")
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
    # Two upgrade types (UPGRADE_TYPES): revenue (PROPERTY_UPGRADES, the row's
    # `upgraded` flag) and security (PROPERTY_SECURITY_UPGRADES, `secured`),
    # unsold while SECURITY_UPGRADES_LIVE is False. A type word picks one;
    # without it, the one the deed still lacks, or a button per type when it
    # lacks both.
    _UPGRADE_FLAGS = {"revenue": "upgraded", "security": "secured"}

    @staticmethod
    def _upgrade_full_cost(pid: str, kind: str) -> int:
        return PROPERTY_UPGRADES[pid][1] if kind == "revenue" else PROPERTY_SECURITY_UPGRADES[pid]

    @staticmethod
    def _security_effect_text(uid: int) -> str:
        tier = get_insurance_tier(uid, "property") if get_insurance_expiry(uid, "property") else None
        have = f"{tier.title()} property insurance" if tier else "no property insurance"
        return (
            "• Thefts on this deed succeed 25% less often.\n"
            "• With property insurance, the alarm also calls the insurer's investigators, "
            "doubling the thief's arrest odds.\n"
            f"You have: 📋 {have}"
        )

    def _upgrade_line(self, uid: int, pid: str, row: dict, kind: str) -> tuple[str, bool]:
        """One upgrade's status line for the listing, and whether the price
        shown is artifact-discounted."""
        full = self._upgrade_full_cost(pid, kind)
        if kind == "revenue":
            up_name, _, up_boost = PROPERTY_UPGRADES[pid]
            if row.get("upgraded"):
                return f"⭐ **{up_name}** owned (+{up_boost}% revenue)", False
            what = f"**{up_name}**: {{price}} for +{up_boost}% revenue"
        else:
            if row.get("secured"):
                return "🛡️ **Security** installed", False
            what = "🛡️ **Security**: {price}, thefts succeed 25% less often"
        price = property_upgrade_cost(uid, full)
        price_str = f"~~{full:,}~~ {price:,} 🪙" if price != full else f"{full:,} 🪙"
        return what.format(price=price_str), price != full

    @cmd_assets.command(name="upgrade", aliases=["upgrades"],
                        help="See your properties' upgrades, or buy one for the named property",
                        usage="[revenue|security] [property name]")
    async def assets_upgrade(self, ctx: commands.Context, *, name: str = None):
        uid = ctx.author.id
        kind = None
        # A leading or trailing type word picks the upgrade, unless the whole
        # text is itself a property's name (a business renamed "Security").
        if name and find_property(name) is None:
            words = name.split()
            if words and words[0].lower() in UPGRADE_TYPES:
                kind = words.pop(0).lower()
            elif words and words[-1].lower() in UPGRADE_TYPES:
                kind = words.pop().lower()
            name = " ".join(words) or None
        if kind is not None and kind not in live_upgrade_types():
            await ctx.send(embed=emb(
                "🛡️ Security Upgrades",
                "Security upgrades go on sale when property theft arrives.",
                C_GOLD,
            ))
            return
        if not name:
            await self._upgrade_listing(ctx)
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
        if kind is not None and kind not in upgrade_types_for(pid):
            await ctx.send(embed=emb(
                "🛡️ No Security Upgrade",
                f"{_fmt_prop(prop, row)} is tier {prop['tier']}; security upgrades start at tier {SECURITY_MIN_TIER}.",
                C_GOLD,
            ))
            return
        if kind is None:
            missing = [t for t in upgrade_types_for(pid) if not row.get(self._UPGRADE_FLAGS[t])]
            if not missing:
                await ctx.send(embed=emb("⭐ Already Upgraded", f"{_fmt_prop(prop, row)} has every upgrade.", C_PURPLE))
                return
            if len(missing) == 1:
                kind = missing[0]
            else:
                kind = await confirm_choice(
                    ctx, title="⭐ Build Upgrade",
                    description=(
                        f"Which upgrade for {_fmt_prop(prop, row)}?\n\n"
                        f"**Revenue:** {self._upgrade_line(uid, pid, row, 'revenue')[0]}\n\n"
                        f"**Security:** {self._upgrade_line(uid, pid, row, 'security')[0]}\n"
                        + self._security_effect_text(uid)
                    ),
                    choices=[
                        {"label": f"⭐ Revenue — {property_upgrade_cost(uid, self._upgrade_full_cost(pid, t)):,} 🪙"
                         if t == "revenue" else
                         f"🛡️ Security — {property_upgrade_cost(uid, self._upgrade_full_cost(pid, t)):,} 🪙",
                         "value": t, "default": t == "revenue"}
                        for t in missing
                    ],
                    payer=ctx.author,
                )
                if not kind:
                    return
        await self._buy_upgrade(ctx, prop, row, kind)

    async def _upgrade_listing(self, ctx: commands.Context):
        """Bare `!assets upgrade`: every owned deed's upgrades and prices."""
        uid = ctx.author.id
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
        discounted = False
        for p in props:
            row = _owner_row(p["id"])
            parts = []
            for kind in upgrade_types_for(p["id"]):
                line, cut = self._upgrade_line(uid, p["id"], row, kind)
                parts.append(line)
                discounted |= cut
            lines.append(f"{_fmt_prop(p, row)} — " + " · ".join(parts))
        lines.append("")
        each = (f"a revenue upgrade, and from tier {SECURITY_MIN_TIER} a security upgrade"
                if "security" in live_upgrade_types() else "one upgrade")
        lines.append(
            f"Each property has {each}; an upgrade's full cost adds to the property's value"
            + (" (your artifact discount doesn't change that)" if discounted else "")
            + ". `!assets upgrade <name>` to buy."
        )
        await send_ephemeral(ctx, embed=emb("⭐ Property Upgrades", "\n".join(lines), C_PURPLE))

    async def _buy_upgrade(self, ctx: commands.Context, prop: dict, row: dict, kind: str):
        uid = ctx.author.id
        pid = prop["id"]
        flag = self._UPGRADE_FLAGS[kind]
        if kind == "revenue":
            up_name, full_cost, up_boost = PROPERTY_UPGRADES[pid]
            label = f"its **{up_name}**"
            what = f"Build the **{up_name}** on {_fmt_prop(prop, row)} — **+{up_boost}%** revenue"
        else:
            full_cost = PROPERTY_SECURITY_UPGRADES[pid]
            label = "security"
            what = f"🛡️ Install security on {_fmt_prop(prop, row)}:\n{self._security_effect_text(uid)}\n\nIts cost"
        if row.get(flag):
            await ctx.send(embed=emb("⭐ Already Upgraded", f"{_fmt_prop(prop, row)} already has {label}.", C_PURPLE))
            return
        # Charge the artifact-discounted price; property_value still folds in
        # the full catalog cost, so the discount can't be sold back to the bank.
        up_cost = property_upgrade_cost(uid, full_cost)
        discount_note = (
            f" — your artifact takes it from {full_cost:,} 🪙 to {up_cost:,} 🪙"
            if up_cost != full_cost else ""
        )
        joiner = " folds" if kind == "security" else ", its full cost folds"
        if not await confirm_purchase(
            ctx, title="⭐ Build Upgrade",
            description=f"{what}{joiner} into the value{discount_note}.",
            cost=up_cost, payer=ctx.author,
        ):
            return
        # The confirm wait is a long await — re-check ownership and the
        # upgrade flag, since the deed may have sold (or a concurrent upgrade
        # landed) during the prompt.
        if row.get("owner_id") != uid or state.property_owners.get(pid) is not row:
            await ctx.send(embed=emb("❌ Not Yours", f"You no longer own {_fmt_prop(prop)}. You were not charged.", C_RED))
            return
        if row.get(flag):
            await ctx.send(embed=emb("⭐ Already Upgraded", f"{_fmt_prop(prop, row)} already has {label}.", C_PURPLE))
            return
        # Gate-and-claim: set the flag synchronously before the charge so a
        # concurrent second !assets upgrade sees the claim and bails instead
        # of double-charging.
        row[flag] = True
        if not await shop_charge(ctx, uid, up_cost):
            row[flag] = False
            return
        await save_property_owner(pid, row)
        if kind == "revenue":
            body = (
                f"{_fmt_prop(prop, row)} now has a **{up_name}**!\n"
                f"Revenue: **{property_daily_revenue(pid, row, current=True):,} 🪙/day** (+{up_boost}%) • "
                f"Value: **{property_value(pid, row):,} 🪙**"
            )
        else:
            body = (
                f"{_fmt_prop(prop, row)} now has security: thefts on it succeed 25% less often.\n"
                f"Value: **{property_value(pid, row):,} 🪙**"
            )
        await ctx.send(embed=emb("⭐ Upgrade Built", body, C_GREEN))
        # The upgrade cost folds into portfolio value — offer the records.
        await self._offer_records(ctx)

    # ── !assets rename ────────────────────────────────────────────────────
    @cmd_assets.command(name="rename",
                        help="Rename a business you own", usage="<name> <new name>")
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
