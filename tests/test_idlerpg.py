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
    rpg.voice_speedup(char, 60)
    assert rpg.time_left(char, NOW) == 994
    rpg.pause(char, NOW)
    rpg.voice_speedup(char, 60)
    assert char["remaining"] == 994
    assert rpg.voice_gold_bonus(70) == 7 and rpg.voice_gold_bonus(9) == 0 and rpg.voice_gold_bonus(-50) == 0


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
    assert not any(item.get("name") for item in low["items"].values())
    high = _char(25)
    note = rpg.find_item(1, high, rng, _name)
    assert high["items"]["helm"]["name"] == rpg.UNIQUES[0][2] and note.public


# ── battles ──────────────────────────────────────────────────────────────────

def _rolls(*values):
    """A randint that hands out `values` in order (capped at the roll's maximum)."""
    queue = list(values)
    return lambda low, high: min(queue.pop(0), high)


def test_a_lone_player_fights_the_house_and_a_win_shortens_the_clock():
    chars = {1: _char(30, left=10_000, items={"ring": {"level": 50, "name": None}})}
    notes = rpg.level_up_battle(1, chars, _Scripted(randint=_rolls(50, 0)), _name, NOW)
    assert rpg.HOUSE_NAME in notes[0].text and notes[0].public
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
            1: _char(25, left=10_000, items={"ring": {"level": 40, "name": None}}),   # 25: a fight on every level-up
            2: _char(25, left=10_000, items={"ring": {"level": 40, "name": None}}),
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
    chars = {1: _char(30), 2: _char(30)}
    rng = _Scripted(randrange=0)                      # always the first in the pool
    first = rpg.level_up_battle(1, chars, rng, _name, NOW)
    assert 2 in first[0].uids and chars[2]["challenged_at"] == NOW
    again = rpg.level_up_battle(1, chars, rng, _name, NOW + 600)
    assert again[0].uids == (1,) and rpg.HOUSE_NAME in again[0].text
    later = rpg.level_up_battle(1, chars, rng, _name, NOW + rpg.CHALLENGED_COOLDOWN_SECS)
    assert 2 in later[0].uids


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


def test_a_tied_duel_is_a_coin_toss_and_the_rolls_read_plainly():
    def _fight(random_):
        chars = {1: _char(0, left=600), 2: _char(0, left=600)}
        notes = rpg.duel(1, 2, chars, _Scripted(random_=random_), _name, NOW)
        return chars, notes[0].text

    chars, text = _fight(0.4)
    assert rpg.time_left(chars[2], NOW) == 630 and "P1 wins" in text
    chars, text = _fight(0.6)                       # no gear either side: the challenger can lose
    assert rpg.time_left(chars[1], NOW) == 630 and "P2 wins" in text
    assert "(no gear)" in text and "coin toss" in text and "[0/0]" not in text


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

    chars = {1: _char(30, items={"ring": {"level": 50, "name": None}}), 2: _char(12)}
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
    assert "plus 50 from P1's purse" in notes[0].text


def test_a_finished_quest_pays_every_quester_by_party_size():
    chars = {1: _char(45), 2: _char(45), 3: _char(45)}
    quest = {**rpg.new_quest(), "members": [1, 2, 3], "description": "wait", "kind": "vigil", "ends_at": NOW}
    notes = rpg.tick_quest(chars, quest, _Scripted(), _name, NOW)
    assert [c["gold"] for c in chars.values()] == [750, 750, 750] and "750 gold" in notes[0].text


def test_godsends_and_calamities_are_sometimes_about_gold():
    chars = {1: _char(10, left=10_000, gold=1000)}
    rng = _Scripted(random_=0.15, randint=lambda low, high: high)   # past the 10% item roll, inside the 20% gold one
    assert "1,200" not in rpg.godsend(1, chars, rng, _name, NOW).text and chars[1]["gold"] == 1200
    assert "120 gold gone" in rpg.calamity(1, chars, rng, _name, NOW).text and chars[1]["gold"] == 1080
    assert rpg.time_left(chars[1], NOW) == 10_000

    broke = {1: _char(10, left=10_000)}
    rpg.calamity(1, broke, rng, _name, NOW)      # nothing to steal: the ordinary kind
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

