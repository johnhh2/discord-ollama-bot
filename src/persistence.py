import os
import json
import time

from src.config import (
    CHANNEL_PROMPTS_FILE, ECONOMY_FILE, BOT_ROLES_FILE, BOT_ADMINS_FILE,
    BOT_SETTINGS_FILE, GUILD_SETTINGS_FILE, INSURANCE_FILE, SLOT_JACKPOT_FILE,
    SLOT_JACKPOT_SEED, GODMODE_USERS_FILE, CHESS_GAMES_FILE, RAGEBAIT_FILE,
    MOCK_FILE, RIGGED_SLOTS_FILE, RIGGED_FLIPS_FILE, RIGGED_SCRATCH_FILE, RIGGED_STEAL_FILE, QUOTE_LOG_FILE, SAVED_QUOTES_FILE, SIMP_FILE,
    CURSE_FILE, GAMBLER_STREAK_FILE, EPHEMERAL_MSG_FILE, FANFIC_HISTORIES_FILE,
    FANFIC_OWNERS_FILE, ROLEPLAY_STATE_FILE, INITIAL_BOT_ADMIN_ID, LEVELING_FILE,
    COMMAND_PERMS_FILE,
)


def _load_json(filepath, default):
    os.makedirs("data", exist_ok=True)
    try:
        with open(filepath) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(filepath, data):
    os.makedirs("data", exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)


def load_channel_prompts() -> dict:
    if os.path.exists(CHANNEL_PROMPTS_FILE):
        with open(CHANNEL_PROMPTS_FILE) as f:
            return {int(k): v for k, v in json.load(f).items()}
    return {}


def save_channel_prompts(prompts: dict):
    with open(CHANNEL_PROMPTS_FILE, "w") as f:
        json.dump({str(k): v for k, v in prompts.items()}, f, indent=2)


def load_roleplay_state():
    # Imported here to avoid circular import with state
    from src import state
    raw = _load_json(ROLEPLAY_STATE_FILE, {"roleplays": {}, "histories": {}})
    roleplays = {}
    for k, v in raw.get("roleplays", {}).items():
        v["participants"] = set(v.get("participants", []))
        roleplays[int(k)] = v
    histories = {int(k): v for k, v in raw.get("histories", {}).items()}
    return roleplays, histories


def save_roleplay_state():
    from src import state
    _save_json(ROLEPLAY_STATE_FILE, {
        "roleplays": {
            str(k): {**v, "participants": list(v.get("participants", set()))}
            for k, v in state.active_roleplays.items()
        },
        "histories": {str(k): v for k, v in state.roleplay_histories.items()},
    })


def load_fanfic_histories() -> dict:
    raw = _load_json(FANFIC_HISTORIES_FILE, {})
    return {int(k): v for k, v in raw.items()}


def save_fanfic_histories():
    from src import state
    _save_json(FANFIC_HISTORIES_FILE, {
        str(k): list(v) for k, v in state.channel_histories.items()
        if k in state.fanfic_thread_ids
    })
    _save_json(FANFIC_OWNERS_FILE, {
        str(k): {"owner_id": v["owner_id"], "invited_ids": list(v["invited_ids"])}
        for k, v in state.fanfic_owners.items()
    })


def load_fanfic_owners() -> dict:
    raw = _load_json(FANFIC_OWNERS_FILE, {})
    return {
        int(k): {"owner_id": v["owner_id"], "invited_ids": set(v["invited_ids"])}
        for k, v in raw.items()
    }


def load_economy() -> dict:
    data = _load_json(ECONOMY_FILE, {"users": {}, "last_daily_reset": None})
    data.setdefault("last_daily_reset", None)
    data.setdefault("guild_house", {})
    return data


def save_economy():
    from src import state
    _save_json(ECONOMY_FILE, state.economy)


def load_jackpot() -> int:
    return max(SLOT_JACKPOT_SEED, _load_json(SLOT_JACKPOT_FILE, {}).get("jackpot", SLOT_JACKPOT_SEED))


def save_jackpot(value: int):
    _save_json(SLOT_JACKPOT_FILE, {"jackpot": value})


def load_bot_roles() -> set:
    return set(_load_json(BOT_ROLES_FILE, []))


def save_bot_roles():
    from src import state
    _save_json(BOT_ROLES_FILE, list(state.bot_roles))


def load_bot_admins() -> set:
    return set(_load_json(BOT_ADMINS_FILE, [INITIAL_BOT_ADMIN_ID]))


def save_bot_admins():
    from src import state
    _save_json(BOT_ADMINS_FILE, list(state.bot_admins))


def load_bot_settings() -> dict:
    return _load_json(BOT_SETTINGS_FILE, {"vram_text": "16GB"})


def save_bot_settings():
    from src import state
    _save_json(BOT_SETTINGS_FILE, state.bot_settings)


def load_godmode_users() -> set:
    return set(_load_json(GODMODE_USERS_FILE, []))


def save_godmode_users():
    from src import state
    _save_json(GODMODE_USERS_FILE, list(state.godmode_users))


def load_chess_games() -> dict:
    return {int(k): v for k, v in _load_json(CHESS_GAMES_FILE, {}).items()}


def save_chess_games():
    from src import state
    _save_json(CHESS_GAMES_FILE, state.active_chess_games)


