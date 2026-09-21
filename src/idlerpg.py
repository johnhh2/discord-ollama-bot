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

from typing import Callable, NamedTuple

from src.helpers import format_duration

# ── the curve ────────────────────────────────────────────────────────────────

BASE_TTL = 600           # seconds from level 0 to level 1
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

ALIGN_COOLDOWN_SECS = 86_400
DUEL_PCT = 5

# ── per-day odds (the cog divides by its ticks per day) ──────────────────────

GODSEND_PER_DAY = 1 / 8
CALAMITY_PER_DAY = 1 / 8
HAND_OF_GOD_PER_DAY = 1 / 20
BLESSING_PER_DAY = 1 / 12      # good characters
TEMPTATION_PER_DAY = 1 / 8     # evil characters
TEAM_BATTLE_PER_DAY = 1 / 4    # per guild
# Lawful characters live quieter lives, chaotic ones louder — for good and ill.
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

# The map: coordinates run 0..MAP_SIZE on both axes and wrap at the edges.
MAP_SIZE = 500
JOURNEY_STEP_CHANCE = 0.01     # per quester per second
COLLISION_CRIT_ODDS = 35       # 1-in-N on a won collision fight
COLLISION_STEAL_ODDS = 25      # …else 1-in-N to swap an item, from COLLISION_STEAL_LEVEL up
COLLISION_STEAL_LEVEL = 20

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
LANDMARKS = {
    "Afkhold Keep": (90, 120),
    "the Pinged Plains": (280, 90),
    "Dozing Dragon Pass": (410, 80),
    "Port Brb": (40, 270),
    "the Great Library of Hush": (250, 250),
    "the Muted Mountains": (460, 300),
    "the Sunken Tower": (130, 400),
    "Lurker's Fen": (330, 430),
}
_LANDMARK_NAMES = {point: label for label, point in LANDMARKS.items()}

