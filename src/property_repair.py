"""Auto-repair (`!assets autorepair`) and the daily claim's theft report.

Auto-repair pays the owner's share of a robbed deed's repair bill from the
wallet the moment it's robbed, and retries at each daily claim while the
wallet falls short. It never touches savings. Like `!assets repair`, each
deed's damage is cleared synchronously before the charge and put back if
the charge fails, so the button, the typed command and this can't pay twice.
"""
from __future__ import annotations

from src import property_theft, state

CLAIM_TALLIES = ("robbed_since_claim", "investigations_since_claim", "arrests_since_claim", "auto_repaired_since_claim")


def auto_repair_on(uid: int) -> bool:
    return bool(state.economy["users"].get(str(uid), {}).get("auto_repair"))


async def auto_repair(uid: int, pids: list[str] | None = None, *, keep: int = 0) -> tuple[list[str], int]:
    """Repair what the wallet covers, most income lost first, leaving `keep`
    coins untouched. Returns (repaired pids, coins spent); nothing when the
    user hasn't switched auto-repair on."""
    from src.economy import deduct_balance
    from src.persistence import save_property_owner
    if not auto_repair_on(uid):
        return [], 0
    godmode = uid in state.godmode_users
    budget = state.economy["users"][str(uid)]["balance"] - keep
    chosen, spent = [], 0
    for pid in property_theft.damaged_deeds(uid):
        if pids is not None and pid not in pids:
            continue
        share = 0 if godmode else property_theft.repair_split(state.property_owners[pid])[1]
        if spent + share > budget:
            continue
        chosen.append(pid)
        spent += share
    if not chosen:
        return [], 0
    priors = {pid: property_theft.clear_damage(state.property_owners[pid]) for pid in chosen}
    if spent and not await deduct_balance(uid, spent):
        for pid, prior in priors.items():
            property_theft.restore(state.property_owners[pid], prior)
        return [], 0
    for pid in chosen:
        await save_property_owner(pid, state.property_owners[pid])
    return chosen, spent


def _plural(n: int, one: str, many: str) -> str:
    return f"**{n}** {one if n == 1 else many}"


async def claim_report(uid: int, *, keep: int = 0) -> str:
    """The daily claim's property-theft lines, each starting with a newline,
    or "". Runs auto-repair first and zeroes the since-claim tallies; the
    read-and-zero has no await in it, so a theft landing meanwhile is
    reported by this claim or the next, never both."""
    from src.persistence import save_economy
    user = state.economy["users"][str(uid)]
    robbed, calls, caught, spent_before = (int(user.get(k, 0) or 0) for k in CLAIM_TALLIES)
    for k in CLAIM_TALLIES:
        user[k] = 0
    fixed, spent_now = await auto_repair(uid, keep=keep)
    lines = []
    if robbed:
        lines.append(f"🏚️ Your properties were robbed {_plural(robbed, 'time', 'times')} since your last claim.")
    if calls:
        lines.append(f"🕵️ Investigators answered {_plural(calls, 'alarm', 'alarms')} and caught "
                     f"{_plural(caught, 'thief', 'thieves')}.")
    if spent_before + spent_now:
        lines.append(f"🔧 Auto-repair paid **{spent_before + spent_now:,} 🪙** in repairs.")
    damaged = property_theft.damaged_deeds(uid)
    if damaged:
        lost = sum(property_theft.income_lost(pid, state.property_owners[pid]) for pid in damaged)
        retry = " Auto-repair tries again at your next claim." if auto_repair_on(uid) else ""
        lines.append(f"🔧 {_plural(len(damaged), 'damaged property', 'damaged properties')} "
                     f"{'is' if len(damaged) == 1 else 'are'} losing **{lost:,} 🪙** a day — `!assets repair`.{retry}")
    if robbed or calls or spent_before or fixed:
        await save_economy(uid=uid)
    return "".join(f"\n{line}" for line in lines)