def load_guild_settings() -> dict:
    return _load_json(GUILD_SETTINGS_FILE, {})


def save_guild_settings():
    from src import state
    _save_json(GUILD_SETTINGS_FILE, state.guild_settings)


def get_guild_cfg(guild_id: int) -> dict:
    # Access through src module so monkeypatching src.guild_settings in tests works.
    import src as _src
    gs = _src.guild_settings
    key = str(guild_id)
    if key not in gs:
        gs[key] = {}
    return gs[key]


def load_insurance() -> dict:
    data = _load_json(INSURANCE_FILE, {})
    now = time.time()
    return {k: v for k, v in data.items() if v.get("expires_at", 0) > now}


def save_insurance():
    from src import state
    _save_json(INSURANCE_FILE, state.insurance)


def load_ragebait() -> dict:
    return _load_json(RAGEBAIT_FILE, {})


def save_ragebait():
    from src import state
    _save_json(RAGEBAIT_FILE, state.active_ragebaits)


def load_mock() -> dict:
    return _load_json(MOCK_FILE, {})


def save_mock():
    from src import state
    _save_json(MOCK_FILE, state.active_mocks)


def load_rigged_slots() -> dict:
    raw = _load_json(RIGGED_SLOTS_FILE, {})
    # migrate old format (list of ints) to dict {uid: symbol}
    if isinstance(raw, list):
        return {str(uid): "7️⃣" for uid in raw}
    return {str(k): v for k, v in raw.items()}


def save_rigged_slots():
    from src import state
    _save_json(RIGGED_SLOTS_FILE, {str(k): v for k, v in state.rigged_slots.items()})


def load_rigged_flips() -> dict:
    return {int(k): v for k, v in _load_json(RIGGED_FLIPS_FILE, {}).items()}


def save_rigged_flips():
    from src import state
    _save_json(RIGGED_FLIPS_FILE, {str(k): v for k, v in state.rigged_flips.items()})


def load_rigged_scratch() -> dict:
    # uid (int) → number of symbols to match (1-4)
    return {int(k): v for k, v in _load_json(RIGGED_SCRATCH_FILE, {}).items()}


def save_rigged_scratch():
    from src import state
    _save_json(RIGGED_SCRATCH_FILE, {str(k): v for k, v in state.rigged_scratch.items()})


def load_rigged_steal() -> dict:
    # uid (int) → remaining rigged successes
    return {int(k): v for k, v in _load_json(RIGGED_STEAL_FILE, {}).items()}


def save_rigged_steal():
    from src import state
    _save_json(RIGGED_STEAL_FILE, {str(k): v for k, v in state.rigged_steal.items()})


def load_gambler_streak() -> dict:
    return _load_json(GAMBLER_STREAK_FILE, {})


def save_gambler_streak():
    from src import state
    _save_json(GAMBLER_STREAK_FILE, state.gambler_streak)


def load_quote_log() -> list:
    return _load_json(QUOTE_LOG_FILE, [])


def save_quote_log(log: list):
    import src as _src
    _src._save_json(_src.QUOTE_LOG_FILE, log[-10:])


def load_saved_quotes() -> dict:
    return _load_json(SAVED_QUOTES_FILE, {})


def save_saved_quotes(quotes: dict):
    _save_json(SAVED_QUOTES_FILE, quotes)


def load_simp() -> dict:
    return {int(k): v for k, v in _load_json(SIMP_FILE, {}).items()}


def save_simp(simp_data: dict):
    _save_json(SIMP_FILE, simp_data)


def load_curse() -> dict:
    return {int(k): v for k, v in _load_json(CURSE_FILE, {}).items()}


def save_curse(curse_data: dict):
    _save_json(CURSE_FILE, curse_data)


def load_lottery(guild_id: int) -> dict:
    return _load_json(f"data/lottery_{guild_id}.json", {"prize_pool": 0, "players": {}, "last_posted_week": 0})


def save_lottery(guild_id: int, lottery_data: dict):
    _save_json(f"data/lottery_{guild_id}.json", lottery_data)


def load_leveling() -> dict:
    """Load leveling data. Structure: {uid_str: {xp, level, msg_last_hour, msg_today, cmd_last_hour, cmd_today, voice_last_15, voice_today}}"""
    return _load_json(LEVELING_FILE, {})


def save_leveling():
    from src import state
    _save_json(LEVELING_FILE, state.leveling)


def load_command_perms() -> dict:
    return _load_json(COMMAND_PERMS_FILE, {})


def save_command_perms():
    from src import state
    _save_json(COMMAND_PERMS_FILE, state.command_perms)


def load_records(guild_id: int) -> dict:
    return _load_json(f"data/records_{guild_id}.json", {})


def save_records(guild_id: int, records: dict):
    _save_json(f"data/records_{guild_id}.json", records)


def try_set_record(guild_id: int, category: str, value: int, holder_id: int, holder_name: str, **meta) -> bool:
    """Update a record if value exceeds the current record. Returns True if a new record was set."""
    if guild_id is None:
        return False
    records = load_records(guild_id)
    current = records.get(category, {})
    if value > current.get("value", -1):
        records[category] = {"value": value, "holder_id": holder_id, "holder_name": holder_name, **meta}
        save_records(guild_id, records)
        return True
    return False
