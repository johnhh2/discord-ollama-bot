"""The idle RPG ruleset (src/idlerpg.py): the curve, clocks, penalties, items,
battles, alignment and quests. Pure functions — no Discord, no DB."""
import random
import re

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


def test_only_milestones_and_week_long_levels_are_channel_news():
    news = [level for level in range(1, 70) if rpg.level_is_news(level)]
    assert news == [10, 20, 30, 40, 50, 60, 62, 63, 64, 65, 66, 67, 68, 69]
    assert rpg.ttl(60) < rpg.RARE_LEVEL_SECS <= rpg.ttl(61)


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


def test_voice_speeds_a_running_clock_and_leaves_a_paused_one_alone():
    char = _char(left=1000)
    rpg.apply_boost(char, 60, rpg.VOICE_BONUS_PCT)
    assert rpg.time_left(char, NOW) == 994
    rpg.pause(char, NOW)
    rpg.apply_boost(char, 60, rpg.VOICE_BONUS_PCT)
    assert char["remaining"] == 994
    assert rpg.gold_bonus(70, rpg.VOICE_BONUS_PCT) == 7 and rpg.gold_bonus(9, rpg.VOICE_BONUS_PCT) == 0


# ── penalties ────────────────────────────────────────────────────────────────

def test_penalty_grows_with_level():
    low, high = _char(0), _char(10)
    assert rpg.penalize(low, rpg.PEN_PART, NOW) == 200
    assert rpg.penalize(high, rpg.PEN_PART, NOW) == int(200 * 1.1 ** 10)
    assert low["penalty_total"] == 200 and low["last_penalty_at"] == NOW


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
    # Every find is named; only these eight are the named ones that matter.
    assert not any(item["name"] in rpg.UNIQUE_NAMES for item in low["items"].values())
    high = _char(25)
    note = rpg.find_item(1, high, rng, _name)
    assert high["items"]["helm"]["name"] == rpg.UNIQUES[0][2] and note.public


def test_a_rerolled_unique_is_only_news_when_much_better():
    _lv, slot, crown, low, high = rpg.UNIQUES[0]
    char = _char(25, items={slot: {"level": 50, "name": crown}})
    # A few levels better: worn, told in the feed, kept out of the channel.
    note = rpg.find_item(1, char, _Scripted(randrange=0, randint=lambda lo, hi: 55), _name)
    assert char["items"][slot]["level"] == 55 and not note.public
    # 1.5× better is a real upgrade — and a different unique in the slot always is.
    note = rpg.find_item(1, char, _Scripted(randrange=0, randint=lambda lo, hi: 83), _name)
    assert char["items"][slot]["level"] == 83 and note.public
    assert rpg._named_find_is_news({"level": 90, "name": "Something Else"}, crown, 91)