# (first waypoint, second waypoint, what the party was chosen to do)
_JOURNEYS = (
    ("Afkhold Keep", "Dozing Dragon Pass", "carry the keep's last lantern to the dragon's door without waking it"),
    ("Port Brb", "the Great Library of Hush", "return a book that is four centuries overdue, and face the librarian"),
    ("the Sunken Tower", "the Muted Mountains", "haul the tower's drowned bell up to where nobody will hear it"),
    ("Lurker's Fen", "the Pinged Plains", "lead the fen's lost sheep home across the plains"),
    ("the Pinged Plains", "Port Brb", "deliver an urgent message that stopped being urgent a week ago"),
    ("the Muted Mountains", "Afkhold Keep", "escort a very old king home from a very long holiday"),
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


def new_character(class_name: str, now: int) -> dict:
    return {
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
        return Note((uid,), f"🎒 {name(uid)} found a level {found} {slot} (was level {held}).")
    return Note((uid,), f"🎒 {name(uid)} found a level {found} {slot}, but their level {held} one is better.")


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

def _rolled(roll: int, power: int) -> str:
    """How a fighter's roll reads in the feed. The IRC bot printed a bare
    `[roll/power]`, which nobody could decode."""
    return f"(rolled {roll} of {power})" if power else "(no gear)"


def level_up_battle(uid: int, chars: dict, rng, name: NameFn, now: int) -> "list[Note]":
    me = chars[uid]
    if me["level"] < BATTLE_ALWAYS_LEVEL and rng.random() >= BATTLE_CHANCE_BELOW:
        return []
    pool = [u for u in running(chars) if u != uid]
    # The house is one more contender, so a lone player still gets fights.
    pick = rng.randrange(len(pool) + 1)
    opp_uid = pool[pick] if pick < len(pool) else None
    opp = chars.get(opp_uid)
    if opp is None:
        opp_sum = max(item_sum(c) for c in chars.values()) + 1
        opp_name, win_pct, lose_pct = HOUSE_NAME, 20, 10
    else:
        opp_sum = battle_sum(opp)
        opp_name = name(opp_uid)
        win_pct, lose_pct = max(opp["level"] / 4, 7), max(opp["level"] / 7, 7)

    my_sum = battle_sum(me)
    my_roll, opp_roll = rng.randint(0, my_sum), rng.randint(0, opp_sum)
    involved = (uid,) if opp is None else (uid, opp_uid)
    head = f"⚔️ {name(uid)} {_rolled(my_roll, my_sum)} challenged {opp_name} {_rolled(opp_roll, opp_sum)}"
    if my_roll < opp_roll:
        lost = scale(me, now, lose_pct)
        return [Note(involved, f"{head} and lost. {format_duration(lost)} added to their clock.", True)]

    won = -scale(me, now, -win_pct)
    notes = [Note(involved, f"{head} and won! {format_duration(won)} off their clock.", True)]
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


def duel(uid: int, target_uid: int, chars: dict, rng, name: NameFn, now: int) -> "list[Note]":
    """The loser hands DUEL_PCT of their remaining time to the winner."""
    a, b = chars[uid], chars[target_uid]
    a_sum, b_sum = battle_sum(a), battle_sum(b)
    a_roll, b_roll = rng.randint(0, a_sum), rng.randint(0, b_sum)
    # A tie is a coin toss, not the challenger's: a duel needs no consent, and
    # two gearless characters always tie — the IRC rule would hand every new
    # player a free win over any other.
    a_wins = a_roll > b_roll or (a_roll == b_roll and rng.random() < 0.5)
    (w_uid, winner), (l_uid, loser) = ((uid, a), (target_uid, b)) if a_wins else ((target_uid, b), (uid, a))
    stake = scale(loser, now, DUEL_PCT)
    tied = " It was dead even, so a coin toss settled it." if a_roll == b_roll else ""
    shift(winner, -min(stake, time_left(winner, now)))
    return [Note(
        (uid, target_uid),
        f"🤺 {name(uid)} {_rolled(a_roll, a_sum)} duelled {name(target_uid)} {_rolled(b_roll, b_sum)}.{tied} "
        f"{name(w_uid)} wins and takes {format_duration(stake)} off {name(l_uid)}'s clock.",
        True,
    )]


def team_battle(chars: dict, rng, name: NameFn, now: int) -> "list[Note]":
    pool = running(chars)
    if len(pool) < TEAM_SIZE * 2:
        return []
    picked = rng.sample(pool, TEAM_SIZE * 2)
    teams = picked[:TEAM_SIZE], picked[TEAM_SIZE:]
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
    moved = -scale(char, now, -rng.randint(5, 12))
    return Note((uid,), f"🌟 {name(uid)} {rng.choice(_GODSENDS)}. {format_duration(moved)} off their clock.")


def calamity(uid: int, chars: dict, rng, name: NameFn, now: int) -> Note:
    char = chars[uid]
    if rng.random() < 0.1:
        note = _item_event(uid, char, rng, name, good=False)
        if note:
            return note
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


def random_events(uid: int, chars: dict, rng, name: NameFn, now: int, ticks_per_day: int) -> "list[Note]":
    """One tick's worth of luck for one running character."""
    char = chars[uid]
    factor = LAW_EVENT_FACTOR[char["law"]] / ticks_per_day
    notes = []
    if rng.random() < HAND_OF_GOD_PER_DAY * factor:
        notes.append(hand_of_god(uid, chars, rng, name, now))
    if rng.random() < GODSEND_PER_DAY * factor:
        notes.append(godsend(uid, chars, rng, name, now))
    if rng.random() < CALAMITY_PER_DAY * factor:
        notes.append(calamity(uid, chars, rng, name, now))
    if char["moral"] == "good" and rng.random() < BLESSING_PER_DAY / ticks_per_day:
        notes.append(_blessing(uid, chars, rng, name, now))
    if char["moral"] == "evil" and rng.random() < TEMPTATION_PER_DAY / ticks_per_day:
        notes.append(_temptation(uid, chars, rng, name, now))
    return [n for n in notes if n]


# ── the map ──────────────────────────────────────────────────────────────────
#
# Ported rule for rule from the IRC bot's moveplayers / collision_fight:
# once a second every running character steps -1, 0 or +1 on each axis, the
# grid wraps, and two characters landing on one square fight with a
# 1-in-(players online) chance. Questers on a journey don't wander — each
# has a 1% chance a second to take one step toward the current waypoint.

def ensure_position(char: dict, rng) -> None:
    if char.get("x") is None or char.get("y") is None:
        char["x"], char["y"] = rng.randrange(MAP_SIZE), rng.randrange(MAP_SIZE)


def _wander(value: int, rng) -> int:
    value += rng.randint(-1, 1)
    if value > MAP_SIZE:
        return 0
    if value < 0:
        return MAP_SIZE
    return value


def _toward(value: int, goal: int) -> int:
    return value if value == goal else value + (1 if value < goal else -1)


def landmark_at(point) -> "str | None":
    return _LANDMARK_NAMES.get(tuple(point)) if point else None


def _place(point) -> str:
    label = landmark_at(point)
    return f"{label} [{point[0]}, {point[1]}]" if label else f"[{point[0]}, {point[1]}]"


def collision_fight(uid: int, opp_uid: int, chars: dict, rng, name: NameFn, now: int) -> "list[Note]":
    me, opp = chars[uid], chars[opp_uid]
    my_sum, opp_sum = battle_sum(me), battle_sum(opp)
    my_roll = rng.randrange(my_sum) if my_sum else 0
    opp_roll = rng.randrange(opp_sum) if opp_sum else 0
    involved = (uid, opp_uid)
    head = f"⚔️ {name(uid)} {_rolled(my_roll, my_sum)} came upon {name(opp_uid)} {_rolled(opp_roll, opp_sum)} at [{me['x']}, {me['y']}]"
    if my_roll < opp_roll:
        lost = scale(me, now, max(opp["level"] // 7, 7))
        return [Note(involved, f"{head} and was defeated. {format_duration(lost)} added to their clock.", True)]
    won = -scale(me, now, -max(opp["level"] // 4, 7))
    notes = [Note(involved, f"{head} and took them in combat! {format_duration(won)} off their clock.", True)]
    if not rng.randrange(COLLISION_CRIT_ODDS):
        hurt = scale(opp, now, 5 + rng.randrange(20))
        notes.append(Note(involved, f"💥 A critical strike! {name(opp_uid)} is set back {format_duration(hurt)}.", True))
    elif not rng.randrange(COLLISION_STEAL_ODDS) and me["level"] >= COLLISION_STEAL_LEVEL:
        taken = _steal(me, opp, rng)
        if taken:
            notes.append(Note(involved, f"🫳 In the fierce battle {name(opp_uid)} dropped their {taken}, and {name(uid)} picked it up!", True))
    return notes


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
    for u in members:
        scale(chars[u], now, -QUEST_REWARD_PCT)
    _end_quest(quest, now + QUEST_REST_SECS)
    return [Note(tuple(members), f"🏆 {_names(members, name)} have completed their journey! Each is {QUEST_REWARD_PCT}% closer to their next level.", True)], True


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
        for u in members:
            scale(chars[u], now, -QUEST_REWARD_PCT)
        _end_quest(quest, now + QUEST_REST_SECS)
        return [Note(members, f"🏆 {_names(members, name)} completed their quest! Each is {QUEST_REWARD_PCT}% closer to their next level.", True)]

    if now < quest.get("not_before", 0):
        return []
    eligible = quest_eligible(chars)
    if len(eligible) < QUEST_MIN_PARTY:
        return []
    members = rng.sample(eligible, min(QUEST_MAX_PARTY, len(eligible)))
    # Mentions, not names: being picked is the one thing worth a badge.
    called = ", ".join(f"<@{u}>" for u in members)
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
            tuple(members),
        )]
    start, end, text = picked
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
        tuple(members),
        True,
    )]
