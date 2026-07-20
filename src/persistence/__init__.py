"""Persistence package — DB-backed save/load layer.

Public surface preserved exactly: every name that used to live on the flat
`src.persistence` module is re-exported here, so existing
`from src.persistence import X` imports keep working unchanged.

The `_init_db_state_done` flag intentionally lives on this package (not on
the `init` submodule) so test fixtures can flip it via
`src.persistence._init_db_state_done = False`.
"""

from src.db import get_pool, with_cursor  # noqa: F401
from src.guild_config import get_guild_cfg  # noqa: F401

from src.persistence._helpers import _load_json  # noqa: F401
from src.persistence.economy import (  # noqa: F401
    _economy_user_row,
    _ECONOMY_UPSERT_SQL,
    save_economy,
    save_guild_house,
    save_insurance,
    save_jackpot,
)
from src.persistence.settings import (  # noqa: F401
    save_guild_settings,
    save_bot_roles,
    save_godmode_users,
    save_bot_settings,
)
from src.persistence.shop_effects import (  # noqa: F401
    save_ragebait,
    save_mock,
    save_curse,
    save_tax,
    save_spellcheck,
)
from src.persistence.artifacts import save_user_artifact  # noqa: F401
from src.persistence.rigged import (  # noqa: F401
    save_rigged_slots,
    save_rigged_flips,
    save_rigged_scratch,
    save_rigged_steal,
)
from src.persistence.streaks import save_command_streak, save_gambler_streak  # noqa: F401
from src.persistence.recap_usage import save_recap_usage  # noqa: F401
from src.persistence.voice_pings import (  # noqa: F401
    save_voice_ping,
    delete_voice_ping,
    update_voice_ping_last_pinged,
    load_voice_pings,
    save_voice_ping_ignore,
    delete_voice_ping_ignore,
    load_voice_ping_ignores,
)
from src.persistence.notable_events import (  # noqa: F401
    log_notable_event,
    load_notable_events_today,
    prune_notable_events,
)
from src.persistence.chess import (  # noqa: F401
    save_chess_game,
    save_chess_games,
    delete_chess_game,
    save_chess_report,
    load_chess_report,
    load_head_to_head,
    load_bot_head_to_head,
    count_pvp_wins_in_guild,
)
from src.persistence.ai import save_ai_threads, save_channel_prompts  # noqa: F401
from src.persistence.quotes import (  # noqa: F401
    save_quote_log,
    save_saved_quotes,
    load_saved_quotes,
)
from src.persistence.leveling import save_leveling  # noqa: F401
from src.persistence.lottery import load_lottery, save_lottery  # noqa: F401
from src.persistence.records import (  # noqa: F401
    load_global_records,
    load_records,
    save_records,
    try_set_record,
    is_global_top,
)
from src.persistence.history import (  # noqa: F401
    load_balance_history,
    save_balance_history,
    load_bot_stats_history,
    save_bot_stats_history,
    load_command_usage_history,
    save_command_usage_history,
    load_crime_history,
    load_gambling_history,
    prune_balance_history,
    prune_bot_stats_history,
    prune_command_usage_history,
    prune_crime_history,
    prune_gambling_history,
    prune_levelup_history,
    upsert_crime_delta,
    upsert_gambling_delta,
    upsert_levelup_delta,
    load_today_crime_row,
    load_today_gambling_row,
    load_today_levelups_row,
    load_levelup_history,
)
from src.persistence.command_perms import save_command_perms  # noqa: F401
from src.persistence.mc_ping import (  # noqa: F401
    save_mc_ping_sample,
    load_mc_ping_samples,
    prune_mc_ping_samples,
    record_mc_player_event,
    load_mc_player_events,
    prune_mc_player_events,
    upsert_mc_daily_player_stats,
    load_mc_daily_player_stats,
    prune_mc_daily_player_stats,
)
from src.persistence.daily_counters import (  # noqa: F401
    bump_daily_counter,
    load_daily_counter,
    prune_daily_counters,
)
from src.persistence.user_perm_overrides import (  # noqa: F401
    save_user_perm_override,
    delete_user_perm_override,
)
from src.persistence.blocklist import (  # noqa: F401
    save_blocklist,
    delete_blocklist,
    save_global_blocklist,
    delete_global_blocklist,
)
from src.persistence.ephemeral import (  # noqa: F401
    save_restart_msg,
    load_restart_msg,
    clear_restart_msg,
    add_ephemeral_msg,
    load_and_clear_ephemeral_msgs,
)
from src.persistence.issues import (  # noqa: F401
    insert_issue,
    get_issue_by_message,
    get_issue_by_id,
    update_issue_status,
    list_issues,
    soft_delete_issue,
    insert_error_mute,
    delete_error_mute,
    load_error_mutes,
)
from src.persistence.bounties import (  # noqa: F401
    insert_bounty,
    get_bounty_by_message,
    update_bounty,
    insert_claim,
    update_claim,
    get_claim_by_dm,
    get_claim_by_contest,
    get_claim_by_poll,
    load_active_bounties,
)
from src.persistence.feature_requests import (  # noqa: F401
    insert_feature_request,
    get_feature_request_by_message,
    get_feature_request_by_feature_id,
    update_feature_request_status,
    link_feature_to_request,
)
from src.persistence.init import init_db_state  # noqa: F401

import asyncio as _asyncio

# Module-level guard, owned by the package so tests can patch it directly.
_init_db_state_done = False

# Set when init_db_state has finished loading state from the DB. on_message
# awaits this so messages received between gateway-ready and state-load can't
# trigger _ensure_user / _grant_xp against an empty in-memory state and
# overwrite the DB row with zeros.
init_done = _asyncio.Event()
