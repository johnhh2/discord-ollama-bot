"""Idle RPG rules (!idle) — pure functions over character dicts. No Discord,
no DB, and every random draw goes through the `rng` a caller passes in, so
the whole ruleset is testable with a seeded `random.Random`.

The rules are the classic IRC IdleRPG's (level by idling) with a gentler
curve, prestige and a daily duel, minus its penalties for talking — on
Discord there is always somewhere else to talk. A character's clock is an
absolute `next_level_at` while it runs and a `remaining` seconds count while
it is paused — exactly one of the two is set. Everything that speeds up or
slows down a character goes through `shift` / `scale`, which handle both.

Functions that tell the players something return `Note`s; the cog decides
where they are posted (src/cogs/idle_cog.py).
"""
from __future__ import annotations

import math
import re
from typing import Callable, NamedTuple

from src.helpers import format_duration

# ── the curve ────────────────────────────────────────────────────────────────

BASE_TTL = 600           # seconds from level 0 to level 1
START_JITTER_SECS = 600  # …plus up to this much at birth, so characters made together don't level in step
LEVEL_MULT = 1.12        # each level takes this much longer (level 60 ≈ 52 idle days in all)
SOFT_CAP = 60
POST_CAP_MULT = 1.25     # past the soft cap the curve steepens
PENALTY_MULT = 1.10      # penalties grow with level too

# A level-up is channel news on every MILESTONE_EVERY-th level, and on every
# level once one takes at least RARE_LEVEL_SECS (level 62 on the base curve).
# The rest stay in the player's own feed thread.
MILESTONE_EVERY = 10
RARE_LEVEL_SECS = 7 * 86_400

PRESTIGE_LEVEL = 60
PRESTIGE_BONUS_PCT = 5   # faster levelling per prestige rank…
PRESTIGE_MAX_RANKS = 5   # …for the first five ranks

# Penalty bases, in seconds at level 0.
PEN_PART = 200           # left their own feed thread
PEN_QUIT = 20            # left the server

# A player counts as logged in for this long after they were last seen online.
GRACE_SECS = 3600

# Sitting in one of the server's voice channels: the clock runs this much
# faster and gold earned by the tick (level-ups, fights, quests, luck) is this
# much larger. Wagers and the shop are untouched.
VOICE_BONUS_PCT = 10

ALIGN_COOLDOWN_SECS = 86_400
DUEL_PCT = 5
DUEL_MAX_ROUNDS = 5

# ── gold ─────────────────────────────────────────────────────────────────────
# The game's own currency: per character, earned only by playing, never
# exchanged with the bot's coins and never sent between players (a !pay
# would let a server funnel everything to one character). It moves between
# characters only through a wagered duel and a collision fight's spoils.
# Income and prices both scale with level.
GOLD_PER_LEVEL = 10            # × the level reached
GOLD_PER_WIN = 5               # × the beaten opponent's level
GOLD_HOUSE_WIN = 10
GOLD_QUEST_PER_MEMBER = 250    # × party size, to each quester
GOLD_GODSEND_PER_LEVEL = 20
GOLD_EVENT_CHANCE = 0.2        # share of godsends / calamities that are about gold
GOLD_SPOILS_PCT = 5            # of the loser's purse, to a collision fight's winner

PRICE_FIND_PER_LEVEL = 25
PRICE_SHARPEN_PER_ITEM_LEVEL = 20
PRICE_RUSH_PER_LEVEL = 15
PRICE_SECOND_DUEL_PER_LEVEL = 10
PRICE_CLASS = 100
SHARPEN_PCT = 10
RUSH_PCT = 10

# ── per-day odds (the cog divides by its ticks per day) ──────────────────────

GODSEND_PER_DAY = 1 / 8
CALAMITY_PER_DAY = 1 / 8
HAND_OF_GOD_PER_DAY = 1 / 20
BLESSING_PER_DAY = 1 / 12      # good characters
TEMPTATION_PER_DAY = 1 / 8     # evil characters
TEAM_BATTLE_PER_DAY = 1 / 4    # per guild
# Lawful characters live quieter lives, chaotic ones louder — for good and ill.
# Only godsends and calamities scale: they mirror each other, so the axis
# changes the swing and not the average. The Hand of God helps four times
# in five, so scaling it too made chaotic simply the best pick.
LAW_EVENT_FACTOR = {"lawful": 0.5, "neutral": 1.0, "chaotic": 2.0}

BATTLE_ALWAYS_LEVEL = 25       # below this a level-up only sometimes means a fight
BATTLE_CHANCE_BELOW = 0.25
CRIT_ODDS = {"good": 50, "neutral": 35, "evil": 20}   # 1-in-N on a won battle
STEAL_CHANCE = 0.02
TEAM_SIZE = 3

QUEST_MIN_LEVEL = 40
QUEST_MIN_PARTY, QUEST_MAX_PARTY = 2, 4
QUEST_MIN_SECS, QUEST_MAX_SECS = 12 * 3600, 24 * 3600
QUEST_REWARD_PCT = 25
QUEST_REST_SECS = 6 * 3600     # before the next one

# The map: coordinates run 0..MAP_SIZE on both axes and wrap at the edges —
# a step off one is a step onto the other, so the realm is a globe and every
# distance on it is the short way round. MAP_SPAN is how many squares that
# makes: 0..MAP_SIZE inclusive.
MAP_SIZE = 500
MAP_SPAN = MAP_SIZE + 1
JOURNEY_STEP_CHANCE = 0.01     # per quester per second
COLLISION_CRIT_ODDS = 35       # 1-in-N on a won collision fight
COLLISION_STEAL_ODDS = 25      # …else 1-in-N to swap an item, from COLLISION_STEAL_LEVEL up
COLLISION_STEAL_LEVEL = 20



class Pace(NamedTuple):
    """How often luck strikes. The IRC odds assume dozens of players idling
    for months; a Discord server with a handful sees almost nothing at that
    rate, so "lively" is the default and "classic" is there for a crowd."""
    godsend_per_day: float
    calamity_per_day: float
    hand_of_god_per_day: float
    battle_chance_below: float   # chance a level-up under BATTLE_ALWAYS_LEVEL means a fight
    min_team_size: int           # team battles shrink to this when too few are online
    mob_fights_per_day: float    # monster encounters per character, per day spent outside the towns


PACES = {
    # sizzlorox's own ~90 a day — one every sixteen minutes of wilds time.
    # It only works because a fight spends hit points rather than clock; at
    # the old stakes this would have levelled a character by itself.
    "lively": Pace(1.0, 1.0, 1 / 5, BATTLE_CHANCE_BELOW, 2, 90.0),
    "classic": Pace(GODSEND_PER_DAY, CALAMITY_PER_DAY, HAND_OF_GOD_PER_DAY, BATTLE_CHANCE_BELOW, TEAM_SIZE, 30.0),
}
DEFAULT_PACE = "lively"
CLASSIC = PACES["classic"]

LAWS = ("lawful", "neutral", "chaotic")
MORALS = ("good", "neutral", "evil")

ITEM_SLOTS = (
    "ring", "amulet", "charm", "weapon", "helm",
    "tunic", "gloves", "leggings", "shield", "boots",
)

# (minimum character level, slot, name, lowest item level, highest item level)
UNIQUES = (
    (25, "helm", "Crown of the Unblinking Moderator", 50, 74),
    (25, "ring", "Signet of Perpetual Lurking", 50, 74),
    (30, "tunic", "Hauberk of the Muted Channel", 75, 99),
    (35, "amulet", "Amulet of the Last Seen Recently", 100, 124),
    (40, "weapon", "Greatsword of Read Receipts", 150, 174),
    (45, "weapon", "Staff of the Unanswered Ping", 175, 200),
    (48, "boots", "Slippers of the Away Status", 250, 300),
    (52, "weapon", "Banhammer of the Elder Admins", 300, 350),
)
UNIQUE_ODDS = 40   # 1-in-N per level-up, checked per eligible unique

HOUSE_NAME = "the Idle Warden"

_GODSENDS = (
    "found a forgotten shortcut through the hills",
    "was carried a league by a friendly giant",
    "slept so well that a whole day's march felt like a stroll",
    "was handed a map by a ghost with nothing better to do",
    "caught a tailwind blowing the right way for once",
    "was mistaken for royalty and waved through every gate",
    "drank from a spring that tasted faintly of purpose",
    "got a lift on a merchant's cart",
)
_CALAMITIES = (
    "was chased up a tree by an unreasonable goose",
    "took a wrong turn at a very convincing signpost",
    "lost a day arguing with a bridge troll about tolls",
    "fell into a bog and had to dry every sock",
    "was cursed with hiccups by a bored hedge witch",
    "walked in circles around a suspiciously familiar rock",
    "ate the mushrooms",
    "was held up at a border for improper paperwork",
)
_ITEM_BOONS = (
    "A wandering smith polished", "A blessing settled on", "Moonlight tempered",
)
_ITEM_BANES = (
    "Rust crept into", "A gremlin chewed on", "Rain warped",
)
# The places drawn on the map (assets/idle_map.png), at the coordinates the
# IRC game's own journeys used for them; the Towers and T'rnalvph sit on
# their drawings rather than their captions.
LANDMARKS = {
    "Denmark": (35, 40),
    "the Mountains of Qwok": (290, 65),
    "the land of Qwok": (430, 60),
    "Jow Botzi territory": (155, 155),
    "Velvragh": (325, 270),
    "the Secret Passage to Bharash": (70, 315),
    "the Great Shahlil mountains": (50, 350),
    "the Towers of Ankh-Allor": (255, 425),
    "T'rnalvph": (470, 400),
}
_LANDMARK_NAMES = {point: label for label, point in LANDMARKS.items()}