class _Fight(_Scripted):
    """rolls: my roll, then the monster's, per fight. No groups, no drops."""
    def __init__(self, rolls, *, random_=0.999, randrange=99):
        super().__init__(random_=random_, randrange=randrange)
        self._rolls = list(rolls)

    def randint(self, low, high):
        return min(self._rolls.pop(0), high)


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
                prefix, beast, strength, gold_mult, tier = rpg.roll_monster(level, biome, rng)
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


def test_a_won_fight_pays_gold_and_clock_and_a_town_is_safe():
    char = _char(20, left=10_000, x=480, y=20, items={"ring": {"level": 60, "name": None}})
    notes = rpg.mob_encounter(1, char, _Fight([60, 0]), _name, NOW)
    # randrange 99 → the commonest pool; choice takes its first: a Starving Rat, tier 1.
    assert "killed a Starving Rat" in notes[0].text and "[Plains]" in notes[0].text and not notes[0].public
    assert char["gold"] == 10 and rpg.time_left(char, NOW) == 9900 and char["mob_kills"] == 1   # 1 × 0.25 × 20 × 2

    safe = _char(20, x=rpg.LANDMARKS["Velvragh"][0] + 30, y=rpg.LANDMARKS["Velvragh"][1])
    assert rpg.mob_encounter(1, safe, _Fight([60, 0]), _name, NOW) == []


def test_a_close_fight_ends_with_someone_fleeing_and_nothing_lost():
    char = _char(20, left=10_000, x=480, y=20, gold=500, items={"ring": {"level": 60, "name": None}})
    notes = rpg.mob_encounter(1, char, _Fight([30, 28]), _name, NOW)
    assert "fled" in notes[0].text
    assert (char["gold"], rpg.time_left(char, NOW), char["mob_kills"], char["mob_deaths"]) == (500, 10_000, 0, 0)


def test_a_lost_fight_costs_clock_and_gold_and_carries_you_to_a_towns_outskirts():
    char = _char(20, left=10_000, x=480, y=150, gold=1200, items={"ring": {"level": 60, "name": None}})
    notes = rpg.mob_encounter(1, char, _Fight([0, 99]), _name, NOW)
    assert "struck P1 down" in notes[-1].text and "outskirts of the land of Qwok" in notes[-1].text
    assert char["gold"] == 1100 and rpg.time_left(char, NOW) == 10_100 and char["mob_deaths"] == 1   # 1/12 of 1,200
    rich = _char(20, x=480, y=150, gold=60_000, items={"ring": {"level": 60, "name": None}})
    rpg.mob_encounter(1, rich, _Fight([0, 99]), _name, NOW)
    assert rich["gold"] == 59_900                               # capped at 5 × level
    assert rpg.nearest_town(char)[1] <= rpg.MOB_RESPAWN_DISTANCE
    assert rpg.market_in_reach(char) == "the land of Qwok"
    assert rpg.nearest_town(char)[1] > rpg.TOWN_CORE_RADIUS    # the market, not the errand


def test_a_group_is_fought_one_at_a_time_and_a_rare_kill_is_news():
    char = _char(40, left=100_000, x=480, y=20, items={"ring": {"level": 60, "name": None}})
    rng = _Fight([3, 60, 0, 60, 0, 60, 0], random_=0.0)        # a group of three, all beaten; then the drop roll hits
    notes = rpg.mob_encounter(1, char, rng, _name, NOW)
    assert notes[0].text.count("Starving Rat") == 3 and char["mob_kills"] == 3
    assert len(notes) == 2 and "found a level" in notes[1].text   # a won fight can turn up an item

    class _DragonSlayer(_Fight):
        def choice(self, seq):
            dragons = [entry for entry in seq if entry[0] == "Dragon"]
            return dragons[0] if dragons else seq[0]
    hunter = _char(60, left=100_000, x=300, y=100, items={"ring": {"level": 60, "name": None}})
    notes = rpg.mob_encounter(1, hunter, _DragonSlayer([60, 0], randrange=0), _name, NOW)
    assert "Dragon" in notes[0].text and notes[0].public


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
