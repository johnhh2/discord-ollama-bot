"""The idle RPG ruleset (src/idlerpg.py): the curve, clocks, penalties, items,
battles, alignment and quests. Pure functions — no Discord, no DB."""
import random

import pytest

from src import idlerpg as rpg

NOW = 1_800_000_000


def _name(uid: int) -> str:
    return f"P{uid}"


def _char(level: int = 0, *, left: int = 1000, **over) -> dict:
    char = rpg.new_character("Tester", NOW - 100_000)
    char.update(level=level, next_level_at=NOW + left, last_seen=NOW, **over)
    return char


class _Scripted:
    """An rng whose answers the test dictates; anything unscripted is 0 / first."""
    def __init__(self, *, random_=0.0, randint=None, randrange=0):
        self._random, self._randint, self._randrange = random_, randint, randrange

    def random(self):
        return self._random

    def randint(self, low, high):
        return high if self._randint is None else self._randint(low, high)

    def randrange(self, n):
        return min(self._randrange, n - 1)

    def choice(self, seq):
        return seq[0]

    def sample(self, seq, k):
        return list(seq)[:k]


# ── the curve and the clock ──────────────────────────────────────────────────

def test_curve_grows_per_level_and_steepens_past_the_cap():
    assert rpg.ttl(0) == 600
    assert rpg.ttl(1) == 672
    assert rpg.ttl(61) == pytest.approx(rpg.ttl(60) * 1.25, abs=1)
    assert rpg.ttl(10, prestige=2) == pytest.approx(rpg.ttl(10) * 0.9, abs=1)
    assert rpg.ttl(10, prestige=9) == rpg.ttl(10, prestige=rpg.PRESTIGE_MAX_RANKS)


def test_level_up_starts_the_next_clock_where_the_last_ran_out():
    char = _char(left=-500)   # the tick arrived 500s late
    rpg.level_up(char)
    assert char["level"] == 1
    assert char["next_level_at"] == NOW - 500 + rpg.ttl(1)


def test_pause_and_resume_keep_the_time_left():
    char = _char(left=1000)
    rpg.pause(char, NOW + 400)
    assert rpg.is_paused(char) and char["remaining"] == 600
    rpg.shift(char, 50)
    assert rpg.time_left(char, NOW) == 650
    rpg.resume(char, NOW + 9000)
    assert char["next_level_at"] == NOW + 9650 and char["remaining"] is None


def test_scale_moves_by_a_share_of_the_time_left():
    char = _char(left=1000)
    assert rpg.scale(char, NOW, -25) == -250
    assert rpg.time_left(char, NOW) == 750


def test_prestige_resets_level_and_items_but_keeps_the_rank():
    char = _char(60, items={"ring": {"level": 80, "name": None}})
    rpg.do_prestige(char, NOW)
    assert (char["level"], char["items"], char["prestige"]) == (0, {}, 1)
    assert char["next_level_at"] == NOW + rpg.ttl(0, 1)


# ── penalties ────────────────────────────────────────────────────────────────

def test_penalty_grows_with_level():
    low, high = _char(0), _char(10)
    assert rpg.penalize(low, rpg.PEN_TALK_PER_CHAR, NOW, units=40) == 40
    assert rpg.penalize(high, rpg.PEN_TALK_PER_CHAR, NOW, units=40) == int(40 * 1.1 ** 10)
    assert low["penalty_total"] == 40 and low["last_penalty_at"] == NOW


def test_a_questers_penalty_fails_the_quest_for_everyone():
    chars = {1: _char(45), 2: _char(45), 3: _char(5)}
    quest = {"members": [1, 2], "description": "wait", "ends_at": NOW + 5000, "not_before": 0}
    before = rpg.time_left(chars[3], NOW)

    _secs, notes = rpg.penalize_player(1, chars, quest, rpg.PEN_PART, NOW, _name)

    assert not rpg.quest_active(quest)
    assert quest["not_before"] == NOW + rpg.QUEST_DROUGHT_SECS
    assert rpg.time_left(chars[3], NOW) > before       # a bystander pays too
    assert notes and notes[0].public and "P1" in notes[0].text


def test_a_bystanders_penalty_leaves_the_quest_alone():
    chars = {1: _char(45), 2: _char(45), 3: _char(5)}
    quest = {"members": [1, 2], "description": "wait", "ends_at": NOW + 5000, "not_before": 0}
    _secs, notes = rpg.penalize_player(3, chars, quest, rpg.PEN_PART, NOW, _name)
    assert rpg.quest_active(quest) and notes == []


# ── items ────────────────────────────────────────────────────────────────────

def test_item_rolls_stay_inside_the_level_bound():
    rng = random.Random(7)
    for level in (1, 10, 40):
        rolls = [rpg.roll_item_level(level, rng) for _ in range(300)]
        assert 1 <= min(rolls) and max(rolls) <= max(1, int(level * 1.5))


def test_found_item_only_replaces_a_worse_one():
    char = _char(20, items={"ring": {"level": 99, "name": None}})
    rng = _Scripted(random_=0.999, randrange=1)   # never a unique, always the lowest roll; choice → "ring"
    note = rpg.find_item(1, char, rng, _name)
    assert char["items"]["ring"]["level"] == 99 and "is better" in note.text
    char["items"].clear()
    rpg.find_item(1, char, rng, _name)
    assert char["items"]["ring"]["level"] == 1