# The settlements among them have markets. `!idle shop` works within
# MARKET_RADIUS of one; walking into its centre (TOWN_CORE_RADIUS) makes the
# character trade on its own — the wide ring is the player's chance to spend
# the gold their way first. Nobody steers on this map, so the ring has to be
# generous: five of them cover about a fifth of the realm.
TOWNS = ("Denmark", "the land of Qwok", "Velvragh", "the Towers of Ankh-Allor", "Jow Botzi territory")
MARKET_RADIUS = 60
TOWN_CORE_RADIUS = 15
# Two players only run into each other in a level-up battle within half a
# market ring of one another. Without it the pool was "everyone online", so
# in a small server every level-up was a fight with the same rival; now the
# map decides who you meet, and an empty stretch of wilderness is quiet.
BATTLE_RANGE = MARKET_RADIUS // 2
AUTO_TRADE_COOLDOWN_SECS = 12 * 3600
AUTO_TRADE_BUDGET_PCT = 50     # an errand never spends more than this share of the purse

# (first waypoint, second waypoint, what the party was chosen to do)
_JOURNEYS = (
    ("Denmark", "Velvragh", "deliver a strongly worded letter from the Danes to the elders of Velvragh"),
    ("the Secret Passage to Bharash", "the Towers of Ankh-Allor", "smuggle a sleeping wizard back into his own tower"),
    ("Jow Botzi territory", "the land of Qwok", "trade a cart of turnips for whatever Qwok thinks is fair"),
    ("the Great Shahlil mountains", "the Mountains of Qwok", "settle, on foot and with a rope, which mountains are taller"),
    ("T'rnalvph", "Velvragh", "chart the dark lands and bring the map home before the ink runs"),
    ("the Towers of Ankh-Allor", "Denmark", "return a borrowed ladder, several centuries late"),
)

_VIGILS = (
    "stand vigil over the sleeping dragon of the eastern pass",
    "escort a caravan of extremely slow pilgrims",
    "guard the silence of the Great Library",
    "wait out the siege of a castle nobody remembers the name of",
    "watch a pot until it boils, by royal decree",
    "hold the line at a bridge no enemy has yet found",
)


class Note(NamedTuple):
    """Something to tell the players. `uids` are the characters it concerns
    (their feed threads get it); `public` also posts it in the idle channel.
    `ping` lists the users whose `<@id>` in `text` may really mention them
    there — the post stays silent, so that's a badge, not a notification."""
    uids: tuple
    text: str
    public: bool = False
    ping: tuple = ()
    show_map: bool = False   # post the map under it (a journey's start)
    public_text: "str | None" = None   # said in the channel instead, when the room needs less than the feed


NameFn = Callable[[int], str]


# ── characters and their clocks ──────────────────────────────────────────────

def ttl(level: int, prestige: int = 0) -> int:
    """Seconds it takes to go from `level` to `level + 1`."""
    if level <= SOFT_CAP:
        base = BASE_TTL * LEVEL_MULT ** level
    else:
        base = BASE_TTL * LEVEL_MULT ** SOFT_CAP * POST_CAP_MULT ** (level - SOFT_CAP)
    bonus = PRESTIGE_BONUS_PCT * min(prestige, PRESTIGE_MAX_RANKS)
    return int(base * (100 - bonus) / 100)


def level_is_news(level: int, prestige: int = 0) -> bool:
    """Whether reaching `level` is announced in the idle channel."""
    return level % MILESTONE_EVERY == 0 or ttl(level - 1, prestige) >= RARE_LEVEL_SECS


UNCLAIMED_CLASS = "Adventurer"


def stagger_start(char: dict, rng) -> None:
    """Characters that share a clock level — and roll their battles — in the
    same minute, every level, for good. A random head start breaks the step."""
    shift(char, rng.randrange(START_JITTER_SECS))


def new_character(class_name: str, now: int, claimed: bool = True) -> dict:
    return {
        "claimed": claimed,       # False: enrolled by the bot, not yet taken up with !idle join
        "class": class_name,
        "level": 0,
        "next_level_at": now + ttl(0),
        "remaining": None,
        "law": "neutral",
        "moral": "neutral",
        "prestige": 0,
        "penalty_total": 0,
        "last_seen": now,
        "last_penalty_at": 0,
        "thread_id": None,
        "created_at": now,
        "align_changed_at": 0,
        "duel_day": None,
        "items": {},
        "x": None,   # placed by ensure_position, which has the rng
        "y": None,
        "gold": 0,
        "rush_day": None,         # gameplay-day of the last shop rush / second duel
        "extra_duel_day": None,
        "auto_trade": True,
        "traded_at": 0,
        "travel_to": None,   # a LANDMARKS key while the player has it walking there (!idle travel)
        "hp": HP_BASE,
        "mob_kills": 0,
        "mob_deaths": 0,
        "gamble_town": None,      # the town visit the budget below belongs to
        "gamble_visit_at": 0,
        "gamble_budget": 0,
        "gambles": 0,
        "gamble_won": 0,
        "gamble_lost": 0,
    }


def is_paused(char: dict) -> bool:
    return char["next_level_at"] is None


def time_left(char: dict, now: int) -> int:
    if is_paused(char):
        return char["remaining"]
    return max(0, char["next_level_at"] - now)


def shift(char: dict, seconds: int) -> None:
    """Move the level-up `seconds` later (negative = sooner), paused or not."""
    if is_paused(char):
        char["remaining"] = max(0, char["remaining"] + seconds)
    else:
        char["next_level_at"] += seconds


def scale(char: dict, now: int, pct: float) -> int:
    """Shift by `pct` percent of the time left; returns the seconds moved."""
    seconds = int(time_left(char, now) * pct / 100)
    shift(char, seconds)
    return seconds


def pause(char: dict, at: int) -> None:
    if not is_paused(char):
        char["remaining"] = max(0, char["next_level_at"] - at)
        char["next_level_at"] = None


def resume(char: dict, now: int) -> None:
    if is_paused(char):
        char["next_level_at"] = now + char["remaining"]
        char["remaining"] = None


