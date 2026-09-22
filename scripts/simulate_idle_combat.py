"""Measure the !idle monster-fight balance against the current constants.

CLAUDE.md asks for this before changing MOB_WIN_CLOCK_DIVISOR, the monster
tiers, the gold rates or Pace.mob_fights_per_day, because those were set by
simulation and not by argument. It runs the real rules — no model of them —
so whatever the bot does, this does.

Run from the repo root:
    python scripts/simulate_idle_combat.py
    python scripts/simulate_idle_combat.py --days 400 --pace classic

The figures it prints, and what they were tuned to:

    fights/d    ~90 at the lively pace (Pace.mob_fights_per_day)
    won%        ~96-99, falling with level as monsters scale with gear
    deaths/d    ~1-3, rising with level; CAMP_HP_PCT is what keeps it there
    gold/d      a few times a level-up's, which is printed beside it
    clock%/d    about -7 at every level — the whole point of the exercise

Three things will quietly ruin a run of this if you rewrite it (each cost an
afternoon once already):

  * The character must be wandered every minute. A death carries it to a
    town's outskirts, which is inside the market ring, and nothing spawns
    in a ring — so without movement it stops fighting for good and every
    figure below collapses toward zero while still looking plausible.
  * A flawless kill leaves hit points untouched, so "did HP go up" does not
    identify a camp. Match the ⛺ note instead.
  * clock%/d has to be summed per fight as a share of the time then
    remaining. `scale` moves a percentage of what is left, so comparing a
    start and end clock compounds and reports nonsense.
"""
import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import idlerpg as rpg   # noqa: E402

NOW = 1_800_000_000
# (level, item power) pairs spanning the curve: a beginner, the middle, the
# soft cap and past it.
RUNGS = ((5, 8), (20, 60), (40, 200), (60, 500))
WILDS = (5, 255)          # open country, clear of every market ring


def simulate(level: int, gear: int, pace: rpg.Pace, days: int, seed: int) -> dict:
    rng = random.Random(seed)
    char = rpg.new_character("Sim", NOW)
    char.update(level=level, x=WILDS[0], y=WILDS[1], gold=10_000,
                # A clock far longer than the run, so no level-up interrupts
                # it: this measures fighting alone.
                next_level_at=NOW + 10 ** 9,
                items={"ring": {"level": gear, "name": None}})
    char["hp"] = rpg.max_hp(char)
    chars, quest = {1: char}, rpg.new_quest()
    started_with = char["gold"]
    fights = camps = 0
    clock_pct = 0.0

    for _ in range(days * 1440):
        rpg.regen_hp(char)
        rpg.move_players(chars, quest, rng, lambda uid: "P", NOW, 60)
        if rng.random() >= pace.mob_fights_per_day / 1440:
            continue
        before = rpg.time_left(char, NOW)
        notes = rpg.mob_encounter(1, char, rng, lambda uid: "P", NOW)
        if any("⛺" in note.text for note in notes):
            camps += 1
            continue
        fights += 1
        clock_pct += (rpg.time_left(char, NOW) - before) / max(before, 1) * 100

    kills, deaths = char["mob_kills"], char["mob_deaths"]
    return {
        "fights/d": fights / days,
        "camps/d": camps / days,
        "won%": 100 * kills / max(kills + deaths, 1),
        "deaths/d": deaths / days,
        "gold/d": (char["gold"] - started_with) / days,
        "lvlup gold": float(rpg.level_gold(level)),
        "clock%/d": clock_pct / days,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=200, help="simulated days per rung (default 200)")
    parser.add_argument("--pace", default=rpg.DEFAULT_PACE, choices=sorted(rpg.PACES))
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    pace = rpg.PACES[args.pace]

    print(f"{args.days} days per rung at the {args.pace} pace, "
          f"MOB_WIN_CLOCK_DIVISOR={rpg.MOB_WIN_CLOCK_DIVISOR}\n")
    columns = ("fights/d", "camps/d", "won%", "deaths/d", "gold/d", "lvlup gold", "clock%/d")
    print("  lvl  gear" + "".join(f"{name:>12}" for name in columns))
    for level, gear in RUNGS:
        out = simulate(level, gear, pace, args.days, args.seed)
        print(f"{level:>5}{gear:>6}" + "".join(f"{out[name]:>12.2f}" for name in columns))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