def test_unique_needs_the_level_and_lands_in_its_slot():
    rng = _Scripted(randrange=0)   # every unique roll hits
    low = _char(24)
    rpg.find_item(1, low, rng, _name)
    assert not any(item.get("name") for item in low["items"].values())
    high = _char(25)
    note = rpg.find_item(1, high, rng, _name)
    assert high["items"]["helm"]["name"] == rpg.UNIQUES[0][2] and note.public


# ── battles ──────────────────────────────────────────────────────────────────

def test_a_lone_player_fights_the_house_and_a_win_shortens_the_clock():
    chars = {1: _char(30, left=10_000, items={"ring": {"level": 50, "name": None}})}
    rng = _Scripted(randint=lambda low, high: high if high == 50 else 0)   # we roll 50, the Warden 0
    notes = rpg.level_up_battle(1, chars, rng, _name, NOW)
    assert rpg.HOUSE_NAME in notes[0].text and notes[0].public
    assert rpg.time_left(chars[1], NOW) == 8000      # the house is worth 20%


def test_battles_below_the_threshold_are_only_sometimes():
    chars = {1: _char(5)}
    assert rpg.level_up_battle(1, chars, _Scripted(random_=0.9), _name, NOW) == []
    assert rpg.level_up_battle(1, chars, _Scripted(random_=0.1), _name, NOW) != []


def test_paused_characters_are_never_picked_as_opponents():
    sleeper = _char(30)
    rpg.pause(sleeper, NOW)
    chars = {1: _char(30), 2: sleeper}
    for seed in range(20):
        notes = rpg.level_up_battle(1, chars, random.Random(seed), _name, NOW)
        assert all(2 not in note.uids for note in notes)
    assert sleeper["remaining"] == 1000


def test_duel_moves_the_same_seconds_from_loser_to_winner():
    chars = {
        1: _char(10, left=10_000, items={"ring": {"level": 10, "name": None}}),
        2: _char(10, left=20_000),
    }
    notes = rpg.duel(1, 2, chars, _Scripted(), _name, NOW)   # 1 rolls its max, 2 has nothing to roll
    assert rpg.time_left(chars[2], NOW) == 21_000            # +5% of 20,000
    assert rpg.time_left(chars[1], NOW) == 9000
    assert notes[0].uids == (1, 2) and notes[0].public


def test_team_battle_needs_six_running_characters():
    chars = {uid: _char(10) for uid in range(1, 6)}
    assert rpg.team_battle(chars, random.Random(1), _name, NOW) == []
    chars[6] = _char(10)
    notes = rpg.team_battle(chars, random.Random(1), _name, NOW)
    assert len(notes) == 1 and len(notes[0].uids) == 6


# ── alignment ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("lawful good", ("lawful", "good")),
    ("Chaotic", ("chaotic", "neutral")),
    ("evil", ("neutral", "evil")),
    ("neutral", ("neutral", "neutral")),
    ("true neutral", ("neutral", "neutral")),
    ("neutral evil", ("neutral", "evil")),
    ("good lawful", None),
    ("heroic", None),
])
def test_parse_alignment(text, expected):
    assert rpg.parse_alignment(text) == expected


def test_alignment_bends_battle_power():
    items = {"ring": {"level": 100, "name": None}}
    assert rpg.battle_sum(_char(items=items, moral="good")) == 110
    assert rpg.battle_sum(_char(items=items, moral="evil")) == 90
    assert rpg.battle_sum(_char(items=items)) == 100


def test_chaotic_lives_see_more_events_than_lawful_ones():
    def _count(law: str) -> int:
        rng = random.Random(3)
        chars = {1: _char(10, left=10 ** 9, law=law)}
        return sum(len(rpg.random_events(1, chars, rng, _name, NOW, ticks_per_day=1)) for _ in range(4000))
    assert _count("chaotic") > _count("neutral") > _count("lawful")


# ── quests ───────────────────────────────────────────────────────────────────

def test_quest_starts_with_two_clean_high_level_players_and_pays_out():
    chars = {1: _char(40), 2: _char(50), 3: _char(39), 4: _char(60, last_penalty_at=NOW - 60)}
    quest = rpg.new_quest()
    notes = rpg.tick_quest(chars, quest, random.Random(1), _name, NOW)
    assert sorted(quest["members"]) == [1, 2] and notes[0].public
    assert rpg.QUEST_MIN_SECS <= quest["ends_at"] - NOW <= rpg.QUEST_MAX_SECS

    assert rpg.tick_quest(chars, quest, random.Random(1), _name, NOW + 60) == []
    done = quest["ends_at"]
    chars[1]["next_level_at"] = done + 1000
    rpg.tick_quest(chars, quest, random.Random(1), _name, done)
    assert rpg.time_left(chars[1], done) == 750
    assert not rpg.quest_active(quest) and quest["not_before"] == done + rpg.QUEST_REST_SECS


def test_no_quest_with_one_eligible_player_or_during_the_drought():
    quest = rpg.new_quest()
    assert rpg.tick_quest({1: _char(40), 2: _char(3)}, quest, random.Random(1), _name, NOW) == []
    quest["not_before"] = NOW + 10
    assert rpg.tick_quest({1: _char(40), 2: _char(40)}, quest, random.Random(1), _name, NOW) == []


def test_a_quest_whose_party_all_left_is_dropped():
    quest = {"members": [8, 9], "description": "wait", "ends_at": NOW + 500, "not_before": 0}
    assert rpg.tick_quest({1: _char(1)}, quest, random.Random(1), _name, NOW) == []
    assert not rpg.quest_active(quest)
