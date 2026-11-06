"""Property theft: the rules `!steal <property>` and `!assets repair` share.

A theft goes for two days of the deed's current rent (`theft_take`). A
success pays the thief that much and damages the deed, cutting its income
by DAMAGE_PCT until the owner repairs it; the repair bill is the take, and
the owner's property insurance at that moment (snapshotted onto the row)
pays its share. A failure fines the thief FINE_PERMILLE of the take and may
jail them. Security cuts the odds; security plus any property insurance
calls the investigators, who double the jail chance on a failure and roll
an arrest on a success (the thief keeps the loot). One success per thief
per gameplay day.

Everything here is a pure read or a synchronous mutation of `state`, so a
caller can gate and claim with no await in between (see CLAUDE.md:
Concurrency).
"""
from __future__ import annotations

from src import state
from src.properties import PROPERTIES_BY_ID, property_daily_revenue

SUCCESS_BY_TIER = {1: 0.35, 2: 0.30, 3: 0.25, 4: 0.20, 5: 0.15}
JAIL_BY_TIER = {1: 0.20, 2: 0.25, 3: 0.30, 4: 0.35, 5: 0.40}
TAKE_RENT_DAYS = 2
DAMAGE_PCT = 25
FINE_PERMILLE = 125  # 12.5% of the take
BAIL_TAKE_MULT = 2
BAIL_MIN = 1_000
INVESTIGATOR_JAIL_MULT = 2


def is_damaged(row: dict | None) -> bool:
    return bool(row) and row.get("damaged_at") is not None


def theft_take(pid: str, row: dict) -> int:
    """Two days of what the deed pays today, revenue upgrade included and
    damage ignored (a damaged deed can't be targeted anyway)."""
    healthy = {**row, "damaged_at": None}
    return TAKE_RENT_DAYS * property_daily_revenue(pid, healthy, current=True)


def has_investigators(row: dict) -> bool:
    """Security plus any active property insurance on the owner."""
    from src.economy import get_insurance_expiry
    return bool(row.get("secured")) and get_insurance_expiry(row["owner_id"], "property") is not None


def success_chance(thief_id: int, pid: str, row: dict) -> float:
    from src.artifacts import steal_success_chance
    from src.properties import SECURITY_SUCCESS_MULT
    chance = steal_success_chance(thief_id, SUCCESS_BY_TIER[PROPERTIES_BY_ID[pid]["tier"]])
    if row.get("secured"):
        chance *= SECURITY_SUCCESS_MULT
    return min(1.0, chance)


def jail_chance(thief_id: int, pid: str, row: dict, *, succeeded: bool) -> float:
    """A failure's jail roll (doubled by investigators), or a success's
    arrest roll: the tier's base chance with investigators, else none."""
    from src.artifacts import crime_catch_chance
    base = crime_catch_chance(thief_id, JAIL_BY_TIER[PROPERTIES_BY_ID[pid]["tier"]])
    investigated = has_investigators(row)
    if succeeded:
        return base if investigated else 0.0
    return min(1.0, base * INVESTIGATOR_JAIL_MULT) if investigated else base


def fine_for(take: int) -> int:
    return take * FINE_PERMILLE // 1000


def bail_for(take: int) -> int:
    return max(BAIL_MIN, take * BAIL_TAKE_MULT)


def stolen_today(uid: int) -> bool:
    from src.economy import _ct_today
    return state.economy["users"].get(str(uid), {}).get("property_theft_day") == _ct_today()


def cover_snapshot(owner_id: int) -> tuple[int, int]:
    """(pct, cap) of the owner's property insurance right now, or (0, 0)."""
    from src.economy import get_insurance_expiry, get_insurance_tier, insurance_tier_info
    if get_insurance_expiry(owner_id, "property") is None:
        return 0, 0
    info = insurance_tier_info(get_insurance_tier(owner_id, "property"), "property")
    return int(info["refund_pct"]), int(info["refund_cap"])


def damage(row: dict, take: int, now: float) -> dict:
    """Mark the deed damaged, locking in the owner's cover. Returns what it
    replaced, for `restore` on a rollback."""
    prior = {k: row.get(k) for k in ("damaged_at", "repair_cost", "repair_cover_pct", "repair_cover_cap")}
    pct, cap = cover_snapshot(row["owner_id"])
    row.update(damaged_at=now, repair_cost=take, repair_cover_pct=pct, repair_cover_cap=cap)
    return prior


def clear_damage(row: dict) -> dict:
    prior = {k: row.get(k) for k in ("damaged_at", "repair_cost", "repair_cover_pct", "repair_cover_cap")}
    row.update(damaged_at=None, repair_cost=0, repair_cover_pct=0, repair_cover_cap=0)
    return prior


def restore(row: dict, prior: dict) -> None:
    row.update(prior)


def repair_split(row: dict) -> tuple[int, int]:
    """(insurer's share, owner's share) of a damaged deed's repair bill."""
    bill = int(row.get("repair_cost", 0) or 0)
    covered = min(bill * int(row.get("repair_cover_pct", 0) or 0) // 100, int(row.get("repair_cover_cap", 0) or 0))
    return covered, bill - covered


def income_lost(pid: str, row: dict) -> int:
    """What the damage costs the owner a day: the full rent minus the
    damaged rent, at today's rate."""
    healthy = {**row, "damaged_at": None}
    return property_daily_revenue(pid, healthy, current=True) - property_daily_revenue(pid, row, current=True)


def damaged_deeds(uid: int) -> list[str]:
    """The owner's damaged deeds, most income lost first: the order a
    repair-all spends the wallet in."""
    pids = [pid for pid, row in state.property_owners.items()
            if row["owner_id"] == int(uid) and is_damaged(row) and pid in PROPERTIES_BY_ID]
    return sorted(pids, key=lambda pid: (-income_lost(pid, state.property_owners[pid]), pid))
