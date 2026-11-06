"""Property supply: which pool deeds reach the market, and the per-player cap.

Once per gameplay-day, at the first minute-loop tick after the 5am CT reset,
`run_property_release` measures the economy and admits every pool deed whose
threshold it has passed. Nothing refills: a sold deed stays sold, and no rule
here looks at how many deeds of a tier are unsold.

The measure is a development index D, three quarters ownership and one
quarter wealth:

    D = 0.75 * owned_deeds / 40 + 0.25 * W,   W = log10(net_worth / 3M)

so D is 1.0 at 40 owned deeds and 30M of net worth, and keeps growing past
it. Net worth is wallets + savings + property book value + artifacts at
catalog cost. A tier's progress is D ** TIER_EXPONENTS[tier] (all 1.0 for
now — the dial to turn if tier 1 sells out long before tier 5 moves).

Each pool deed carries its own threshold (RELEASE_THRESHOLDS, in release
order): k / target plus a seeded jitter of up to 30% of a step, so two tiers
release on the same morning only by coincidence. The table was rolled once
(seed "discord-ollama-bot-property-supply-1965") and is a shipped constant.

Once a tier's last pool deed is due, the thresholds carry on at the same
spacing (copy_threshold) and each releases the tier's next numbered copy
(properties.copy_id): Lemonade Stand #2, ... then every #3. So supply keeps
scaling with the economy instead of stopping at the catalog's end.

The per-player cap is 5 + floor(W / 0.4): one more slot each time net worth
grows 2.5 times. Both releases and the cap ratchet — a released deed stays
released and the cap is a high-water mark — so a shrinking economy never
takes anything back.
"""
from __future__ import annotations

import hashlib
import logging
import math
import time

from src import state
from src.properties import (
    PROPERTIES_BY_ID, PROPERTY_BASE_MAX_OWNED, PROPERTY_RELEASE_ORDER,
    copy_id, is_released, max_owned, register_copy, total_owned_property_value,
)

OWNED_ANCHOR = 40
NET_WORTH_FLOOR = 3_000_000
NET_WORTH_ANCHOR = 30_000_000
OWNED_WEIGHT = 0.75
TIER_EXPONENTS = {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0, 5: 1.0}
# W per extra slot: log10(2.5) ≈ 0.4, so the cap steps at 7.5M, 18.9M, 47M…
CAP_STEP = 0.4

# Threshold in tier progress for each pool deed, matched by position to
# PROPERTY_RELEASE_ORDER. Spacing is 1 / (6, 5, 4, 3, 2) for tiers 1..5 —
# low tiers release faster — with the jitter on top.
RELEASE_THRESHOLDS: dict[int, tuple[float, ...]] = {
    1: (0.133, 0.318, 0.471, 0.629, 0.804, 0.957, 1.128, 1.372, 1.514, 1.689, 1.795, 1.999),
    2: (0.201, 0.389, 0.658, 0.775, 1.053, 1.244, 1.417, 1.622, 1.844, 1.975),
    3: (0.239, 0.540, 0.756, 0.975, 1.208, 1.560, 1.713, 2.064, 2.241),
    4: (0.410, 0.582, 1.075, 1.289, 1.730, 2.076),
    5: (0.507, 0.868, 1.638, 2.090, 2.373),
}

assert all(len(RELEASE_THRESHOLDS[t]) == len(ids) for t, ids in PROPERTY_RELEASE_ORDER.items()), \
    "every pool deed needs a release threshold"
assert all(list(th) == sorted(th) for th in RELEASE_THRESHOLDS.values()), \
    "thresholds rise along the release order"

RELEASE_STEPS = {1: 1 / 6, 2: 1 / 5, 3: 1 / 4, 4: 1 / 3, 5: 1 / 2}