def test_a_rerolled_trophy_is_only_news_when_much_better():
    rng = _Scripted()   # random() is 0 so the drop lands; randint hands back the top of the range
    slot, title, _lo, high = rpg.SIGNATURE_DROPS["Dragon"]
    char = _char(40, items={slot: {"level": high - 1, "name": title}})
    note = rpg.signature_drop(1, char, "Dragon", rng, _name)
    assert char["items"][slot]["level"] == high and not note.public
    char["items"][slot] = {"level": high // 2, "name": title}
    assert rpg.signature_drop(1, char, "Dragon", rng, _name).public


# ── battles ──────────────────────────────────────────────────────────────────

def _rolls(*values):
    """A randint that hands out `values` in order (capped at the roll's maximum)."""
    queue = list(values)
    return lambda low, high: min(queue.pop(0), high)


def test_a_lone_player_fights_the_house_and_a_win_shortens_the_clock():
    chars = {1: _char(30, left=10_000, items={"ring": {"level": 50, "name": None}})}
    notes = rpg.level_up_battle(1, chars, _Scripted(randint=_rolls(50, 0)), _name, NOW)
    assert rpg.HOUSE_NAME in notes[0].text and not notes[0].public   # the house concerns nobody else
    assert rpg.time_left(chars[1], NOW) == 8000      # a decisive win over the house is worth 20%


def test_the_house_is_the_challengers_own_match_not_the_best_players():
    chars = {
        1: _char(30, items={"ring": {"level": 20, "name": None}}),
        2: _char(30, items={"ring": {"level": 900, "name": None}}),
    }
    rpg.pause(chars[2], NOW)                          # nobody to pick but the house
    notes = rpg.level_up_battle(1, chars, _Scripted(randint=_rolls(20, 999)), _name, NOW)
    assert "(rolled 20 of 20)" in notes[0].text and notes[0].text.count("of 20)") == 2


def test_the_stake_follows_the_margin_of_the_win():
    def _fight(mine, theirs):
        chars = {
            1: _char(25, left=10_000, x=100, y=100, items={"ring": {"level": 40, "name": None}}),   # 25: a fight on every level-up
            2: _char(25, left=10_000, x=100, y=110, items={"ring": {"level": 40, "name": None}}),      # …within reach of each other
        }
        notes = rpg.level_up_battle(1, chars, _Scripted(randint=_rolls(mine, theirs, 5), randrange=0, random_=0.9), _name, NOW)   # the spare 5 feeds a critical strike's roll
        return rpg.time_left(chars[1], NOW), notes[0].text

    assert _fight(40, 0)[0] == 9300                   # a rout: the whole 7%
    assert _fight(30, 20)[0] == 9650                  # by half the reach: half of it
    left, text = _fight(21, 20)
    assert left == 9825 and "narrowly won" in text    # by a hair: a quarter
    left, text = _fight(19, 20)
    assert left == 10_175 and "narrowly lost" in text
    assert rpg.margin_factor(0, 3, 7, 3) == pytest.approx(3 / 3.5)


def test_nobody_is_challenged_twice_inside_the_cooldown():
    chars = {1: _char(30, x=100, y=100), 2: _char(30, x=100, y=110)}
    rng = _Scripted(randrange=0)                      # always the first in the pool
    first = rpg.level_up_battle(1, chars, rng, _name, NOW)
    assert 2 in first[0].uids and chars[2]["challenged_at"] == NOW
    again = rpg.level_up_battle(1, chars, rng, _name, NOW + 600)
    assert again[0].uids == (1,) and rpg.HOUSE_NAME in again[0].text
    later = rpg.level_up_battle(1, chars, rng, _name, NOW + rpg.CHALLENGED_COOLDOWN_SECS)
    assert 2 in later[0].uids


def test_a_player_out_of_range_is_never_the_opponent():
    def _fight(gap, **over):
        chars = {1: _char(30, x=100, y=100), 2: _char(30, x=100, y=100 + gap, **over)}
        return rpg.level_up_battle(1, chars, _Scripted(randrange=0), _name, NOW)[0]

    near = _fight(rpg.BATTLE_RANGE)                                 # just inside: a real opponent
    assert 2 in near.uids and near.public                           # …and worth telling the channel
    far = _fight(rpg.BATTLE_RANGE + 1)
    assert far.uids == (1,) and rpg.HOUSE_NAME in far.text          # a step further: the house instead
    assert not far.public                                           # which stays in the feed
    assert rpg.BATTLE_RANGE == rpg.MARKET_RADIUS

    # Before the tick has placed a character, nobody can reach it.
    unplaced = {1: _char(30, x=100, y=100), 2: _char(30)}
    assert rpg.HOUSE_NAME in rpg.level_up_battle(1, unplaced, _Scripted(randrange=0), _name, NOW)[0].text
    assert not rpg.in_battle_range(_char(), _char(x=1, y=1))


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
    whole = {u: rpg.hp_of(c) for u, c in chars.items()}
    story, notes = rpg.duel(1, 2, chars, _Scripted(randrange=5), _name, NOW)   # never a dodge, never a crit
    assert rpg.time_left(chars[2], NOW) == 21_000            # the loser gives up 5% of 20,000…
    assert rpg.time_left(chars[1], NOW) == 9000              # …and the winner takes the same
    assert notes[0].uids == (1, 2) and not notes[0].public   # the duellists' feeds, not the room
    assert len(story) >= 2 and story[0].startswith("🤺") and "**Round 1**" in story[1]
    assert {u: rpg.hp_of(c) for u, c in chars.items()} == whole   # a match, not a mugging


def test_a_duel_nobody_wins_on_damage_falls_to_a_coin_toss():
    def _fight(random_):
        # Identical and gearless: every blow lands for the same 1, so the
        # five rounds end dead level and the toss decides.
        chars = {1: _char(0, left=600), 2: _char(0, left=600)}
        _story, notes = rpg.duel(1, 2, chars, _Scripted(random_=random_, randrange=5), _name, NOW)
        return chars, notes[0].text

    chars, text = _fight(0.4)
    assert rpg.time_left(chars[2], NOW) == 630 and "P1 wins" in text and "coin toss" in text
    chars, text = _fight(0.6)                       # the challenger has no edge to fall back on
    assert rpg.time_left(chars[1], NOW) == 630 and "P2 wins" in text


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


def test_the_hand_of_god_ignores_law_and_chaos_so_no_alignment_is_simply_best(monkeypatch):
    hands = []
    monkeypatch.setattr(rpg, "hand_of_god", lambda uid, *args: hands.append(uid) or rpg.Note((uid,), "hog"))
    monkeypatch.setattr(rpg, "godsend", lambda uid, *args: None)
    monkeypatch.setattr(rpg, "calamity", lambda uid, *args: None)
    counts = {}
    for law in rpg.LAWS:
        hands.clear()
        rng = random.Random(4)
        chars = {1: _char(10, left=10 ** 9, law=law)}
        for _ in range(3000):
            rpg.random_events(1, chars, rng, _name, NOW, 1)
        counts[law] = len(hands)
    # Same seed, same number of draws per tick: identical, not merely close.
    assert counts["lawful"] == counts["neutral"] == counts["chaotic"] > 0


def test_chaotic_lives_see_more_events_than_lawful_ones():
    def _count(law: str) -> int:
        rng = random.Random(3)
        chars = {1: _char(10, left=10 ** 9, law=law)}
        return sum(len(rpg.random_events(1, chars, rng, _name, NOW, ticks_per_day=1)) for _ in range(4000))
    assert _count("chaotic") > _count("neutral") > _count("lawful")


# ── gold ─────────────────────────────────────────────────────────────────────

def test_winning_a_level_up_battle_pays_by_the_opponents_level_and_the_house_pays_flat():
    chars = {1: _char(30, items={"ring": {"level": 50, "name": None}})}
    rng = _Scripted(randint=lambda low, high: high if high == 50 else 0)
    rpg.level_up_battle(1, chars, rng, _name, NOW)
    assert chars[1]["gold"] == rpg.GOLD_HOUSE_WIN

    chars = {1: _char(30, x=100, y=100, items={"ring": {"level": 50, "name": None}}), 2: _char(12, x=100, y=110)}
    rng = _Scripted(randint=lambda low, high: high if high == 50 else 0, randrange=0)   # randrange 0 picks player 2… and crits
    notes = rpg.level_up_battle(1, chars, rng, _name, NOW)
    assert chars[1]["gold"] == 60 and "60 gold" in notes[0].text and chars[2]["gold"] == 0


def test_a_collision_win_also_lifts_five_percent_of_the_losers_purse():
    chars = {
        1: _char(10, x=10, y=10, gold=1000),
        2: _char(10, x=11, y=11, gold=0, items={"ring": {"level": 30, "name": None}}),
    }
    rng = _Walk([(0, 0), (-1, -1)], random_=0.4, randrange=10 ** 9)
    notes = rpg.move_players(chars, rpg.new_quest(), rng, _name, NOW, 1)
    assert chars[1]["gold"] == 950 and chars[2]["gold"] == 50 + 50
    assert "plus 50 lifted from P1's purse" in notes[0].text


def test_a_finished_quest_pays_every_quester_by_party_size():
    chars = {1: _char(45), 2: _char(45), 3: _char(45)}
    quest = {**rpg.new_quest(), "members": [1, 2, 3], "description": "wait", "kind": "vigil", "ends_at": NOW}
    notes = rpg.tick_quest(chars, quest, _Scripted(), _name, NOW)
    assert [c["gold"] for c in chars.values()] == [750, 750, 750] and "750 gold" in notes[0].text


class _Luck(_Scripted):
    """random() answers from a queue, then settles on one value — enough to
    steer luck down whichever branch a test is about."""
    def __init__(self, randoms, **kw):
        super().__init__(**kw)
        self._queue = list(randoms)

    def random(self):
        return self._queue.pop(0) if self._queue else self._random


def _lucky(hp=None, **over):
    char = _char(12, left=10_000, x=5, y=255, gold=500,
                 items={"ring": {"level": 30, "name": None}}, **over)
    char["hp"] = rpg.max_hp(char) if hp is None else hp
    return char


def test_a_godsend_mends_a_wounded_character_and_leaves_a_whole_one_alone():
    hurt = _lucky(hp=50)
    note = rpg.godsend(1, {1: hurt}, _Luck([0.1]), _name, NOW)
    assert "HP (" in note.text and rpg.hp_of(hurt) > 50 and not note.public

    whole = _lucky()
    note = rpg.godsend(1, {1: whole}, _Luck([0.1]), _name, NOW)   # nothing to mend: the next kind instead
    assert "HP (" not in note.text and rpg.hp_of(whole) == rpg.max_hp(whole)


def test_a_godsend_can_put_you_on_a_cart_to_a_town():
    char = _lucky(travel_to="Denmark")
    note = rpg.godsend(1, {1: char}, _Luck([0.1]), _name, NOW)
    assert "set down in" in note.text and char["travel_to"] is None
    assert (char["x"], char["y"]) in [rpg.LANDMARKS[t] for t in rpg.TOWNS]
    assert rpg.market_in_reach(char) is not None


def test_a_godsend_can_turn_up_an_item_in_the_road():
    char = _lucky()
    char["items"] = {}
    note = rpg.godsend(1, {1: char}, _Luck([0.9, 0.1]), _name, NOW)
    assert "Lying in the road" in note.text and char["items"]


def test_a_calamity_can_wound_but_never_kill():
    char = _lucky()
    note = rpg.calamity(1, {1: char}, _Luck([0.1]), _name, NOW)
    assert "HP (" in note.text and 0 < rpg.hp_of(char) < rpg.max_hp(char)

    dying = _lucky(hp=2)
    rpg.calamity(1, {1: dying}, _Luck([0.1]), _name, NOW)
    assert rpg.hp_of(dying) == 1                       # a wound, never a grave
    untouchable = _lucky(hp=1)
    rpg.calamity(1, {1: untouchable}, _Luck([0.1]), _name, NOW)
    assert rpg.hp_of(untouchable) == 1


def test_a_calamity_can_leave_you_lost_but_not_while_you_are_in_town():
    char = _lucky(travel_to="Denmark")
    note = rpg.calamity(1, {1: char}, _Luck([0.9, 0.1]), _name, NOW)
    assert "squares off" in note.text and char["travel_to"] is None
    assert 0 <= char["x"] <= rpg.MAP_SIZE and 0 <= char["y"] <= rpg.MAP_SIZE

    town = rpg.LANDMARKS["Velvragh"]
    safe = _lucky()
    safe["x"], safe["y"] = town
    note = rpg.calamity(1, {1: safe}, _Luck([0.9, 0.1]), _name, NOW)
    assert "squares off" not in note.text and (safe["x"], safe["y"]) == town


def test_godsends_and_calamities_are_sometimes_about_gold():
    # No wounds and nowhere on the map, so heal, cart and lost all fall
    # through; the queue then steps past the item roll to the gold one.
    def _purse(gold=1000):
        char = _char(10, left=10_000, gold=gold)
        char["hp"] = rpg.max_hp(char)
        return {1: char}

    rich = _purse()
    rng = _Luck([0.9, 0.9, 0.1], randint=lambda low, high: high)
    assert "purse" in rpg.godsend(1, rich, rng, _name, NOW).text and rich[1]["gold"] == 1200

    rng = _Luck([0.9, 0.9, 0.1], randint=lambda low, high: high)
    assert "120 gold lost" in rpg.calamity(1, rich, rng, _name, NOW).text and rich[1]["gold"] == 1080
    assert rpg.time_left(rich[1], NOW) == 10_000        # neither touched the clock

    broke = _purse(gold=0)
    rng = _Luck([0.9, 0.9, 0.1], randint=lambda low, high: high)
    rpg.calamity(1, broke, rng, _name, NOW)             # nothing to steal: the ordinary kind
    assert broke[1]["gold"] == 0 and rpg.time_left(broke[1], NOW) > 10_000


def test_prestige_keeps_the_gold():
    char = _char(60, gold=777)
    rpg.do_prestige(char, NOW)
    assert char["gold"] == 777


def test_shop_prices_scale_with_level_and_sharpening_with_the_item():
    assert rpg.shop_prices(_char(0))["find"] == 25 and rpg.shop_prices(_char(40))["find"] == 1000
    char = _char(10, gold=10_000, items={"ring": {"level": 5, "name": None}, "helm": {"level": 300, "name": "Crown"}})
    assert rpg.buy_sharpen(char, "ring") == (True, "Your level 5 ring is now level 6.")    # at least +1
    bought, text = rpg.buy_sharpen(char, "helm")
    assert bought and char["items"]["helm"]["level"] == 330 and text.startswith("Your Crown")
    assert char["gold"] == 10_000 - 100 - 6000


# ── towns ────────────────────────────────────────────────────────────────────

def test_markets_reach_sixty_squares_and_only_settlements_have_one():
    town = rpg.LANDMARKS["Velvragh"]
    assert rpg.market_in_reach(_char(x=town[0] + 60, y=town[1])) == "Velvragh"
    assert rpg.market_in_reach(_char(x=town[0] + 61, y=town[1])) is None
    wilds = rpg.LANDMARKS["the Great Shahlil mountains"]
    assert rpg.market_in_reach(_char(x=wilds[0], y=wilds[1])) is None
    assert rpg.market_in_reach(_char()) is None and rpg.nearest_town(_char()) is None   # not yet on the map
    assert all(town in rpg.LANDMARKS for town in rpg.TOWNS)


def test_the_errand_stays_inside_half_the_purse_and_never_buys_the_players_choices():
    x, y = rpg.LANDMARKS["Denmark"]
    rng = _Scripted(random_=0.999, randrange=1)
    char = _char(10, left=10_000, gold=300, x=x, y=y, items={"ring": {"level": 5, "name": None}})
    note = rpg.auto_trade(1, char, rng, _name, NOW)           # budget 150: no find (250), but the ring (100)
    assert char["gold"] == 200 and char["items"]["ring"]["level"] == 6 and "Their level 5 ring" in note.text
    assert rpg.time_left(char, NOW) == 10_000 and char["rush_day"] is None and char["class"] == "Tester"
    assert rpg.auto_trade(1, char, rng, _name, NOW + 3600) is None                       # cooldown


def test_a_poor_visit_is_not_stamped_and_the_ring_and_the_wilds_do_not_trade():
    x, y = rpg.LANDMARKS["Denmark"]
    rng = _Scripted()
    poor = _char(10, gold=40, x=x, y=y, items={"ring": {"level": 5, "name": None}})
    assert rpg.auto_trade(1, poor, rng, _name, NOW) is None and poor["traded_at"] == 0 and poor["gold"] == 40
    ring = _char(10, gold=5000, x=x + rpg.TOWN_CORE_RADIUS + 1, y=y)
    assert rpg.auto_trade(1, ring, rng, _name, NOW) is None and ring["gold"] == 5000
    off = _char(10, gold=5000, x=x, y=y, auto_trade=False)
    assert rpg.auto_trade(1, off, rng, _name, NOW) is None and off["gold"] == 5000


# ── monsters ─────────────────────────────────────────────────────────────────

# The most remote Plains square there is: outside every market ring even
# counting the wrap, so a monster can actually find you.
WILDS = (5, 255)


def _wanderer(level=10, gear=25, gold=1000, **over):
    char = _char(level, left=10 ** 6, x=WILDS[0], y=WILDS[1], gold=gold, **over)
    if gear:
        char["items"] = {"ring": {"level": gear, "name": None}}
    char["hp"] = rpg.max_hp(char)
    return char


def test_a_body_grows_with_level_and_gear_and_mends_itself():
    char = _char(10, items={"ring": {"level": 50, "name": None}})
    assert rpg.max_hp(char) == 100 + 5 * 10 + 50
    char["hp"] = 10
    rpg.regen_hp(char)
    assert rpg.hp_of(char) == 10 + max(1, rpg.max_hp(char) // rpg.HP_REGEN_DIVISOR)
    rpg.heal(char, 10 ** 6)
    assert rpg.hp_of(char) == rpg.max_hp(char)          # never over the brim
    char["items"].clear()                                # gear lost, body with it
    assert rpg.hp_of(char) == rpg.max_hp(char) == 150
    assert rpg.hp_pct(char) == 100


def test_a_fight_is_rounds_of_blows_and_the_wounds_are_kept():
    char = _wanderer()
    rng = random.Random(2)
    rounds, killed, marks = rpg.fight_monster(char, 40, 10 ** 9, rng)   # nothing can kill that
    assert rounds == rpg.MOB_MAX_ROUNDS and not killed
    assert 0 < rpg.hp_of(char) < rpg.max_hp(char)
    assert set(marks) <= {"·", "✳", "✳".lower()}    # only dodges and crits leave a mark

    whole = _wanderer()
    rounds, killed, _marks = rpg.fight_monster(whole, 1, 1, rng)        # and that dies to a look
    assert killed and rounds <= 2                                       # 2 only if the first swing missed


def test_a_kill_pays_gold_and_a_slice_of_the_clock_and_a_town_is_safe():
    char = _wanderer(gold=100)
    notes = rpg.mob_encounter(1, char, random.Random(5), _name, NOW)
    assert notes and "[Plains]" in notes[0].text and not notes[0].public
    assert char["mob_kills"] == 1 and char["gold"] > 100
    assert rpg.time_left(char, NOW) < 10 ** 6            # a kill shaves the clock, a little

    town = rpg.LANDMARKS["Velvragh"]
    safe = _wanderer()
    safe["x"], safe["y"] = town[0] + 30, town[1]
    assert rpg.mob_encounter(1, safe, random.Random(5), _name, NOW) == []


def test_a_hurt_character_makes_camp_instead_of_fighting():
    char = _wanderer()
    char["hp"] = rpg.max_hp(char) * rpg.CAMP_HP_PCT // 100
    assert rpg.needs_rest(char)
    notes = rpg.mob_encounter(1, char, random.Random(1), _name, NOW)
    assert len(notes) == 1 and notes[0].text.startswith("⛺") and not notes[0].public
    assert char["mob_kills"] == 0 and not rpg.needs_rest(char)


def test_falling_costs_clock_and_gold_and_carries_you_to_a_town_patched_up():
    rng = random.Random(3)
    for _ in range(4000):
        char = _wanderer(gold=1200)
        char["x"], char["y"] = WILDS
        char["next_level_at"] = NOW + 10_000
        char["hp"] = rpg.max_hp(char) // 2               # wounded, but still willing
        notes = rpg.mob_encounter(1, char, rng, _name, NOW)
        if char["mob_deaths"]:
            break
    else:
        raise AssertionError("no death in 4000 wounded encounters")
    assert "struck P1 down" in notes[-1].text and "outskirts of" in notes[-1].text
    assert char["gold"] < 1200 and rpg.time_left(char, NOW) > 10_000
    assert rpg.hp_of(char) == rpg.max_hp(char)           # patched up on the way
    assert rpg.nearest_town(char)[1] <= rpg.MOB_RESPAWN_DISTANCE


def test_a_group_is_fought_one_after_another():
    rng = random.Random(1)
    for _ in range(500):
        char = _wanderer(level=30, gear=200)
        notes = rpg.mob_encounter(1, char, rng, _name, NOW)
        if notes and ", then a " in notes[0].text:
            assert char["mob_kills"] >= 2
            return
    raise AssertionError("never met a group")


def test_killing_something_legendary_is_channel_news(monkeypatch):
    # A full-strength dragon is a five-round standoff even at level 40 — this
    # one is starving, so it can actually be put down.
    monkeypatch.setattr(rpg, "roll_monster", lambda level, biome, rng, effect=None: ("Starving", "Dragon", 0.4, 1, 5))
    char = _wanderer(level=40, gear=400)
    notes = rpg.mob_encounter(1, char, random.Random(4), _name, NOW)
    assert "killed a Starving Dragon" in notes[0].text and notes[0].public


def test_monsters_are_bulky_in_proportion_to_the_character_they_meet():
    small, big = _wanderer(level=1, gear=2), _wanderer(level=40, gear=400)
    rat, dragon = rpg.monster_hp(small, 1, 0.5), rpg.monster_hp(small, 5, 1.8)
    assert rat < dragon and rat < rpg.max_hp(small) // 4
    assert rpg.monster_hp(big, 1, 0.5) > rat          # the same rat is fatter beside a bigger body
    assert rpg.monster_hp(big, 5, 1.8) > rpg.monster_hp(big, 1, 0.5) * 4


def test_biomes_follow_the_drawn_map_and_every_one_can_spawn_something():
    assert rpg.biome_at(300, 100) == "Mountains" and rpg.biome_at(60, 450) == "Mountains"
    assert rpg.biome_at(480, 480) == "Darklands" and rpg.biome_at(20, 20) == "Coast"
    assert rpg.biome_at(90, 250) == "Caves" and rpg.biome_at(250, 490) == "Haunted"
    assert rpg.biome_at(430, 250) == "Forest" and rpg.biome_at(440, 150) == "Plains"
    biomes = {biome for _n, _r, _t, _tier, where in rpg.MOB_TYPES for biome in where}
    rng = random.Random(5)
    for biome in biomes:
        for level in (1, 30, 80):
            for _ in range(200):
                _prefix, _beast, strength, _gold, tier = rpg.roll_monster(level, biome, rng)
                assert 1 <= tier <= 5 and strength > 0


def test_level_and_dangerous_ground_bring_out_the_rarer_monsters():
    def _rare_share(level, biome):
        rng = random.Random(9)
        rolls = [rpg.roll_monster(level, biome, rng) for _ in range(4000)]
        return sum(tier >= 4 for *_rest, tier in rolls) / len(rolls)
    assert _rare_share(60, "Mountains") > _rare_share(5, "Mountains") * 2
    assert _rare_share(30, "Darklands") > _rare_share(30, "Plains")


def test_monsters_are_cut_to_the_characters_size_and_go_easy_on_beginners():
    geared = _char(30, items={"ring": {"level": 120, "name": None}})
    assert rpg.mob_power(geared, 1.0) == 100                   # 120 / 1.2 × (0.6 + 0.4)
    assert rpg.mob_power(geared, 3.0) == 180
    assert rpg.mob_power(_char(5, items={"ring": {"level": 120, "name": None}}), 1.0) == 50
    assert rpg.mob_power(_char(30), 1.0) == 50                 # no gear: sized to the level instead


# ── travel ───────────────────────────────────────────────────────────────────

def test_a_traveller_walks_straight_to_town_without_wandering_or_meeting_anyone():
    gx, gy = rpg.LANDMARKS["Velvragh"]
    chars = {
        1: _char(10, x=gx - 2, y=gy + 1, travel_to="Velvragh"),
        2: _char(10, x=gx - 2, y=gy + 1),                       # stands on the same square, and stays
    }
    rng = _Walk(random_=0.0)                                    # every traveller steps every second
    notes = rpg.move_players(chars, rpg.new_quest(), rng, _name, NOW, 1)
    assert (chars[1]["x"], chars[1]["y"]) == (gx - 1, gy) and notes == []     # diagonal, and no fight

    notes = rpg.move_players(chars, rpg.new_quest(), rng, _name, NOW, 3)
    assert (chars[1]["x"], chars[1]["y"]) == (gx, gy) and chars[1]["travel_to"] is None
    assert [n.text for n in notes] == ["🧭 P1 arrived in Velvragh."] and not notes[0].public

    wx, wy = rpg.LANDMARKS["T'rnalvph"]
    chars = {1: _char(10, x=wx - 1, y=wy, travel_to="T'rnalvph")}
    notes = rpg.move_players(chars, rpg.new_quest(), rng, _name, NOW, 1)
    assert [n.text for n in notes] == ["🧭 P1 arrived at T'rnalvph."] and chars[1]["travel_to"] is None


def test_a_traveller_mostly_stands_still_and_a_paused_one_always_does():
    chars = {1: _char(10, x=10, y=10, travel_to="Velvragh")}
    rpg.move_players(chars, rpg.new_quest(), _Walk([(1, 1)] * 5, random_=0.5), _name, NOW, 5)
    assert (chars[1]["x"], chars[1]["y"]) == (10, 10)           # 0.5 ≥ the 1% step chance, and no wander either
    rpg.pause(chars[1], NOW)
    rpg.move_players(chars, rpg.new_quest(), _Walk(random_=0.0), _name, NOW, 5)
    assert (chars[1]["x"], chars[1]["y"]) == (10, 10)


def test_being_picked_for_a_journey_cancels_travel_and_a_vigil_does_not():
    class _PickJourney(_Scripted):
        def choice(self, seq):
            return rpg._JOURNEYS[0]
    chars = {1: _char(40, travel_to="Denmark"), 2: _char(50)}
    rpg.tick_quest(chars, rpg.new_quest(), _PickJourney(), _name, NOW)
    assert chars[1]["travel_to"] is None
    chars = {1: _char(40, travel_to="Denmark"), 2: _char(50)}
    rpg.tick_quest(chars, rpg.new_quest(), _Scripted(), _name, NOW)
    assert chars[1]["travel_to"] == "Denmark"


def test_towns_match_by_any_unambiguous_part_of_the_name():
    assert rpg.match_place("Velvragh") == "Velvragh"
    assert rpg.match_place("towers") == "the Towers of Ankh-Allor"
    assert rpg.match_place(" QWOK ") == "the land of Qwok"          # a town beats its namesake mountains
    assert rpg.match_place("mountains of qwok") == "the Mountains of Qwok"
    assert rpg.match_place("trnalvph") == rpg.match_place("T'rnalvph") == "T'rnalvph"
    assert rpg.match_place("shahlil") == "the Great Shahlil mountains" and rpg.match_place("bharash") == "the Secret Passage to Bharash"
    assert rpg.match_place("the") is None and rpg.match_place("") is None and rpg.match_place("mountains") is None
    # Every wild destination really is wild: monsters, no market.
    for place, (x, y) in rpg.LANDMARKS.items():
        if place not in rpg.TOWNS:
            assert rpg.biome_at(x, y) != "Plains" and rpg.market_in_reach(_char(x=x, y=y)) is None
    char = _char(x=300, y=200)
    assert rpg.travel_steps(char, "Velvragh") == 70 and rpg.travel_eta_secs(char, "Velvragh") == 7000


# ── pace ─────────────────────────────────────────────────────────────────────

def test_lively_pace_brings_luck_about_daily_and_classic_about_weekly():
    def _events(pace) -> int:
        rng = random.Random(11)
        chars = {1: _char(10, left=10 ** 12)}
        return sum(len(rpg.random_events(1, chars, rng, _name, NOW, 1440, pace)) for _ in range(1440 * 30))

    lively, classic = _events(rpg.PACES["lively"]), _events(rpg.CLASSIC)
    assert 45 <= lively <= 90        # ~2.2 a day over thirty days
    assert 3 <= classic <= 20        # ~0.3 a day
    assert rpg.PACES[rpg.DEFAULT_PACE] is rpg.PACES["lively"]


def test_monsters_come_about_ninety_a_day_in_the_wilds_and_never_in_town():
    def _fights(pace, spot, days=10):
        rng = random.Random(11)
        fights = 0
        char = _char(10, left=10 ** 9, x=spot[0], y=spot[1], gold=10 ** 6,
                     items={"ring": {"level": 25, "name": None}})
        char["hp"] = rpg.max_hp(char)
        for tick in range(1440 * days):
            rpg.regen_hp(char)
            if rng.random() < pace.mob_fights_per_day / 1440:
                fights += bool(rpg.mob_encounter(1, char, rng, _name, NOW + tick * 60))
                char["x"], char["y"] = spot                  # a fall carries them townward
        return fights / days

    wilds = WILDS
    town = (rpg.LANDMARKS["Velvragh"][0] + 30, rpg.LANDMARKS["Velvragh"][1])
    assert 80 <= _fights(rpg.PACES["lively"], wilds) <= 100      # sizzlorox's own rate
    assert 25 <= _fights(rpg.CLASSIC, wilds) <= 36
    assert _fights(rpg.PACES["lively"], town) == 0               # a market ring is safe ground


def test_no_pace_turns_early_level_ups_into_constant_fights():
    # Lively once fought on half of them; with two players and a level every
    # few minutes, that was one player being fought without pause.
    for pace in rpg.PACES.values():
        assert pace.battle_chance_below <= 0.25


def test_lively_team_battles_shrink_to_two_a_side_and_never_to_one():
    lively = rpg.PACES["lively"]
    chars = {uid: _char(10) for uid in range(1, 4)}
    assert rpg.team_battle(chars, random.Random(1), _name, NOW, lively) == []
    chars[4] = _char(10)
    assert len(rpg.team_battle(chars, random.Random(1), _name, NOW, lively)[0].uids) == 4
    chars.update({uid: _char(10) for uid in range(5, 9)})
    assert len(rpg.team_battle(chars, random.Random(1), _name, NOW, lively)[0].uids) == 6


# ── quests ───────────────────────────────────────────────────────────────────

def test_quest_starts_with_two_high_level_players_and_pays_out():
    paused = _char(60)
    rpg.pause(paused, NOW)
    chars = {1: _char(40), 2: _char(50), 3: _char(39), 4: paused}
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


def test_a_quest_never_pings_a_character_nobody_claimed():
    chars = {1: _char(40), 2: _char(50, claimed=False)}
    for picked in (rpg._VIGILS[0], rpg._JOURNEYS[0]):
        class _Pick(_Scripted):
            def choice(self, seq):
                return picked
        for char in chars.values():
            char["travel_to"] = None
        note = rpg.tick_quest(chars, rpg.new_quest(), _Pick(), _name, NOW)[0]
        assert note.ping == (1,) and "<@1>" in note.text and "<@2>" not in note.text and "P2" in note.text
        assert note.uids == (1, 2)                                   # still a quester, still paid


def test_no_quest_with_one_eligible_player_or_during_the_drought():
    quest = rpg.new_quest()
    assert rpg.tick_quest({1: _char(40), 2: _char(3)}, quest, random.Random(1), _name, NOW) == []
    quest["not_before"] = NOW + 10
    assert rpg.tick_quest({1: _char(40), 2: _char(40)}, quest, random.Random(1), _name, NOW) == []


def test_a_quest_whose_party_all_left_is_dropped():
    quest = {**rpg.new_quest(), "members": [8, 9], "description": "wait", "kind": "vigil", "ends_at": NOW + 500}
    assert rpg.tick_quest({1: _char(1)}, quest, random.Random(1), _name, NOW) == []
    assert not rpg.quest_active(quest)


# ── the map ──────────────────────────────────────────────────────────────────

class _Walk(_Scripted):
    """Wander steps come from a list of (dx, dy) pairs, then stand still."""
    def __init__(self, steps=(), **kwargs):
        super().__init__(**kwargs)
        self._steps = [d for pair in steps for d in pair]

    def randint(self, low, high):
        if (low, high) == (-1, 1):
            return self._steps.pop(0) if self._steps else 0
        return super().randint(low, high)


def test_only_two_characters_meeting_measure_round_the_edge():
    assert rpg.axis_gap(499, 2) == 4 and rpg.axis_gap(10, 20) == 10   # 499 -> 500 -> 0 -> 1 -> 2
    assert rpg.axis_gap(0, rpg.MAP_SIZE) == 1            # a step apart, not the width of the realm

    # Neighbours across the east edge fight each other like any others…
    assert rpg.in_battle_range({"x": 499, "y": 250}, {"x": 2, "y": 250})
    assert not rpg.in_battle_range({"x": 499, "y": 250}, {"x": rpg.BATTLE_RANGE + 2, "y": 250})

    # …but a town belongs to the corner it is drawn in. Denmark is at (35, 40)
    # and stays a long walk from the far edge, however the ground wraps.
    outside = _char(x=499, y=40)
    assert rpg.market_in_reach(outside) is None
    assert rpg.nearest_town(outside)[1] > rpg.MARKET_RADIUS
    assert rpg.travel_steps(outside, "Denmark") == 464   # the long way, as drawn


def test_a_traveller_walks_the_map_as_drawn():
    walker = _char(x=499, y=40, travel_to="Denmark")
    for _ in range(3):
        walker["x"] = rpg._toward(walker["x"], 35)
    assert walker["x"] == 496                            # west across the sheet, not over the edge


def test_wandering_wraps_at_the_edges_like_the_original():
    chars = {1: _char(x=rpg.MAP_SIZE, y=0)}
    rpg.move_players(chars, rpg.new_quest(), _Walk([(1, -1)], random_=0.999), _name, NOW, 1)
    assert (chars[1]["x"], chars[1]["y"]) == (0, rpg.MAP_SIZE)


def test_paused_characters_do_not_move_and_nobody_moves_with_no_one_online():
    sleeper = _char(x=50, y=50)
    rpg.pause(sleeper, NOW)
    chars = {1: sleeper}
    assert rpg.move_players(chars, rpg.new_quest(), _Walk([(1, 1)] * 5), _name, NOW, 5) == []
    assert (sleeper["x"], sleeper["y"]) == (50, 50)


def test_two_characters_on_one_square_fight_with_a_one_in_online_chance():
    def _meet(random_):
        chars = {
            1: _char(10, left=10_000, x=10, y=10),
            2: _char(10, left=10_000, x=11, y=11, items={"ring": {"level": 30, "name": None}}),
        }
        rng = _Walk([(0, 0), (-1, -1)], random_=random_, randrange=10 ** 9)   # 2 walks onto 1's square
        return chars, rpg.move_players(chars, rpg.new_quest(), rng, _name, NOW, 1)

    chars, notes = _meet(0.4)            # 0.4 × 2 online < 1: they fight
    assert "came upon" in notes[0].text and "[10, 10]" in notes[0].text and notes[0].public
    assert rpg.time_left(chars[2], NOW) == 9300      # the challenger won 7% of their clock

    chars, notes = _meet(0.6)            # 0.6 × 2 online ≥ 1: they pass by
    assert notes == [] and rpg.time_left(chars[2], NOW) == 10_000


def test_journey_questers_walk_to_the_first_waypoint_then_the_second_and_are_paid():
    chars = {1: _char(45, left=10_000, x=33, y=41), 2: _char(45, left=10_000, x=35, y=40), 3: _char(5, x=300, y=300)}
    quest = {**rpg.new_quest(), "members": [1, 2], "description": "walk", "kind": "journey", "p1": [35, 40], "p2": [36, 40]}
    rng = _Walk(random_=0.0)             # every quester steps every second; bystanders stand still

    notes = rpg.move_players(chars, quest, rng, _name, NOW, 2)
    assert (chars[1]["x"], chars[1]["y"]) == (35, 40)   # diagonal first, then straight
    assert quest["stage"] == 1 and notes == []

    notes = rpg.move_players(chars, quest, rng, _name, NOW, 1)   # the second they are all found at p1
    assert quest["stage"] == 2 and "have reached Denmark [35, 40]" in notes[0].text
    assert (chars[1]["x"], chars[2]["x"]) == (35, 35)            # …is spent on that: nobody moved

    notes = rpg.move_players(chars, quest, rng, _name, NOW, 2)
    assert "completed their journey" in notes[-1].text
    assert rpg.time_left(chars[1], NOW) == 7500 and rpg.time_left(chars[3], NOW) == 1000
    assert not rpg.quest_active(quest) and quest["not_before"] == NOW + rpg.QUEST_REST_SECS


def test_questers_do_not_wander_or_collide_while_on_a_journey():
    chars = {1: _char(45, x=10, y=10), 2: _char(45, x=10, y=10)}
    quest = {**rpg.new_quest(), "members": [1, 2], "description": "walk", "kind": "journey", "p1": [400, 400], "p2": [0, 0]}
    notes = rpg.move_players(chars, quest, _Walk([(1, 1)] * 10, random_=0.5), _name, NOW, 5)
    assert notes == [] and (chars[1]["x"], chars[1]["y"]) == (10, 10)   # 0.5 ≥ the 1% step chance


def test_a_started_journey_names_its_waypoints_and_asks_for_the_map():
    class _PickJourney(_Scripted):
        def choice(self, seq):
            return rpg._JOURNEYS[0]
    chars = {1: _char(40), 2: _char(50)}
    quest = rpg.new_quest()
    notes = rpg.tick_quest(chars, quest, _PickJourney(), _name, NOW)
    assert quest["kind"] == "journey" and quest["ends_at"] is None and quest["stage"] == 1
    assert quest["p1"] == list(rpg.LANDMARKS["Denmark"])
    assert notes[0].show_map and notes[0].ping == (1, 2)


def test_every_journey_runs_between_two_real_landmarks():
    for start, end, _text in rpg._JOURNEYS:
        assert start in rpg.LANDMARKS and end in rpg.LANDMARKS and start != end
    for x, y in rpg.LANDMARKS.values():
        assert 0 <= x <= rpg.MAP_SIZE and 0 <= y <= rpg.MAP_SIZE


def test_the_map_renders_a_png_for_an_empty_realm_and_a_busy_one():
    from src.idle_map import render_map
    assert render_map([])[:4] == b"\x89PNG"
    crowd = [(uid, f"player{uid}", uid * 7 % 501, uid * 13 % 501) for uid in range(60)]
    quest = {"members": [1, 2], "stage": 2, "p1": [35, 40], "p2": [410, 80]}
    assert render_map(crowd, highlight=(3,), quest=quest)[:4] == b"\x89PNG"


def test_the_map_background_ships_at_one_pixel_per_map_unit():
    from PIL import Image
    from src import idle_map
    with Image.open(idle_map._BACKGROUND_PATH) as art:
        assert art.size == (rpg.MAP_SIZE, rpg.MAP_SIZE)


def test_characters_made_together_do_not_level_in_step():
    rng = random.Random(2)
    clocks = set()
    for _ in range(7):
        char = rpg.new_character(rpg.UNCLAIMED_CLASS, NOW, claimed=False)
        rpg.stagger_start(char, rng)
        assert NOW + rpg.ttl(0) <= char["next_level_at"] < NOW + rpg.ttl(0) + rpg.START_JITTER_SECS
        clocks.add(char["next_level_at"] // 60)
    assert len(clocks) >= 5                       # seven characters, spread over the minutes


# ── named finds and the bag ──────────────────────────────────────────────────

def test_a_find_is_named_for_what_it_was_worth_to_the_finder():
    assert rpg.item_name("boots", 30, 20, random.Random(11)).endswith(" Boots")
    # The same roll reads as a triumph at one level and as junk at another.
    great = rpg.item_name("ring", 30, 20, random.Random(3)).split()[0]
    poor = rpg.item_name("ring", 3, 60, random.Random(3)).split()[0]
    assert rpg.ITEM_RARITIES.index(great) > rpg.ITEM_RARITIES.index(poor)
    assert poor == rpg.ITEM_RARITIES[0]


def test_a_worse_find_goes_in_the_bag_and_a_full_bag_spills_the_worst():
    char = _char(20, items={"ring": {"level": 99, "name": None}})
    rng = _Scripted(random_=0.999, randrange=1)   # never a unique, lowest roll, choice → "ring"
    note = rpg.find_item(1, char, rng, _name)
    assert "into the bag" in note.text and len(char["loot"]) == 1
    assert char["items"]["ring"]["level"] == 99   # the good one is still worn

    for _ in range(rpg.LOOT_MAX):
        rpg.find_item(1, char, rng, _name)
    assert len(char["loot"]) == rpg.LOOT_MAX
    char["loot"][0]["level"] = 50                 # the worst goes, not the newest
    note = rpg.find_item(1, char, rng, _name)
    assert "left in the road" in note.text
    assert len(char["loot"]) == rpg.LOOT_MAX and char["loot"][-1]["level"] == 50


def test_selling_the_bag_pays_by_item_level_and_empties_it():
    char = _char(20, gold=100, loot=[{"slot": "ring", "level": 4, "name": None},
                                     {"slot": "boots", "level": 6, "name": None}])
    assert rpg.loot_value(char) == 10 * rpg.LOOT_GOLD_PER_LEVEL
    pieces, paid = rpg.sell_loot(char)
    assert (pieces, paid) == (2, 10 * rpg.LOOT_GOLD_PER_LEVEL)
    assert char["loot"] == [] and char["gold"] == 100 + paid
    assert rpg.sell_loot(char) == (0, 0)


def test_the_town_errand_sells_the_bag_even_with_auto_trading_off():
    town = rpg.LANDMARKS["Velvragh"]
    char = _char(10, gold=0, auto_trade=False, x=town[0], y=town[1],
                 loot=[{"slot": "ring", "level": 5, "name": None}])
    note = rpg.auto_trade(1, char, random.Random(1), _name, NOW)
    assert "Sold 1 piece" in note.text and char["gold"] == 5 * rpg.LOOT_GOLD_PER_LEVEL
    assert char["loot"] == [] and "gold spent" not in note.text


# ── titles ───────────────────────────────────────────────────────────────────

def test_a_title_is_earned_once_worn_at_once_and_outlives_the_stat():
    char = _char(10, gold=1000)
    assert rpg.check_titles(1, char, _name) == []
    char["gold"] = 50_000
    notes = rpg.check_titles(1, char, _name)
    assert len(notes) == 1 and "Gold Hoarder" in notes[0].text and notes[0].public
    assert char["title"] == "hoarder" and rpg.title_of(char) == "Gold Hoarder"
    assert rpg.check_titles(1, char, _name) == []      # never twice
    char["gold"] = 0
    assert rpg.title_of(char) == "Gold Hoarder"        # a purse spent doesn't cost you it


def test_a_second_title_does_not_replace_the_one_being_worn():
    char = _char(10, gold=50_000, mob_kills=250)
    rpg.check_titles(1, char, _name)
    assert char["title"] == "hoarder" and set(char["titles"]) == {"hoarder", "hunter"}
    assert rpg.titled(char, "P1") == "P1 the Gold Hoarder"
    assert rpg.titled(_char(1), "P1") == "P1"


# ── the world, blessings and the boost ───────────────────────────────────────

def _world_rng(kind: str):
    """An rng whose every chance fires and whose choice() takes `kind`."""
    class _Rng(random.Random):
        def random(self):
            return 0.0

        def choice(self, seq):
            return kind if kind in seq else list(seq)[0]
    return _Rng(5)


def test_a_world_event_is_told_as_an_omen_then_a_beginning_then_an_end():
    rows = []
    omen = rpg.tick_world(rows, _world_rng("blood_moon"), NOW, 1440, 21)
    assert len(omen) == 1 and omen[0].public and not omen[0].uids
    assert rpg.running_world(rows, NOW) is None          # an omen is not yet weather
    assert rpg.world_effect(rows, NOW) is None

    start = NOW + rpg.WORLD_OMEN_SECS
    began = rpg.tick_world(rows, _world_rng("blood_moon"), start, 1440, 21)
    assert "Blood moon" in began[0].text and rpg.world_effect(rows, start) is not None
    assert rpg.tick_world(rows, _world_rng("blood_moon"), start + 1, 1440, 21) == []   # nothing new to say

    ended = rpg.tick_world(rows, _world_rng("blood_moon"), rows[0]["ends_at"], 1440, 21)
    assert "pale again" in ended[0].text and rows == []


def test_an_event_slept_through_is_still_told_from_both_ends():
    rows = []
    rpg.tick_world(rows, _world_rng("power_hour"), NOW, 1440, 18)
    long_after = rows[0]["ends_at"] + 10_000
    began = rpg.tick_world(rows, _world_rng("power_hour"), long_after, 1440, 18)
    assert "Power hour" in began[0].text and rows            # staged first, never ended unannounced
    ended = rpg.tick_world(rows, _world_rng("power_hour"), long_after, 1440, 18)
    assert "back to its own pace" in ended[0].text and rows == []


def test_only_one_world_event_runs_at_a_time():
    rows = []
    for _ in range(20):
        rpg.tick_world(rows, _world_rng("storm"), NOW, 1440, 3)
    assert len([r for r in rows if r["kind"] in rpg.WORLD_EVENTS]) == 1


def test_a_blood_moon_makes_monsters_stronger_and_richer_everywhere():
    char = _wanderer(level=20, gear=60)
    rules = rpg.WORLD_EVENTS["blood_moon"]
    assert rpg.world_here((rules, ""), "Plains") is rules      # not weather; the moon is over everyone
    assert rpg.mob_power(char, 1.0, rules.mob_power_pct) > rpg.mob_power(char, 1.0)
    assert rules.mob_gold_pct > 0


def test_a_storm_is_only_weather_where_it_is_raining():
    rules = rpg.WORLD_EVENTS["storm"]
    assert rpg.world_here((rules, "Caves"), "Caves") is rules
    assert rpg.world_here((rules, "Caves"), "Plains") is None
    assert rpg.world_here(None, "Caves") is None


def test_an_invasion_puts_its_kind_anywhere_and_never_a_rare_one():
    rows = []
    rpg.tick_world(rows, _world_rng("invasion"), NOW, 1440, 12)
    assert rows[0]["detail"] not in rpg.RARE_KILLS
    effect = (rpg.WORLD_EVENTS["invasion"], "Pirate")   # coast-only, so the biome must give way
    assert rpg.roll_monster(20, "Mountains", _world_rng("x"), effect)[1] == "Pirate"
    # Without the horde, a pirate is not something the mountains produce.
    assert rpg.roll_monster(20, "Mountains", random.Random(2))[1] != "Pirate"


def test_a_blessing_costs_gold_stacks_for_everyone_and_wears_off():
    rows, char = [], _char(10, gold=rpg.BLESS_COST * 2)
    assert rpg.guild_boost_pct(rows, NOW) == 0
    cast, text = rpg.cast_bless(7, char, rows, NOW)
    assert cast and char["gold"] == rpg.BLESS_COST and str(rpg.BLESS_BOOST_PCT) in text
    assert rpg.guild_boost_pct(rows, NOW) == rpg.BLESS_BOOST_PCT

    rpg.cast_bless(8, _char(10, gold=rpg.BLESS_COST), rows, NOW)
    assert rpg.guild_boost_pct(rows, NOW) == rpg.BLESS_BOOST_PCT * 2
    assert rpg.guild_boost_pct(rows, NOW + rpg.BLESS_SECS) == 0     # both have lapsed

    notes = rpg.tick_world(rows, _Scripted(random_=1.0), NOW + rpg.BLESS_SECS, 1440)
    assert len([n for n in notes if "worn off" in n.text]) == 2 and not rows


def test_blessings_stack_only_so_far_and_a_pauper_casts_nothing():
    rows = [{"kind": "bless", "detail": "", "cast_by": i, "stage": 1,
             "starts_at": NOW, "ends_at": NOW + 60} for i in range(rpg.BLESS_MAX + 3)]
    assert rpg.guild_boost_pct(rows, NOW) == rpg.BLESS_BOOST_PCT * rpg.BLESS_MAX
    poor = _char(10, gold=rpg.BLESS_COST - 1)
    cast, why = rpg.cast_bless(1, poor, rows, NOW)
    assert not cast and "costs" in why and poor["gold"] == rpg.BLESS_COST - 1


def test_a_personal_boost_adds_to_the_guilds_and_the_whole_stack_is_capped():
    char = _char(10, boost_pct=50, boost_until=NOW + 600)
    assert rpg.boost_pct(char, 25, NOW) == 75
    assert rpg.boost_pct(char, 25, NOW + 601) == 25          # theirs has run out
    assert rpg.boost_pct(char, 10 ** 4, NOW) == rpg.BOOST_MAX_PCT


def test_a_boost_moves_the_clock_and_pays_on_what_was_earned():
    char = _char(10, left=1000)
    rpg.apply_boost(char, 60, 50)
    assert rpg.time_left(char, NOW) == 1000 - 30
    rpg.pause(char, NOW)
    rpg.apply_boost(char, 60, 50)
    assert char["remaining"] == 970                          # a paused clock is left where it is
    assert rpg.gold_bonus(100, 50) == 50
    assert rpg.gold_bonus(-100, 50) == 0                     # a tick that lost money pays nothing
    assert rpg.gold_bonus(100, 0) == 0


def test_a_godsend_can_hand_out_a_spell_of_good_running():
    char = _char(10)
    chars, rng = {1: char}, random.Random(0)
    for _ in range(500):
        note = rpg.godsend(1, chars, rng, _name, NOW)
        if char["boost_until"] > NOW:
            assert char["boost_pct"] in rpg.BOOST_GODSEND_PCTS and "faster" in note.text
            assert rpg.boost_pct(char, 0, NOW) == char["boost_pct"]
            return
    raise AssertionError("no boost among 500 godsends")


# ── hunts ────────────────────────────────────────────────────────────────────

def _in_town(level=20, **over):
    town = rpg.LANDMARKS["Velvragh"]
    return _char(level, left=10 ** 6, x=town[0], y=town[1], **over)


def test_a_town_hands_out_a_hunt_and_points_it_at_huntable_country():
    char = _in_town()
    note = rpg.offer_hunt(1, char, _world_rng("x"), _name, NOW, 60)
    assert note is not None and "asked to deal with" in note.text
    assert rpg.hunting(char) and rpg.HUNT_MIN <= char["hunt_count"] <= rpg.HUNT_MAX
    # It always walks: nothing spawns inside the ring the errand was given in.
    goal = {"x": char["hunt_x"], "y": char["hunt_y"]}
    assert rpg.market_in_reach(goal) is None
    assert rpg.biome_at(goal["x"], goal["y"]) in rpg.hunt_biomes(char)


def test_a_town_offers_nothing_to_a_character_hunting_resting_or_out_of_reach():
    busy = _in_town(hunt_mob="Rat", hunt_count=3)
    assert rpg.offer_hunt(1, busy, _world_rng("x"), _name, NOW, 60) is None
    rested = _in_town(hunt_at=NOW - rpg.HUNT_REST_SECS + 60)
    assert rpg.offer_hunt(1, rested, _world_rng("x"), _name, NOW, 60) is None
    assert rpg.offer_hunt(1, _wanderer(), _world_rng("x"), _name, NOW, 60) is None   # no town, no errand


def test_the_quarry_pool_widens_with_level_and_is_never_empty():
    assert [b[0] for b in rpg.hunt_quarries(0)] == ["Rat", "Slime", "Crab"]
    assert len(rpg.hunt_quarries(40)) > len(rpg.hunt_quarries(10))


def test_finishing_a_hunt_pays_clock_and_gold_and_starts_the_rest():
    char = _wanderer(level=20, gear=400, gold=0, hunt_mob="Rat", hunt_count=2, hunt_killed=2)
    before = rpg.time_left(char, NOW)
    note = rpg.finish_hunt(1, char, _name, NOW)
    assert "finished the hunt" in note.text and not note.public
    assert char["gold"] == rpg.HUNT_GOLD_PER_KILL_PER_LEVEL * 2 * 20
    assert rpg.time_left(char, NOW) < before and char["hunts_done"] == 1
    assert not rpg.hunting(char) and char["hunt_at"] == NOW


def test_hunting_draws_the_quarry_out_and_a_kill_anywhere_counts(monkeypatch):
    monkeypatch.setattr(rpg, "fight_monster", lambda c, p, hp, rng: (1, True, ""))
    char = _wanderer(level=20, gear=400, hunt_mob="Bat", hunt_count=10 ** 6)
    char["x"], char["y"] = 90, 250              # Caves, where a Bat lives
    assert rpg.biome_at(char["x"], char["y"]) in rpg.hunt_biomes(char)
    for seed in range(40):
        rpg.mob_encounter(1, char, random.Random(seed), _name, NOW)
    drawn_out = char["hunt_killed"]
    assert drawn_out > 0

    # The same character standing where no bat lives meets far fewer of them.
    plain = _wanderer(level=20, gear=400, hunt_mob="Bat", hunt_count=10 ** 6)
    plain["x"], plain["y"] = 440, 150           # Plains: not Bat country
    for seed in range(40):
        rpg.mob_encounter(1, plain, random.Random(seed), _name, NOW)
    assert plain["hunt_killed"] < drawn_out


def test_a_hunter_keeps_to_the_quarry_s_country():
    # (170, 320) is the corner of Haunted country: a step west or south leaves it.
    char = _wanderer(hunt_mob="Zombie", hunt_count=5)
    char["x"], char["y"] = 170, 320
    chars = {1: char}
    rpg.move_players(chars, rpg.new_quest(), _Walk([(-1, -1)] * 100, random_=0.5), _name, NOW, 100)
    assert (char["x"], char["y"]) == (170, 320)      # every step out was refused
    rpg.move_players(chars, rpg.new_quest(), _Walk([(1, 1)] * 5, random_=0.5), _name, NOW, 5)
    assert (char["x"], char["y"]) == (175, 325)      # steps inside are taken as usual
    assert char["hunt_x"] is None                    # still hunting, not walking


def test_a_hunter_found_outside_its_country_is_steered_back():
    # A hunt from before the steering held, or a character carried to a town
    # after a fall: no hunt_x, standing in Caves country with Zombies to find.
    char = _wanderer(hunt_mob="Zombie", hunt_count=5)
    char["x"], char["y"] = 100, 300
    chars = {1: char}
    notes = rpg.move_players(chars, rpg.new_quest(), _Walk(random_=0.0), _name, NOW, 1)
    assert [n.text for n in notes] == ["📜 P1 has strayed from the hunt and heads back to Haunted country for the Zombies."]
    assert char["hunt_x"] is not None and (char["x"], char["y"]) == (101, 301)
    notes = rpg.move_players(chars, rpg.new_quest(), _Walk(random_=0.0), _name, NOW, 60)
    assert any("reached Haunted country" in n.text for n in notes)
    assert char["hunt_x"] is None and rpg.hunting_ground(char, char["x"], char["y"])
    assert not any("strayed" in n.text for n in notes)   # said once, when it set off


def test_a_finished_hunt_is_reported_by_the_encounter_that_finished_it(monkeypatch):
    monkeypatch.setattr(rpg, "fight_monster", lambda c, p, hp, rng: (1, True, ""))
    monkeypatch.setattr(rpg, "beast_named", lambda kind, rng: ("Normal", kind, 1.0, 1, 1))
    char = _wanderer(level=20, gear=400, gold=0, hunt_mob="Bat", hunt_count=1)
    char["x"], char["y"] = 90, 250
    texts = [n.text for n in rpg.mob_encounter(1, char, _world_rng("x"), _name, NOW)]
    assert any("finished the hunt" in t for t in texts) and not rpg.hunting(char)


# ── initiative, and what a fall costs ────────────────────────────────────────

def test_initiative_leans_to_the_stronger_side_without_ever_being_certain():
    rng = random.Random(4)
    even = sum(rpg.strikes_first(100, 100, rng) for _ in range(2000))
    strong = sum(rpg.strikes_first(300, 100, rng) for _ in range(2000))
    weak = sum(rpg.strikes_first(100, 300, rng) for _ in range(2000))
    assert 900 < even < 1100                       # a match is a coin toss
    assert 1400 < strong < 1600 and 400 < weak < 600
    # Scale-free: the share is what counts, not the gap.
    assert abs(sum(rpg.strikes_first(3, 1, rng) for _ in range(2000)) - strong) < 150


def test_a_monster_can_land_the_first_blow(monkeypatch):
    """The character used to swing first every round unconditionally, so
    anything killed by an opening blow never swung back at all."""
    monkeypatch.setattr(rpg, "strikes_first", lambda mine, theirs, rng: False)
    char = _wanderer(level=20, gear=60)
    char["hp"] = 2                                 # one blow from anything is fatal
    _rounds, killed, _marks = rpg.fight_monster(char, 10_000, 1, random.Random(1))
    assert not killed and rpg.hp_of(char) <= 0     # it got there first


def test_being_struck_down_costs_the_bag_and_says_so_in_plain_words(monkeypatch):
    monkeypatch.setattr(rpg, "roll_monster", lambda level, biome, rng, effect=None: ("Normal", "Rat", 50.0, 1, 3))
    def _fatal(char, their_power, their_hp, rng):
        char["hp"] = 0
        return 2, False, "··"
    monkeypatch.setattr(rpg, "fight_monster", _fatal)
    char = _wanderer(level=20, gear=60, gold=1200)
    char["loot"] = [{"slot": "ring", "level": 10, "name": "Crude Iron Ring"},
                    {"slot": "boots", "level": 5, "name": "Plain Bone Boots"}]
    worth = rpg.loot_value(char)

    notes = rpg.mob_encounter(1, char, random.Random(2), _name, NOW)
    text = next(n.text for n in notes if n.text.startswith("☠"))

    assert char["loot"] == [] and char["mob_deaths"] == 1
    assert "2 pieces lost from their bag" in text and f"(worth {worth:,} gold)" in text
    # Every number says which way it went, and the clock move is a signed
    # amount at the very end rather than prose in the middle of the line.
    assert "gold lost" in text and "added to their clock" not in text
    assert re.search(r"\+(\d+(day|hr|min|sec) ?)+$", text)


def test_a_kill_line_marks_its_gold_as_a_gain(monkeypatch):
    monkeypatch.setattr(rpg, "fight_monster", lambda c, p, hp, rng: (1, True, ""))
    char = _wanderer(level=20, gear=400)
    text = rpg.mob_encounter(1, char, random.Random(5), _name, NOW)[0].text
    assert "+" in text.split("gold")[0].split(".")[-1]


def test_a_clock_move_is_a_signed_amount_and_nothing_when_it_is_zero():
    assert rpg.clock_delta(63) == "+1min 3sec" and rpg.clock_delta(-63) == "-1min 3sec"
    assert rpg.clock_delta(3) == "+3sec" and rpg.clock_delta(-90_000) == "-1day 1hr"
    assert rpg.clock_delta(0) == "" and rpg.clock_tail(0) == ""     # no move, nothing said
    assert rpg.clock_tail(-3) == " -3sec"                           # ready to append


def test_monster_lines_get_the_right_article():
    assert rpg._a("Elite Bat") == "an Elite Bat" and rpg._a("Normal Rat") == "a Normal Rat"
    assert rpg._a("Undead Ogre").startswith("an") and rpg._a("Omega Rat").startswith("an")
    # str.capitalize() would have lowercased the beast's own name with it.
    assert rpg._sentence(rpg._a("Elite Bat")) == "An Elite Bat"


def test_a_better_find_puts_what_it_replaced_in_the_bag():
    char = _wanderer(level=30, gear=0)
    char["items"] = {slot: {"level": 1, "name": f"Cracked Wooden {slot.title()}"} for slot in rpg.ITEM_SLOTS}
    text = rpg.find_item(1, char, random.Random(2), _name).text
    worn = next(item for item in char["loot"])
    assert worn["level"] == 1 and "is now in their bag" in text
    assert char["items"][worn["slot"]]["level"] > 1                 # …and the better one is worn


def test_an_empty_slot_says_so_rather_than_quoting_level_zero():
    char = _wanderer(level=30, gear=0)
    char["items"] = {}
    text = rpg.find_item(1, char, random.Random(2), _name).text
    assert "their first" in text and "level 0" not in text and char["loot"] == []


# ── events keep hours ────────────────────────────────────────────────────────

def test_each_kind_of_world_event_keeps_its_own_hours():
    assert rpg.world_kinds_at(21) == sorted(["blood_moon", "power_hour", "storm"])
    assert rpg.world_kinds_at(12) == sorted(["invasion", "storm"])
    assert rpg.world_kinds_at(3) == ["storm"]          # the small hours: only weather
    for hour in range(24):
        assert rpg.world_kinds_at(hour), hour          # something is always possible


def test_a_blood_moon_only_rises_at_night():
    day, night = [], []
    for hour, into in ((12, day), (21, night)):
        for seed in range(30):
            rows = []
            rpg.tick_world(rows, _world_rng("blood_moon"), NOW, 1, hour)
            into.append(rows[0]["kind"])
    assert "blood_moon" not in day and "blood_moon" in night


# ── signature drops ──────────────────────────────────────────────────────────

def test_only_the_rare_kills_leave_a_trophy_and_it_lands_or_is_bagged():
    rng = random.Random(1)
    char = _wanderer(level=40, gear=5)
    for _ in range(50):
        note = rpg.signature_drop(1, char, "Dragon", rng, _name)
        if note is not None:
            break
    assert note is not None and "Wingcase Shield" in note.text and note.public
    assert char["items"]["shield"]["name"] == "Wingcase Shield"

    # A second one, beneath what is already worn, goes in the bag instead.
    char["items"]["shield"] = {"level": 10 ** 4, "name": None}
    for _ in range(50):
        note = rpg.signature_drop(1, char, "Dragon", rng, _name)
        if note is not None and "into the bag" in note.text:
            break
    assert char["loot"] and char["loot"][-1]["name"] == "Wingcase Shield"


def test_an_ordinary_monster_leaves_no_trophy():
    rng = random.Random(1)
    char = _wanderer(level=40, gear=5)
    assert all(rpg.signature_drop(1, char, "Rat", rng, _name) is None for _ in range(200))


def test_every_trophy_belongs_to_a_rare_kill_and_a_real_slot():
    for kind, (slot, title, low, high) in rpg.SIGNATURE_DROPS.items():
        assert kind in rpg.RARE_KILLS and slot in rpg.ITEM_SLOTS and 0 < low < high
        assert title in rpg.SIGNATURE_NAMES


def test_rare_kills_are_rare_monsters():
    # The Golem (rarity 60) was once "rare" and filled the channel with
    # trophies; anything a level-30 character meets every other day is not news.
    rarity = {kind: r for kind, r, *_rest in rpg.MOB_TYPES}
    assert rpg.RARE_KILLS <= set(rarity)
    assert all(rarity[kind] < 50 for kind in rpg.RARE_KILLS), sorted(rpg.RARE_KILLS, key=rarity.get)


# ── lore and flavour ─────────────────────────────────────────────────────────

def test_every_place_on_the_map_has_lore_and_it_is_not_boilerplate():
    assert set(rpg.LORE) == set(rpg.LANDMARKS)
    for place, text in rpg.LORE.items():
        assert len(text) > 120 and text.strip() == text, place
    assert len(set(rpg.LORE.values())) == len(rpg.LORE)     # no two places share a paragraph


def test_camping_does_not_say_the_same_thing_every_time():
    char = _wanderer(level=20, gear=60)
    seen = set()
    for seed in range(60):
        char["hp"] = 1
        note = rpg.mob_encounter(1, char, random.Random(seed), _name, NOW)[0]
        assert note.text.startswith("⛺")
        seen.add(note.text.split(". ")[0])
    assert len(seen) >= 5


# ── the standings board ──────────────────────────────────────────────────────

def test_a_board_column_ranks_by_its_own_number_and_skips_the_scoreless():
    chars = {
        1: _char(10, gold=500, mob_kills=3),
        2: _char(10, gold=900, mob_kills=0),
        3: _char(10, gold=0, mob_kills=9),
    }
    _heading, read_gold, show = rpg.BOARD_COLUMNS[0]
    assert rpg.board_ranking(chars, read_gold) == [(2, 900), (1, 500)]   # uid 3 has none
    assert show(900) == "900"
    _heading, read_kills, _show = rpg.BOARD_COLUMNS[1]
    assert rpg.board_ranking(chars, read_kills) == [(3, 9), (1, 3)]


def test_a_board_tie_is_broken_on_the_lower_id_so_it_stops_shuffling():
    chars = {7: _char(10, gold=100), 3: _char(10, gold=100)}
    _heading, read, _show = rpg.BOARD_COLUMNS[0]
    assert rpg.board_ranking(chars, read) == [(3, 100), (7, 100)]


def test_the_table_column_shows_which_way_a_gambler_is_up():
    _heading, read, show = rpg.BOARD_COLUMNS[4]
    assert show(read(_char(10, gamble_won=500, gamble_lost=200))) == "+300"
    assert show(read(_char(10, gamble_won=100, gamble_lost=900))) == "-800"