def voice_speedup(char: dict, seconds: int) -> None:
    """`seconds` of running clock spent in voice: take the bonus share off."""
    if not is_paused(char):
        shift(char, -(seconds * VOICE_BONUS_PCT // 100))


def voice_gold_bonus(gained: int) -> int:
    return gained * VOICE_BONUS_PCT // 100 if gained > 0 else 0


def logged_in(char: dict, now: int) -> bool:
    return now - char["last_seen"] < GRACE_SECS


def level_up(char: dict) -> None:
    """The next clock starts where the last one ran out, not at `now`, so a
    tick that arrives late (or a boot after downtime) loses nobody any time."""
    char["level"] += 1
    char["next_level_at"] += ttl(char["level"], char["prestige"])


def do_prestige(char: dict, now: int) -> None:
    char["prestige"] += 1
    char["level"] = 0
    char["items"] = {}
    fresh = ttl(0, char["prestige"])
    if is_paused(char):
        char["remaining"] = fresh
    else:
        char["next_level_at"] = now + fresh


def item_sum(char: dict) -> int:
    return sum(item["level"] for item in char["items"].values())


def battle_sum(char: dict) -> int:
    total = item_sum(char)
    if char["moral"] == "good":
        return int(total * 1.1)
    if char["moral"] == "evil":
        return int(total * 0.9)
    return total


def rank_key(now: int):
    """Sort key for the leaderboard: prestige, then level, then who levels next."""
    return lambda pair: (-pair[1]["prestige"], -pair[1]["level"], time_left(pair[1], now), pair[0])


def ladder(chars: dict, now: int, limit: int) -> list:
    """The top `limit` (uid, character) pairs — `!idle top` and `!lb idle`."""
    return sorted(chars.items(), key=rank_key(now))[:limit]


def running(chars: dict) -> list:
    return [uid for uid, c in chars.items() if not is_paused(c)]


# ── alignment ────────────────────────────────────────────────────────────────

def alignment_label(char: dict) -> str:
    if char["law"] == "neutral" and char["moral"] == "neutral":
        return "true neutral"
    return f"{char['law']} {char['moral']}"


ALIGNMENTS = tuple((law, moral) for law in LAWS for moral in MORALS)


def parse_alignment(text: str) -> "tuple[str, str] | None":
    """`lawful good`, `chaotic`, `evil`, `neutral`, `true neutral` → (law, moral)."""
    words = text.lower().split()
    if words in (["neutral"], ["true", "neutral"], ["neutral", "neutral"]):
        return ("neutral", "neutral")
    if len(words) == 1:
        if words[0] in LAWS:
            return (words[0], "neutral")
        if words[0] in MORALS:
            return ("neutral", words[0])
    if len(words) == 2 and words[0] in LAWS and words[1] in MORALS:
        return (words[0], words[1])
    return None


# ── penalties ────────────────────────────────────────────────────────────────

def penalize(char: dict, base: int, now: int) -> int:
    seconds = int(base * PENALTY_MULT ** char["level"])
    shift(char, seconds)
    char["penalty_total"] += seconds
    char["last_penalty_at"] = now
    return seconds


# ── items ────────────────────────────────────────────────────────────────────

def roll_item_level(level: int, rng) -> int:
    """The classic roll: every item level up to 1.5× the character's gets a
    shrinking chance, and the highest one that hits wins."""
    best = 1
    for num in range(1, int(level * 1.5) + 1):
        if rng.random() * 1.4 ** (num / 4) < 1:
            best = num
    return best


def find_item(uid: int, char: dict, rng, name: NameFn) -> Note:
    for min_level, slot, unique, low, high in UNIQUES:
        if char["level"] < min_level or rng.randrange(UNIQUE_ODDS):
            continue
        found = rng.randint(low, high)
        if found > char["items"].get(slot, {}).get("level", 0):
            char["items"][slot] = {"level": found, "name": unique}
            return Note((uid,), f"✨ {name(uid)} unearthed the **{unique}** — a level {found} {slot}!", True)
    slot = rng.choice(ITEM_SLOTS)
    found = roll_item_level(char["level"], rng)
    held = char["items"].get(slot, {}).get("level", 0)
    if found > held:
        char["items"][slot] = {"level": found, "name": None}
        return Note((uid,), f"{name(uid)} found a level {found} {slot} (was level {held}).")
    return Note((uid,), f"{name(uid)} found a level {found} {slot}, but their level {held} one is better.")


def _item_label(slot: str, item: dict) -> str:
    return item.get("name") or f"level {item['level']} {slot}"


def _steal(thief: dict, victim: dict, rng) -> "str | None":
    """Swap one slot if the victim's is better; returns what was taken."""
    slot = rng.choice(ITEM_SLOTS)
    theirs = victim["items"].get(slot)
    mine = thief["items"].get(slot)
    if theirs is None or theirs["level"] <= (mine or {"level": 0})["level"]:
        return None
    thief["items"][slot] = theirs
    if mine is None:
        del victim["items"][slot]
    else:
        victim["items"][slot] = mine
    return _item_label(slot, theirs)


# ── battles ──────────────────────────────────────────────────────────────────

def axis_gap(a: int, b: int) -> int:
    """Squares between two coordinates on one wrapped axis, the short way.
    Only two characters meeting measure like this: they really do walk off
    one edge onto the other. Places do not — a town sits where it is drawn,
    and Denmark is reached from its own corner, not around the back."""
    gap = abs(a - b) % MAP_SPAN
    return min(gap, MAP_SPAN - gap)


def in_battle_range(a: dict, b: dict) -> bool:
    """Close enough on the map to meet. A character the tick has not placed
    yet (`ensure_position` runs in `move_players`) is out of everyone's
    reach until it has one."""
    if a.get("x") is None or b.get("x") is None:
        return False
    return axis_gap(a["x"], b["x"]) ** 2 + axis_gap(a["y"], b["y"]) ** 2 <= BATTLE_RANGE ** 2


def _rolled(roll: int, power: int) -> str:
    """How a fighter's roll reads in the feed. The IRC bot printed a bare
    `[roll/power]`, which nobody could decode."""
    return f"(rolled {roll} of {power})" if power else "(no gear)"


# A fight is one roll a side, which at low power is close to a coin flip —
# and the IRC game staked a flat 7%+ of a level on it. With dozens of players
# that evens out; with two, every swing lands on the same pair. So the stake
# follows the margin: a win by a hair moves MARGIN_FLOOR of it, and only a win
# by half the bigger side's range (or more) moves all of it.
MARGIN_FLOOR = 0.25
MARGIN_FULL_AT = 0.5
# Nobody is picked as a level-up opponent twice inside this window; the house
# steps in instead. In a two-player server the pool is one person, and early
# levels come every few minutes — without it one player is fought constantly.
# An hour: three starved a seven-player server of opponents by its second hour.
CHALLENGED_COOLDOWN_SECS = 3600


def margin_factor(my_roll: int, opp_roll: int, my_sum: int, opp_sum: int) -> float:
    reach = max(my_sum, opp_sum, 1) * MARGIN_FULL_AT
    return max(MARGIN_FLOOR, min(1.0, abs(my_roll - opp_roll) / reach))


def _how(factor: float) -> str:
    return "narrowly " if factor <= 0.5 else ""


def level_up_battle(uid: int, chars: dict, rng, name: NameFn, now: int, pace: Pace = CLASSIC) -> "list[Note]":
    me = chars[uid]
    if me["level"] < BATTLE_ALWAYS_LEVEL and rng.random() >= pace.battle_chance_below:
        return []
    pool = [
        u for u in running(chars)
        if u != uid and now - chars[u].get("challenged_at", 0) >= CHALLENGED_COOLDOWN_SECS
        and in_battle_range(me, chars[u])
    ]
    # The house is one more contender, so a lone player still gets fights.
    pick = rng.randrange(len(pool) + 1)
    opp_uid = pool[pick] if pick < len(pool) else None
    opp = chars.get(opp_uid)
    if opp is None:
        # The house is the challenger's own match. The IRC bot sized it to the
        # best-geared player, so in a small server whoever was behind fought
        # their rival's gear in every house fight as well.
        opp_sum = max(battle_sum(me), 1)
        opp_name, win_pct, lose_pct = HOUSE_NAME, 20, 10
    else:
        opp["challenged_at"] = now   # in memory only: a reboot forgetting it is harmless
        opp_sum = battle_sum(opp)
        opp_name = name(opp_uid)
        win_pct, lose_pct = max(opp["level"] / 4, 7), max(opp["level"] / 7, 7)

    my_sum = battle_sum(me)
    my_roll, opp_roll = rng.randint(0, my_sum), rng.randint(0, opp_sum)
    involved = (uid,) if opp is None else (uid, opp_uid)
    # A fight with the house concerns nobody else — it stays in the player's
    # own feed. Only a fight between two players is channel news.
    news = opp is not None
    head = f"⚔️ {name(uid)} {_rolled(my_roll, my_sum)} challenged {opp_name} {_rolled(opp_roll, opp_sum)}"
    factor = margin_factor(my_roll, opp_roll, my_sum, opp_sum)
    if my_roll < opp_roll:
        lost = scale(me, now, lose_pct * factor)
        return [Note(involved, f"{head} and {_how(factor)}lost. {format_duration(lost)} added to their clock.", news)]

    won = -scale(me, now, -win_pct * factor)
    prize = GOLD_HOUSE_WIN if opp is None else GOLD_PER_WIN * max(opp["level"], 1)
    me["gold"] += prize
    notes = [Note(involved, f"{head} and {_how(factor)}won! {format_duration(won)} off their clock, and {prize:,} gold.", news)]
    if opp is None:
        return notes
    if not rng.randrange(CRIT_ODDS[me["moral"]]):
        hurt = scale(opp, now, rng.randint(5, 25))
        notes.append(Note(involved, f"💥 A critical strike! {opp_name} is set back {format_duration(hurt)}.", True))
    if rng.random() < STEAL_CHANCE:
        taken = _steal(me, opp, rng)
        if taken:
            notes.append(Note(involved, f"🫳 In the chaos, {name(uid)} made off with {opp_name}'s {taken}!", True))
    return notes


def duel(uid: int, target_uid: int, chars: dict, rng, name: NameFn, now: int,
         wager: int = 0) -> "tuple[list[str], list[Note]]":
    """A duel fought blow by blow: (the story, the notes). The loser hands
    DUEL_PCT of their remaining time to the winner and the wager with it.

    Hit points here are for the telling only — both walk away as whole as
    they arrived. A duel is a match, not a mugging, and an unwagered one
    needs no consent: leaving real wounds would make it a way to send a
    rival into the wilds half dead.
    """
    power = {u: max(battle_sum(chars[u]), 1) for u in (uid, target_uid)}
    body = {u: max_hp(chars[u]) for u in (uid, target_uid)}
    left, dealt = dict(body), {uid: 0, target_uid: 0}
    story = [
        f"🤺 {name(uid)} ({power[uid]} power) squares up to {name(target_uid)} ({power[target_uid]} power) — "
        f"first to drop, or the most damage after {DUEL_MAX_ROUNDS} rounds."
    ]

    fought = 0
    for fought in range(1, DUEL_MAX_ROUNDS + 1):
        blows = []
        for striker, victim in ((uid, target_uid), (target_uid, uid)):
            if min(left.values()) <= 0:
                continue          # the round ends the moment someone goes down
            hit, mark = _blow(power[striker], power[victim], body[victim], rng)
            left[victim] -= hit
            dealt[striker] += hit
            if mark == "·":
                blows.append(f"{name(striker)} swings and misses")
            elif mark:
                blows.append(f"{name(striker)} lands a critical strike for **{hit}**")
            else:
                blows.append(f"{name(striker)} hits for **{hit}**")
        story.append(
            f"**Round {fought}** — {'; '.join(blows)}. "
            f"`{max(left[uid], 0)}/{body[uid]}` vs `{max(left[target_uid], 0)}/{body[target_uid]}`"
        )
        if min(left.values()) <= 0:
            break

    if left[uid] <= 0 or left[target_uid] <= 0:
        w_uid = target_uid if left[uid] <= 0 else uid
        how = f"{name(target_uid if w_uid == uid else uid)} goes down in round {fought}"
    elif dealt[uid] != dealt[target_uid]:
        w_uid = uid if dealt[uid] > dealt[target_uid] else target_uid
        how = f"{DUEL_MAX_ROUNDS} rounds, decided on damage — {dealt[w_uid]} to {dealt[target_uid if w_uid == uid else uid]}"
    else:
        w_uid = uid if rng.random() < 0.5 else target_uid
        how = "dead even after every round, so a coin toss settled it"
    l_uid = target_uid if w_uid == uid else uid

    stake = scale(chars[l_uid], now, DUEL_PCT)
    shift(chars[w_uid], -min(stake, time_left(chars[w_uid], now)))
    chars[l_uid]["gold"] -= wager
    chars[w_uid]["gold"] += wager
    staked = f" and the {wager:,} gold on the table" if wager else ""
    return story, [Note(
        (uid, target_uid),
        f"🤺 {name(uid)} duelled {name(target_uid)} — {how}. {name(w_uid)} wins, taking "
        f"{format_duration(stake)} off {name(l_uid)}'s clock{staked}.",
        True,
    )]


def team_battle(chars: dict, rng, name: NameFn, now: int, pace: Pace = CLASSIC) -> "list[Note]":
    pool = running(chars)
    size = min(TEAM_SIZE, len(pool) // 2)
    if size < pace.min_team_size:
        return []
    picked = rng.sample(pool, size * 2)
    teams = picked[:size], picked[size:]
    sums = [sum(battle_sum(chars[u]) for u in team) for team in teams]
    rolls = [rng.randint(0, s) for s in sums]
    win, lose = (0, 1) if rolls[0] >= rolls[1] else (1, 0)
    # Each side moves by 20% of its own shortest clock.
    gain = int(min(time_left(chars[u], now) for u in teams[win]) * 0.2)
    loss = int(min(time_left(chars[u], now) for u in teams[lose]) * 0.2)
    for u in teams[win]:
        shift(chars[u], -min(gain, time_left(chars[u], now)))
    for u in teams[lose]:
        shift(chars[u], loss)

    def _side(i):
        return ", ".join(name(u) for u in teams[i]) + f" {_rolled(rolls[i], sums[i])}"
    return [Note(
        tuple(picked),
        f"🛡️ Team battle! {_side(win)} beat {_side(lose)}. "
        f"Winners gain {format_duration(gain)}; losers are set back {format_duration(loss)}.",
        True,
    )]


# ── random events ────────────────────────────────────────────────────────────

def hand_of_god(uid: int, chars: dict, rng, name: NameFn, now: int) -> Note:
    char = chars[uid]
    pct = rng.randint(5, 75)
    if rng.random() < 0.8:
        moved = -scale(char, now, -pct)
        return Note((uid,), f"🙌 The Hand of God lifts {name(uid)} {format_duration(moved)} closer to level {char['level'] + 1}!", True)
    moved = scale(char, now, pct)
    return Note((uid,), f"🔥 The Hand of God swats {name(uid)} {format_duration(moved)} away from level {char['level'] + 1}.", True)


def _item_event(uid: int, char: dict, rng, name: NameFn, good: bool) -> "Note | None":
    if not char["items"]:
        return None
    slot = rng.choice(sorted(char["items"]))
    item = char["items"][slot]
    before = item["level"]
    item["level"] = max(1, int(before * (1.1 if good else 0.9)))
    lead = rng.choice(_ITEM_BOONS if good else _ITEM_BANES)
    verb = "gains" if good else "loses"
    return Note((uid,), f"{'🌟' if good else '🌧️'} {lead} {name(uid)}'s {_item_label(slot, item)}: it {verb} 10% ({before} → {item['level']}).")


def godsend(uid: int, chars: dict, rng, name: NameFn, now: int) -> Note:
    char = chars[uid]
    if rng.random() < 0.1:
        note = _item_event(uid, char, rng, name, good=True)
        if note:
            return note
    elif rng.random() < GOLD_EVENT_CHANCE:
        purse = GOLD_GODSEND_PER_LEVEL * max(char["level"], 1)
        char["gold"] += purse
        return Note((uid,), f"🌟 {name(uid)} found a purse somebody dropped in a hurry: {purse:,} gold.")
    moved = -scale(char, now, -rng.randint(5, 12))
    return Note((uid,), f"🌟 {name(uid)} {rng.choice(_GODSENDS)}. {format_duration(moved)} off their clock.")


def calamity(uid: int, chars: dict, rng, name: NameFn, now: int) -> Note:
    char = chars[uid]
    if rng.random() < 0.1:
        note = _item_event(uid, char, rng, name, good=False)
        if note:
            return note
    elif char["gold"] and rng.random() < GOLD_EVENT_CHANCE:
        lost = max(1, char["gold"] * rng.randint(5, 10) // 100)
        char["gold"] -= lost
        return Note((uid,), f"🌧️ {name(uid)} was pickpocketed at a crossroads fair: {lost:,} gold gone.")
    moved = scale(char, now, rng.randint(5, 12))
    return Note((uid,), f"🌧️ {name(uid)} {rng.choice(_CALAMITIES)}. {format_duration(moved)} added to their clock.")


def _blessing(uid: int, chars: dict, rng, name: NameFn, now: int) -> "Note | None":
    others = [u for u in running(chars) if u != uid and chars[u]["moral"] == "good"]
    if not others:
        return None
    friend = rng.choice(others)
    pct = rng.randint(5, 12)
    for u in (uid, friend):
        scale(chars[u], now, -pct)
    return Note((uid, friend), f"🕊️ {name(uid)} and {name(friend)} prayed together. Both are {pct}% closer to their next level.")


def _temptation(uid: int, chars: dict, rng, name: NameFn, now: int) -> Note:
    marks = [u for u in running(chars) if u != uid and chars[u]["moral"] == "good"]
    if marks and rng.random() < 0.5:
        mark = rng.choice(marks)
        taken = _steal(chars[uid], chars[mark], rng)
        if taken:
            return Note((uid, mark), f"🦹 {name(uid)} crept into {name(mark)}'s camp and stole their {taken}!", True)
        return Note((uid,), f"🦹 {name(uid)} rifled through {name(mark)}'s pack and found nothing worth taking.")
    moved = scale(chars[uid], now, rng.randint(1, 5))
    return Note((uid,), f"🦹 {name(uid)} was forsaken by their dark patron. {format_duration(moved)} added to their clock.")


def random_events(uid: int, chars: dict, rng, name: NameFn, now: int, ticks_per_day: int,
                  pace: Pace = CLASSIC) -> "list[Note]":
    """One tick's worth of luck for one running character."""
    char = chars[uid]
    factor = LAW_EVENT_FACTOR[char["law"]] / ticks_per_day
    notes = []
    if rng.random() < pace.hand_of_god_per_day / ticks_per_day:
        notes.append(hand_of_god(uid, chars, rng, name, now))
    if rng.random() < pace.godsend_per_day * factor:
        notes.append(godsend(uid, chars, rng, name, now))
    if rng.random() < pace.calamity_per_day * factor:
        notes.append(calamity(uid, chars, rng, name, now))
    if char["moral"] == "good" and rng.random() < BLESSING_PER_DAY / ticks_per_day:
        notes.append(_blessing(uid, chars, rng, name, now))
    if char["moral"] == "evil" and rng.random() < TEMPTATION_PER_DAY / ticks_per_day:
        notes.append(_temptation(uid, chars, rng, name, now))
    return [n for n in notes if n]


# ── the shop ─────────────────────────────────────────────────────────────────
#
# Every purchase is synchronous: the price is checked and taken in the same
# breath as the effect, so two racing !idle shop commands can't both spend
# the same gold. Each returns (bought, what to tell the player).

def level_gold(level: int) -> int:
    return GOLD_PER_LEVEL * level


def shop_prices(char: dict) -> dict:
    level = max(char["level"], 1)
    return {
        "find": PRICE_FIND_PER_LEVEL * level,
        "rush": PRICE_RUSH_PER_LEVEL * level,
        "duel": PRICE_SECOND_DUEL_PER_LEVEL * level,
        "class": PRICE_CLASS,
    }


def sharpen_price(item: dict) -> int:
    return PRICE_SHARPEN_PER_ITEM_LEVEL * item["level"]


def _pay(char: dict, price: int) -> "str | None":
    """Take the price, or say why not."""
    if char["gold"] < price:
        return f"That costs {price:,} gold and you have {char['gold']:,}."
    char["gold"] -= price
    return None


def buy_find(uid: int, char: dict, rng, name: NameFn) -> "tuple[bool, str]":
    broke = _pay(char, shop_prices(char)["find"])
    if broke:
        return False, broke
    return True, find_item(uid, char, rng, name).text


def buy_sharpen(char: dict, slot: str) -> "tuple[bool, str]":
    item = char["items"].get(slot)
    if item is None:
        return False, f"You have no {slot} to sharpen."
    broke = _pay(char, sharpen_price(item))
    if broke:
        return False, broke
    before = item["level"]
    item["level"] = before + max(1, before * SHARPEN_PCT // 100)
    return True, f"Your {_item_label(slot, {**item, 'level': before})} is now level {item['level']}."


def buy_rush(char: dict, now: int, today: str) -> "tuple[bool, str]":
    if char["rush_day"] == today:
        return False, "You've already rushed today."
    broke = _pay(char, shop_prices(char)["rush"])
    if broke:
        return False, broke
    char["rush_day"] = today
    saved = -scale(char, now, -RUSH_PCT)
    return True, f"{format_duration(saved)} off your clock."


def buy_second_duel(char: dict, today: str) -> "tuple[bool, str]":
    if char["duel_day"] != today:
        return False, "You still have today's duel."
    if char["extra_duel_day"] == today:
        return False, "You've already bought a second duel today."
    broke = _pay(char, shop_prices(char)["duel"])
    if broke:
        return False, broke
    char["extra_duel_day"] = today
    char["duel_day"] = None
    return True, "You may duel once more today."


def buy_class(char: dict, class_name: str) -> "tuple[bool, str]":
    broke = _pay(char, PRICE_CLASS)
    if broke:
        return False, broke
    char["class"] = class_name
    return True, f"You are now a {class_name}."


def nearest_town(char: dict) -> "tuple[str, int] | None":
    """(town, distance in squares straight across the map), or None before
    the character has a position. Deliberately not the wrapped distance: a
    market belongs to the corner it is drawn in, and being one step off the
    far edge should not put you in Denmark's."""
    if char.get("x") is None:
        return None
    return min(
        ((town, int(((char["x"] - LANDMARKS[town][0]) ** 2 + (char["y"] - LANDMARKS[town][1]) ** 2) ** 0.5)) for town in TOWNS),
        key=lambda pair: pair[1],
    )


def market_in_reach(char: dict) -> "str | None":
    near = nearest_town(char)
    return near[0] if near and near[1] <= MARKET_RADIUS else None


def auto_trade(uid: int, char: dict, rng, name: NameFn, now: int) -> "Note | None":
    """The errand a character runs on its own in a town's centre: at most one
    find and one sharpening of its weakest item, inside half its purse.
    Timers, duels and names stay the player's to buy."""
    near = nearest_town(char)
    if (
        not char.get("auto_trade", True) or near is None or near[1] > TOWN_CORE_RADIUS
        or now - char.get("traded_at", 0) < AUTO_TRADE_COOLDOWN_SECS
    ):
        return None
    before = char["gold"]
    budget = before * AUTO_TRADE_BUDGET_PCT // 100
    done = []
    price = shop_prices(char)["find"]
    if price <= budget:
        budget -= price
        done.append(buy_find(uid, char, rng, name)[1])
    if char["items"]:
        slot = min(char["items"], key=lambda s: (char["items"][s]["level"], s))
        if sharpen_price(char["items"][slot]) <= budget:
            done.append(buy_sharpen(char, slot)[1].replace("Your ", "Their ", 1))
    if not done:
        return None   # too poor today — no stamp, so a fuller purse still trades this visit
    char["traded_at"] = now
    return Note((uid,), f"🏘️ {name(uid)} wandered into {near[0]} and did some trading. {' '.join(done)} "
                        f"{before - char['gold']:,} gold spent, {char['gold']:,} left.")


# ── hit points ───────────────────────────────────────────────────────────────
#
# sizzlorox/Idle-RPG-Bot's, and the reason ninety fights a day is playable: a
# character carries its wounds between fights, so a fight costs blood rather
# than clock. Only a kill moves the clock (a little) and only falling moves
# it much. Below a quarter of its body a character makes camp instead of
# fighting, so death takes a run of bad luck, not one bad roll.
#
# Their damage is `attack² / (attack + defence)` against a flat pool of hit
# points. Ours reads that as a *share* of the body it lands on, because item
# power here runs from 0 to several hundred while a body does not — without
# it a fight would last twenty rounds at level 1 and two at level 40.

HP_BASE = 100               # sizzlorox's 100 + 5·level…
HP_PER_LEVEL = 5
HP_PER_ITEM_POWER = 1       # …plus armour, so gear doesn't outgrow the body wearing it
HP_REGEN_DIVISOR = 40       # of a full body, per minute: whole again inside an hour
CAMP_HP_PCT = 25            # at or under this a character rests instead of fighting
CAMP_HEAL_PCT = 20

MOB_MAX_ROUNDS = 5          # sizzlorox's, per monster
EVEN_BLOW_PCT = 18          # an evenly matched blow costs this much of a full body
MOB_CRIT_ODDS = 12          # 1-in-N a round: ×1.75, as theirs
MOB_CRIT_MULT = 1.75
MOB_DODGE_ODDS = 12         # 1-in-N a round the blow misses altogether


def max_hp(char: dict) -> int:
    return HP_BASE + HP_PER_LEVEL * char["level"] + HP_PER_ITEM_POWER * item_sum(char)


def hp_of(char: dict) -> int:
    """Never more than the body can hold — losing gear shrinks it."""
    return max(0, min(int(char.get("hp", HP_BASE)), max_hp(char)))


def hp_pct(char: dict) -> int:
    return hp_of(char) * 100 // max(max_hp(char), 1)


def heal(char: dict, amount: int) -> int:
    """Returns what actually went in."""
    before = hp_of(char)
    char["hp"] = min(before + amount, max_hp(char))
    return char["hp"] - before


def regen_hp(char: dict) -> None:
    heal(char, max(1, max_hp(char) // HP_REGEN_DIVISOR))


def needs_rest(char: dict) -> bool:
    return hp_pct(char) <= CAMP_HP_PCT


def _blow(attack: int, defence: int, body: int, rng) -> "tuple[int, str]":
    """One strike: (damage, a mark for the round line). sizzlorox's curve,
    normalised so an even match costs EVEN_BLOW_PCT of a body. `body` is the
    character's own, for blows in both directions — it is the one currency
    wounds are counted in, and a monster's bulk is quoted in it too.
    Measuring a blow against its own victim would make every monster take
    the same five rounds however big it is."""
    if not rng.randrange(MOB_DODGE_ODDS):
        return 0, "·"
    attack = max(1, attack)
    swing = max(1, rng.randint(attack // 2, attack))
    share = swing * swing / (swing + max(defence, 1)) / max(swing, defence, 1)
    damage = max(1, round(body * EVEN_BLOW_PCT / 100 * share / 0.5))
    if not rng.randrange(MOB_CRIT_ODDS):
        return round(damage * MOB_CRIT_MULT), "✳"
    return damage, ""


def monster_hp(char: dict, tier: int, strength: float) -> int:
    """Monsters are bulky in proportion to the character they meet, so a rat
    falls in a round at any level and the worst things in the realm cannot be
    put down inside five."""
    bulk = (0.08 + 0.10 * tier) * strength / 1.2
    return max(1, round(max_hp(char) * bulk))


def fight_monster(char: dict, their_power: int, their_hp: int, rng) -> "tuple[int, bool, str]":
    """Up to MOB_MAX_ROUNDS of blows both ways. Returns (rounds, the monster
    fell, a compact record of the exchange). The character's own hit points
    are spent in place."""
    my_power, my_max = max(battle_sum(char), 1), max_hp(char)
    marks = []
    for rounds in range(1, MOB_MAX_ROUNDS + 1):
        dealt, mark = _blow(my_power, their_power, my_max, rng)
        their_hp -= dealt
        marks.append(mark)
        if their_hp <= 0:
            return rounds, True, "".join(marks)
        taken, mark = _blow(their_power, my_power, my_max, rng)
        char["hp"] = hp_of(char) - taken
        marks.append(mark.lower())
        if char["hp"] <= 0:
            return rounds, False, "".join(marks)
    return MOB_MAX_ROUNDS, False, "".join(marks)

# ── monsters ─────────────────────────────────────────────────────────────────
#
# The encounter design is sizzlorox/Idle-RPG-Bot's (MIT): a monster is a
# rarity prefix plus a type ("Veteran Goblin"), types spawn by biome, a
# higher level opens the rarer ones, one fight in four is against a group,
# mobs are scaled to the player they meet, towns are safe, and a beaten
# character is carried back to one. That bot fights with hit points and five
# stats, which this game doesn't have, so a fight here is this game's usual
# roll — each side up to its power — and the stakes are clock and gold.

# The realm's regions, as drawn on assets/idle_map.png. First match wins;
# everything else is Plains.
_BIOME_BOXES = (
    ("Darklands", lambda x, y: x + y >= 800),              # T'rnalvph's corner
    ("Coast", lambda x, y: x + y <= 110),                  # Denmark's corner
    ("Mountains", lambda x, y: 245 <= x <= 350 and 55 <= y <= 195),    # the Mountains of Qwok
    ("Caves", lambda x, y: 35 <= x <= 150 and 230 <= y <= 330),        # the Secret Passage to Bharash
    ("Mountains", lambda x, y: x <= 190 and y >= 260 and y - 260 >= (x - 40) * 0.6),   # the Great Shahlil mountains
    ("Haunted", lambda x, y: 150 <= x <= 360 and y >= 320),            # around the Towers of Ankh-Allor
    ("Forest", lambda x, y: 220 <= x <= 450 and 170 <= y <= 370),      # Velvragh's woods
)
DANGEROUS_BIOMES = frozenset({"Darklands", "Caves", "Haunted"})

# (name, power multiplier, gold multiplier, rarity) — a prefix is in the pool
# when its rarity is at least the 0..99 roll, so the top two always are and
# the last few need a roll near zero.
MOB_PREFIXES = (
    ("Starving", 0.5, 0.25, 100), ("Normal", 1.0, 1, 100), ("Veteran", 1.25, 2, 50),
    ("Elite", 1.5, 3, 30), ("Champion", 1.75, 4, 15), ("Legendary", 2.0, 5, 10),
    ("Undead", 2.5, 1, 5), ("Deadly", 2.75, 6, 4), ("Berserk", 3.0, 1, 3),
    ("Omega", 2.25, 6, 2), ("Corrupted", 3.25, 7, 2),
)

# (name, rarity, toughness, tier, biomes). Tier 1-5 sets the stakes: that
# percent of the clock either way, and the gold. The Rat lives everywhere at
# rarity 100, which is what keeps every pool non-empty.
_EVERYWHERE = ("Plains", "Coast", "Mountains", "Caves", "Haunted", "Forest", "Darklands")
MOB_TYPES = (
    ("Rat", 100, 0.5, 1, _EVERYWHERE),
    ("Slime", 100, 0.6, 1, ("Plains", "Forest")),
    ("Crab", 100, 0.6, 1, ("Coast",)),
    ("Bat", 90, 0.7, 1, ("Caves", "Haunted", "Forest", "Darklands")),
    ("Goblin", 90, 0.8, 2, ("Plains", "Forest", "Mountains")),
    ("Boar", 90, 0.8, 1, ("Plains", "Forest")),
    ("Bandit", 80, 0.9, 2, ("Plains", "Coast", "Forest", "Mountains")),
    ("Wraith", 80, 1.0, 2, ("Haunted", "Darklands")),
    ("Zombie", 75, 1.0, 2, ("Haunted", "Darklands")),
    ("Bugbear", 75, 1.0, 2, ("Plains", "Forest", "Mountains", "Caves")),
    ("Pirate", 65, 1.0, 2, ("Coast",)),
    ("Cyclops", 63, 1.3, 3, ("Plains", "Mountains")),
    ("Golem", 60, 1.4, 4, ("Mountains", "Caves", "Darklands")),
    ("Knight", 50, 1.2, 3, ("Plains", "Coast", "Forest", "Mountains", "Haunted")),
    ("Orc", 50, 1.2, 3, ("Plains", "Forest", "Mountains")),
    ("Necromancer", 50, 1.3, 3, ("Haunted", "Caves", "Darklands")),
    ("Giant Spider", 50, 1.1, 3, ("Forest", "Caves")),
    ("Griffin", 48, 1.3, 3, ("Plains", "Mountains")),
    ("Vampire", 40, 1.4, 4, ("Haunted", "Darklands")),
    ("Banshee", 40, 1.3, 3, ("Haunted", "Darklands")),
    ("Werewolf", 35, 1.4, 4, ("Forest", "Mountains", "Darklands")),
    ("Water Spirit", 35, 1.2, 3, ("Coast",)),
    ("Ogre", 20, 1.5, 4, ("Plains", "Forest")),
    ("Gargoyle", 25, 1.5, 4, ("Haunted", "Mountains")),
    ("Mountain Troll", 25, 1.7, 5, ("Mountains",)),
    ("Cave Troll", 25, 1.7, 5, ("Caves",)),
    ("Dragon", 15, 1.8, 5, ("Mountains", "Darklands")),
    ("Basilisk", 10, 1.8, 5, ("Caves", "Darklands")),
)
# Killing one of these is channel news; every other fight stays in the feed.
RARE_KILLS = frozenset({"Dragon", "Basilisk", "Mountain Troll", "Cave Troll", "Golem"})

MOB_GROUP_CHANCE = 0.25
MOB_EASY_LEVEL = 5             # at or below it, monsters fight at half strength
MOB_DROP_CHANCE = 0.15         # a won fight turns up an item
MOB_GEAR_DAMAGE_CHANCE = 0.15  # a lost one dents one
MOB_DEATH_GOLD_DIVISOR = 12
MOB_WIN_CLOCK_DIVISOR = 10   # a kill is worth tier/10 % of the clock; falling still costs the whole tier
MOB_DEATH_GOLD_CAP_PER_LEVEL = 5    # …but never more than half a level-up's worth: a fat purse isn't bled dry
MOB_GOLD_PER_LEVEL = 0.4       # × tier × the prefix's gold multiplier
MOB_DANGER_GOLD_BONUS = 1.5    # the dangerous biomes pay for the risk
MOB_RESPAWN_DISTANCE = 40      # squares from the town's centre: inside the market ring, outside the errand's


def biome_at(x: int, y: int) -> str:
    for biome, inside in _BIOME_BOXES:
        if inside(x, y):
            return biome
    return "Plains"


def roll_monster(level: int, biome: str, rng) -> "tuple[str, str, float, float, int]":
    """(prefix, type, power multiplier, gold multiplier, tier). Two 0..99
    rolls: the first picks the prefix pool, and their sum — eased by half the
    character's level — picks the type pool, so a rare prefix tends to come
    with a rare beast and both open up with level. The dangerous biomes lean
    the rolls toward the rare end."""
    lean = 15 if biome in DANGEROUS_BIOMES else 0
    rarity_roll = max(0, rng.randrange(100) - lean)
    type_roll = max(0, rng.randrange(100) - lean)
    prefix = rng.choice([p for p in MOB_PREFIXES if p[3] >= rarity_roll])
    threshold = min(100, type_roll + rarity_roll - level / 2)
    beast = rng.choice([t for t in MOB_TYPES if t[1] >= threshold and biome in t[4]])
    return prefix[0], beast[0], prefix[1] * beast[2], prefix[2], beast[3]


def mob_power(char: dict, strength: float) -> int:
    """A monster is cut from the character it meets: most of their own power,
    bent by how strong its kind is."""
    base = max(battle_sum(char), char["level"] * 2, 4)
    power = base / 1.2 * (0.6 + 0.4 * strength)
    return max(1, int(power * (0.5 if char["level"] <= MOB_EASY_LEVEL else 1)))


def _to_town_outskirts(char: dict) -> str:
    """Carry a beaten character to the edge of the nearest town's market."""
    town, distance = nearest_town(char)
    tx, ty = LANDMARKS[town]
    if distance > MOB_RESPAWN_DISTANCE:
        pull = MOB_RESPAWN_DISTANCE / distance
        char["x"], char["y"] = int(tx + (char["x"] - tx) * pull), int(ty + (char["y"] - ty) * pull)
    return town


def mob_encounter(uid: int, char: dict, rng, name: NameFn, now: int) -> "list[Note]":
    """One encounter in the wild: a monster or, a quarter of the time from
    level 11, a group fought one after another until the character falls,
    breaks off, or has seen them all off."""
    if char.get("x") is None or market_in_reach(char):
        return []   # towns are safe ground
    if needs_rest(char):
        got = heal(char, max(1, max_hp(char) * CAMP_HEAL_PCT // 100))
        return [Note((uid,), f"⛺ {name(uid)} was in no state to fight and made camp. +{got} HP ({hp_of(char)}/{max_hp(char)}).")]

    biome = biome_at(char["x"], char["y"])
    count = 1
    if rng.random() < MOB_GROUP_CHANCE:
        count = rng.randint(1, int(char["level"] * 0.0912) + 1)

    slain, gold, saved, rare, fell_to, broke_off = [], 0, 0, False, None, None
    for _ in range(count):
        prefix, beast, strength, gold_mult, tier = roll_monster(char["level"], biome, rng)
        mob = f"{prefix} {beast}"
        their_power = mob_power(char, strength)
        rounds, killed, marks = fight_monster(char, their_power, monster_hp(char, tier, strength), rng)
        blows = f" `{marks}`" if marks.strip("·") else ""
        if hp_of(char) <= 0:
            fell_to = (mob, tier, rounds)
            break
        if not killed:
            broke_off = f"a {mob} shook {name(uid)} off after {rounds} rounds{blows}"
            break
        slain.append(f"{mob} ({rounds}r{blows})")
        bonus = MOB_DANGER_GOLD_BONUS if biome in DANGEROUS_BIOMES else 1
        gold += max(1, int(tier * gold_mult * max(char["level"], 1) * MOB_GOLD_PER_LEVEL * bonus))
        saved += -scale(char, now, -tier / MOB_WIN_CLOCK_DIVISOR)
        rare = rare or beast in RARE_KILLS

    char["gold"] += gold
    char["mob_kills"] = char.get("mob_kills", 0) + len(slain)
    body = f"{hp_of(char)}/{max_hp(char)} HP"
    where = f"[{biome}]"
    notes = []
    if slain:
        clock = f" {format_duration(saved)} off their clock," if saved else ""
        text = f"🗡️ {where} {name(uid)} killed a {', then a '.join(slain)}.{clock} {gold:,} gold. {body}."
        if broke_off:
            text += f" Then {broke_off}."
        notes.append(Note((uid,), text, rare))
        if fell_to is None and rng.random() < MOB_DROP_CHANCE:
            notes.append(find_item(uid, char, rng, name))
    elif broke_off:
        notes.append(Note((uid,), f"🗡️ {where} {broke_off[0].upper()}{broke_off[1:]}. {body}."))

    if fell_to is not None:
        mob, tier, rounds = fell_to
        lost_time = scale(char, now, tier)
        lost_gold = min(-(-char["gold"] // MOB_DEATH_GOLD_DIVISOR), MOB_DEATH_GOLD_CAP_PER_LEVEL * max(char["level"], 1))
        char["gold"] -= lost_gold
        char["mob_deaths"] = char.get("mob_deaths", 0) + 1
        dented = ""
        if char["items"] and rng.random() < MOB_GEAR_DAMAGE_CHANCE:
            slot = rng.choice(sorted(char["items"]))
            item = char["items"][slot]
            item["level"] = max(1, item["level"] * 9 // 10)
            dented = f" Their {_item_label(slot, item)} was dented in the fall."
        town = _to_town_outskirts(char)
        char["hp"] = max_hp(char)   # patched up on the way, as sizzlorox does
        purse = f" and {lost_gold:,} gold" if lost_gold else ""
        notes.append(Note(
            (uid,),
            f"☠️ {where} A {mob} struck {name(uid)} down in {rounds} rounds. {format_duration(lost_time)} added to their clock"
            f"{purse}; they were carried to the outskirts of {town} and patched up.{dented}",
        ))
    return notes


# ── the tables ───────────────────────────────────────────────────────────────
#
# Town gambling is sizzlorox/Idle-RPG-Bot's: a character in a town bets on
# its own, the stake a share of the purse that grows with it
# (2·ln(gold)·gold/100 — 9% of 100 gold, 18% of 10,000), at even money with
# the house a nose ahead. There it has no limit; here a visit's stakes stop
# at a fifth of the purse the character arrived with. It cannot be switched
# off — it is the town's tax on a hoard — and `!idle gamble` lets the player
# bet by hand on the same terms, in the same places.

GAMBLE_MIN_GOLD = 18
GAMBLE_LOSE_BELOW = 51          # of 100: the house wins 51 rolls in 100
GAMBLE_VISIT_CAP_PCT = 20
GAMBLE_VISIT_SECS = 6 * 3600    # a stay longer than this counts as a new visit
GAMBLES_PER_HOUR = 0.75         # automatic bets, while inside a market ring


def gamble_stake(gold: int) -> int:
    return int(2 * math.log(gold) * gold / 100) if gold > 1 else 0


def _settle_bet(char: dict, stake: int, rng) -> bool:
    """Even money, house edge GAMBLE_LOSE_BELOW - 50. Returns whether it won."""
    won = rng.randrange(100) >= GAMBLE_LOSE_BELOW
    char["gold"] += stake if won else -stake
    char["gambles"] += 1
    char["gamble_won" if won else "gamble_lost"] += stake
    return won


def town_gamble(uid: int, char: dict, rng, name: NameFn, now: int, ticks_per_hour: int) -> "Note | None":
    """One tick of a character's own gambling. The visit's budget is set on
    arriving in a town (or after GAMBLE_VISIT_SECS in the same one) — by the
    clock and the town's name, not by crossing the ring, which a wanderer at
    its edge does every few seconds."""
    town = market_in_reach(char)
    if town is None:
        return None
    if town != char["gamble_town"] or now - char["gamble_visit_at"] >= GAMBLE_VISIT_SECS:
        char["gamble_town"], char["gamble_visit_at"] = town, now
        char["gamble_budget"] = char["gold"] * GAMBLE_VISIT_CAP_PCT // 100
    if char["gold"] < GAMBLE_MIN_GOLD or rng.random() >= GAMBLES_PER_HOUR / ticks_per_hour:
        return None
    stake = min(gamble_stake(char["gold"]), char["gamble_budget"], char["gold"])
    if stake < 1:
        return None
    char["gamble_budget"] -= stake
    won = _settle_bet(char, stake, rng)
    return Note((uid,), f"🎲 [{town}] {name(uid)} sat down at the tables with {stake:,} gold and "
                        f"{'doubled it' if won else 'lost it'}. {char['gold']:,} gold left.")


def manual_gamble(char: dict, stake: int, rng) -> "tuple[bool | None, str]":
    """`!idle gamble`: (won, text), or (None, why not)."""
    town = market_in_reach(char)
    if town is None:
        return None, f"The tables are in the towns — you can only gamble within {MARKET_RADIUS} squares of one."
    if stake < 1:
        return None, "Bet at least 1 gold."
    if stake > char["gold"]:
        return None, f"You only have {char['gold']:,} gold."
    won = _settle_bet(char, stake, rng)
    outcome = f"won {stake:,} gold" if won else f"lost {stake:,} gold"
    return won, f"[{town}] You {outcome}. {char['gold']:,} gold left."


# ── the map ──────────────────────────────────────────────────────────────────
#
# Ported rule for rule from the IRC bot's moveplayers / collision_fight:
# once a second every running character steps -1, 0 or +1 on each axis, the
# grid wraps, and two characters landing on one square fight with a
# 1-in-(players online) chance. Questers on a journey don't wander — each
# has a 1% chance a second to take one step toward the current waypoint.

def ensure_position(char: dict, rng) -> None:
    if char.get("x") is None or char.get("y") is None:
        char["x"], char["y"] = rng.randrange(MAP_SPAN), rng.randrange(MAP_SPAN)


def _wander(value: int, rng) -> int:
    value += rng.randint(-1, 1)
    if value > MAP_SIZE:
        return 0
    if value < 0:
        return MAP_SIZE
    return value


def _toward(value: int, goal: int) -> int:
    """One step straight at the goal. A traveller walks the map as drawn and
    never takes the edge as a short cut — see nearest_town."""
    return value if value == goal else value + (1 if value < goal else -1)


def landmark_at(point) -> "str | None":
    return _LANDMARK_NAMES.get(tuple(point)) if point else None


def _place(point) -> str:
    label = landmark_at(point)
    return f"{label} [{point[0]}, {point[1]}]" if label else f"[{point[0]}, {point[1]}]"


def match_place(text: str) -> "str | None":
    """`velvragh`, `towers`, `shahlil`, `trnalvph` → the place on the map.
    When several match, a lone town among them wins (`qwok` is the land of
    Qwok, not its mountains); otherwise it is too vague."""
    wanted = re.sub(r"[^a-z ]", "", text.lower()).strip()
    hits = [place for place in LANDMARKS if wanted and wanted in re.sub(r"[^a-z ]", "", place.lower())]
    if len(hits) > 1:
        hits = [place for place in hits if place in TOWNS]
    return hits[0] if len(hits) == 1 else None


def travel_steps(char: dict, town: str) -> int:
    """Steps left to `town` straight across the map, matching what
    `nearest_town` reports. A step moves one square on both axes at once."""
    goal = LANDMARKS[town]
    return max(abs(char["x"] - goal[0]), abs(char["y"] - goal[1]))


def travel_eta_secs(char: dict, town: str) -> int:
    return int(travel_steps(char, town) / JOURNEY_STEP_CHANCE)


def collision_fight(uid: int, opp_uid: int, chars: dict, rng, name: NameFn, now: int) -> "list[Note]":
    me, opp = chars[uid], chars[opp_uid]
    my_sum, opp_sum = battle_sum(me), battle_sum(opp)
    my_roll = rng.randrange(my_sum) if my_sum else 0
    opp_roll = rng.randrange(opp_sum) if opp_sum else 0
    involved = (uid, opp_uid)
    head = f"⚔️ {name(uid)} {_rolled(my_roll, my_sum)} came upon {name(opp_uid)} {_rolled(opp_roll, opp_sum)} at [{me['x']}, {me['y']}]"
    factor = margin_factor(my_roll, opp_roll, my_sum, opp_sum)
    if my_roll < opp_roll:
        lost = scale(me, now, max(opp["level"] // 7, 7) * factor)
        return [Note(involved, f"{head} and was {_how(factor)}defeated. {format_duration(lost)} added to their clock.", True)]
    won = -scale(me, now, -max(opp["level"] // 4, 7) * factor)
    spoils = opp["gold"] * GOLD_SPOILS_PCT // 100
    opp["gold"] -= spoils
    prize = GOLD_PER_WIN * max(opp["level"], 1)
    me["gold"] += prize + spoils
    took = f", plus {spoils:,} from {name(opp_uid)}'s purse" if spoils else ""
    notes = [Note(involved, f"{head} and took them in combat! {format_duration(won)} off their clock, and {prize:,} gold{took}.", True)]
    if not rng.randrange(COLLISION_CRIT_ODDS):
        hurt = scale(opp, now, 5 + rng.randrange(20))
        notes.append(Note(involved, f"💥 A critical strike! {name(opp_uid)} is set back {format_duration(hurt)}.", True))
    elif not rng.randrange(COLLISION_STEAL_ODDS) and me["level"] >= COLLISION_STEAL_LEVEL:
        taken = _steal(me, opp, rng)
        if taken:
            notes.append(Note(involved, f"🫳 In the fierce battle {name(opp_uid)} dropped their {taken}, and {name(uid)} picked it up!", True))
    return notes


def _pay_questers(chars: dict, members, now: int) -> int:
    purse = GOLD_QUEST_PER_MEMBER * len(members)
    for u in members:
        scale(chars[u], now, -QUEST_REWARD_PCT)
        chars[u]["gold"] += purse
    return purse


def _journey_step(chars: dict, quest: dict, now: int, name: NameFn) -> "tuple[list, bool]":
    """One second of a journey's bookkeeping. Returns (notes, moved_on): when
    the party has just reached a waypoint the second is spent on that, as in
    the original, and nobody moves."""
    members = [u for u in quest["members"] if u in chars]
    if not members:
        return [], False
    goal = quest["p1"] if quest["stage"] == 1 else quest["p2"]
    if any((chars[u]["x"], chars[u]["y"]) != tuple(goal) for u in members):
        return [], False
    if quest["stage"] == 1:
        quest["stage"] = 2
        return [Note(tuple(members), f"🧭 {_names(members, name)} have reached {_place(quest['p1'])}. Onward to {_place(quest['p2'])}.", True)], True
    purse = _pay_questers(chars, members, now)
    _end_quest(quest, now + QUEST_REST_SECS)
    return [Note(tuple(members), f"🏆 {_names(members, name)} have completed their journey! Each is {QUEST_REWARD_PCT}% closer to their next level and {purse:,} gold richer.", True)], True


def move_players(chars: dict, quest: dict, rng, name: NameFn, now: int, seconds: int) -> "list[Note]":
    """`seconds` one-second steps of the map."""
    for char in chars.values():
        ensure_position(char, rng)
    online = running(chars)
    if not online:
        return []
    notes: list = []
    for _ in range(seconds):
        questers: list = []
        if quest.get("kind") == "journey":
            arrived, moved_on = _journey_step(chars, quest, now, name)
            notes += arrived
            if moved_on:
                continue
            questers = [u for u in quest["members"] if u in chars]

        # Who stands where this second, to spot two characters on one square.
        squares: dict = {}
        for uid in online:
            if uid in questers or uid not in chars:
                continue
            char = chars[uid]
            town = char.get("travel_to")
            if town in LANDMARKS:
                # A traveller walks like a quester — and, like one, meets nobody on the way.
                if rng.random() < JOURNEY_STEP_CHANCE:
                    goal = LANDMARKS[town]
                    char["x"], char["y"] = _toward(char["x"], goal[0]), _toward(char["y"], goal[1])
                    if (char["x"], char["y"]) == goal:
                        char["travel_to"] = None
                        notes.append(Note((uid,), f"🧭 {name(uid)} arrived {'in' if town in TOWNS else 'at'} {town}."))
                continue
            char["x"], char["y"] = _wander(char["x"], rng), _wander(char["y"], rng)
            spot = (char["x"], char["y"])
            held = squares.get(spot)
            if held is not None and not held["battled"]:
                if rng.random() * len(online) < 1:
                    held["battled"] = True
                    notes += collision_fight(uid, held["uid"], chars, rng, name, now)
            else:
                squares[spot] = {"uid": uid, "battled": False}

        goal = quest["p1"] if quest.get("stage") == 1 else quest.get("p2")
        for uid in questers:
            if rng.random() < JOURNEY_STEP_CHANCE:
                char = chars[uid]
                char["x"], char["y"] = _toward(char["x"], goal[0]), _toward(char["y"], goal[1])
    return notes


# ── quests ───────────────────────────────────────────────────────────────────

def new_quest() -> dict:
    return {
        "members": [], "description": "", "kind": None, "ends_at": None,
        "stage": 1, "p1": None, "p2": None, "not_before": 0,
    }


def quest_active(quest: dict) -> bool:
    return quest.get("kind") is not None


def _names(uids, name: NameFn) -> str:
    return ", ".join(name(u) for u in uids)


def _end_quest(quest: dict, not_before: int) -> None:
    quest.update(members=[], description="", kind=None, ends_at=None, stage=1, p1=None, p2=None, not_before=not_before)


def quest_eligible(chars: dict) -> list:
    return [
        uid for uid in running(chars)
        if chars[uid]["level"] >= QUEST_MIN_LEVEL
    ]


def tick_quest(chars: dict, quest: dict, rng, name: NameFn, now: int) -> "list[Note]":
    """Finish a vigil whose time is up, or start a quest when a party is
    ready. A journey finishes in `move_players`, when the party arrives."""
    if quest_active(quest):
        quest["members"] = [u for u in quest["members"] if u in chars]
        if not quest["members"]:
            _end_quest(quest, now)
            return []
        if quest["kind"] != "vigil" or now < quest["ends_at"]:
            return []
        members = tuple(quest["members"])
        purse = _pay_questers(chars, members, now)
        _end_quest(quest, now + QUEST_REST_SECS)
        return [Note(members, f"🏆 {_names(members, name)} completed their quest! Each is {QUEST_REWARD_PCT}% closer to their next level and {purse:,} gold richer.", True)]

    if now < quest.get("not_before", 0):
        return []
    eligible = quest_eligible(chars)
    if len(eligible) < QUEST_MIN_PARTY:
        return []
    members = rng.sample(eligible, min(QUEST_MAX_PARTY, len(eligible)))
    # Being picked is the one thing in the game worth a mention badge — and
    # only for someone who claimed their character. An enrolled member who
    # never asked to play is named, never mentioned.
    pinged = tuple(u for u in members if chars[u].get("claimed", True))
    called = ", ".join(f"<@{u}>" if u in pinged else name(u) for u in members)
    picked = rng.choice(_VIGILS + _JOURNEYS)   # every quest is equally likely, as in the original
    if isinstance(picked, str):
        quest.update(
            members=members, description=picked, kind="vigil",
            ends_at=now + rng.randint(QUEST_MIN_SECS, QUEST_MAX_SECS),
        )
        return [Note(
            tuple(members),
            f"📜 {called} have been chosen to {picked}. "
            f"It ends <t:{quest['ends_at']}:R>, and each of them comes back {QUEST_REWARD_PCT}% closer to their next level.",
            True,
            pinged,
        )]
    start, end, text = picked
    for u in members:
        chars[u]["travel_to"] = None   # the quest decides where they walk now
    quest.update(
        members=members, description=text, kind="journey", ends_at=None,
        stage=1, p1=list(LANDMARKS[start]), p2=list(LANDMARKS[end]),
    )
    return [Note(
        tuple(members),
        f"📜 {called} have been chosen to {text}. They must first reach {_place(quest['p1'])}, "
        f"then {_place(quest['p2'])} — `!idle map` follows their journey. "
        f"Each of them comes back {QUEST_REWARD_PCT}% closer to their next level.",
        True,
        pinged,
        True,
    )]