def copy_threshold(tier: int, k: int) -> float:
    """Threshold for a tier's k-th copy: the pool's spacing continued past
    its last deed, with the same up-to-30% jitter, from a hash so it never
    changes between boots."""
    step = RELEASE_STEPS[tier]
    roll = int(hashlib.sha256(f"property-copy:{tier}:{k}".encode()).hexdigest()[:8], 16) / 2 ** 32
    return RELEASE_THRESHOLDS[tier][-1] + (k + 1) * step + 0.3 * step * roll


def economy_net_worth(now: float | None = None) -> int:
    """Wallets + savings (with interest to date) + property book value +
    artifacts at catalog cost, bot-wide."""
    from src.artifacts import ALL_ARTIFACTS
    from src.economy import savings_growth
    now = time.time() if now is None else now
    wallets = savings = 0.0
    for uid_str, u in state.economy["users"].items():
        wallets += u.get("balance", 0) or 0
        for e in u.get("savings", []):
            savings += e["amount"] * savings_growth(e["deposited_at"], now, int(uid_str))
    costs = {a["id"]: a["cost"] for a in ALL_ARTIFACTS}
    artifacts = sum(costs.get(aid, 0) * qty
                    for owned in state.user_artifacts.values() for aid, qty in owned.items())
    return int(wallets + savings + total_owned_property_value() + artifacts)


def owned_deed_count() -> int:
    return sum(1 for pid in state.property_owners if pid in PROPERTIES_BY_ID)


def wealth_progress(net_worth: float) -> float:
    if net_worth <= NET_WORTH_FLOOR:
        return 0.0
    return math.log(net_worth / NET_WORTH_FLOOR) / math.log(NET_WORTH_ANCHOR / NET_WORTH_FLOOR)


def development_index(owned: int, net_worth: float) -> float:
    return OWNED_WEIGHT * owned / OWNED_ANCHOR + (1 - OWNED_WEIGHT) * wealth_progress(net_worth)


def deeds_due(index: float) -> list[str]:
    """Pool ids whose threshold the index has passed, released or not."""
    due = []
    for tier, ids in PROPERTY_RELEASE_ORDER.items():
        progress = index ** TIER_EXPONENTS[tier] if index > 0 else 0.0
        due += [pid for pid, th in zip(ids, RELEASE_THRESHOLDS[tier]) if th <= progress]
        k = 0
        while copy_threshold(tier, k) <= progress:
            due.append(copy_id(tier, k))
            k += 1
    return due


def cap_for(net_worth: float) -> int:
    return PROPERTY_BASE_MAX_OWNED + int(wealth_progress(net_worth) / CAP_STEP)


async def run_property_release(now: float | None = None) -> tuple[list[str], int]:
    """The once-per-gameplay-day release pass; returns (newly released ids,
    the cap after it). Driven by EconomyCog's minute loop, gated like the
    insurance sweep by a gameplay-day marker in economy_meta.

    The boot that ships this only stamps the day: deeds appear at a reset,
    never the moment the code lands."""
    from src.economy import _ct_today
    from src.persistence import save_property_release, save_property_supply_meta
    today = _ct_today()
    prior = state.economy.get("last_property_release")
    if prior == today:
        return [], max_owned()
    # Claim the day before any await (see CLAUDE.md: Concurrency).
    state.economy["last_property_release"] = today
    if prior is None:
        await save_property_supply_meta()
        return [], max_owned()
    now = time.time() if now is None else now
    net_worth = economy_net_worth(now)
    index = development_index(owned_deed_count(), net_worth)
    due = deeds_due(index)
    for pid in due:
        register_copy(pid)
    new = [pid for pid in due if not is_released(pid)]
    for pid in new:
        state.released_properties.add(pid)
    cap = max(max_owned(), cap_for(net_worth))
    state.economy["property_cap"] = cap
    await save_property_supply_meta()
    for pid in new:
        await save_property_release(pid, now)
    logging.info(f"[property_supply] {today}: index {index:.3f}, net worth {net_worth:,}, "
                 f"released {new or 'nothing'}, cap {cap}")
    return new, cap
