"""Every tool runs against the real schema with seeded rows, and nothing a
tool returns may carry a denied key or a withheld column."""
import json

import pytest
from mcp.client import Client

from botmcp import catalog, redact, server, tools
from botmcp.tests.conftest import seed

SENSITIVE_VALUES = ("TYPED-JAIL-REASON", "TYPED-CONDITION", "TYPED-REPORT", "TYPED-DESCRIPTION", "HOLDER-NAME",
                    "CUSTOM-NAME", "GAMERTAG", "STORY-PROMPT", "TRANSCRIPT", "CHEAT")


async def seed_everything(pool):
    await seed(pool, "INSERT INTO economy_users (user_id, balance, savings, jail_reason, jail_until) VALUES (%s,%s,%s,%s,%s)",
               (7, 1500, json.dumps([{"amount": 200, "deposited_at": 1.0}]), "TYPED-JAIL-REASON", 0))
    await seed(pool, "INSERT INTO economy_users (user_id, balance, savings) VALUES (%s,%s,%s)", (8, 90, "[]"))
    await seed(pool, "INSERT INTO guild_house_balance (guild_id, balance) VALUES (%s,%s)", (1, 5000))
    await seed(pool, "UPDATE slots_jackpot SET jackpot = %s WHERE id = 1", (777,))  # the migration seeds the row
    await seed(pool, "INSERT INTO lottery (guild_id, prize_pool) VALUES (%s,%s)", (1, 300))
    await seed(pool, "INSERT INTO property_owners (property_id, owner_id, acquired_at, custom_name) VALUES (%s,%s,%s,%s)",
               ("villa", 7, 1.0, "CUSTOM-NAME"))
    await seed(pool, "INSERT INTO user_artifacts (user_id, artifact_id, quantity) VALUES (%s,%s,%s)", (7, "savings", 1))
    await seed(pool, "INSERT INTO shop_effects (guild_id, user_id, effect_type, remaining, started_by, history_json, insurance_tier) VALUES (%s,%s,%s,%s,%s,%s,%s)",
               (0, 7, "insurance", 0, 7, "TRANSCRIPT", "basic"))
    await seed(pool, "INSERT INTO shop_effects (guild_id, user_id, effect_type, remaining, started_by, history_json) VALUES (%s,%s,%s,%s,%s,%s)",
               (1, 8, "ragebait", 3, 7, "TRANSCRIPT"))
    await seed(pool, "INSERT INTO leveling (guild_id, user_id, data) VALUES (%s,%s,%s)",
               (1, 7, json.dumps({"xp": 500, "level": 4, "last_xp_at": 1.0})))
    await seed(pool, "INSERT INTO leveling (guild_id, user_id, data) VALUES (%s,%s,%s)",
               (1, 8, json.dumps({"xp": 90, "level": 1})))
    await seed(pool, "INSERT INTO command_streak (user_id, last_date, streak_count) VALUES (%s,%s,%s)", (7, "2026-10-30", 9))
    await seed(pool, "INSERT INTO gambler_streak (user_id, last_full_date, streak_count) VALUES (%s,%s,%s)", (7, "2026-10-30", 2))
    await seed(pool, "INSERT INTO chess_user_stats (user_id, max_elo_defeated, total_elo_defeated, bonus_bins, elo_spent) VALUES (%s,%s,%s,%s,%s)",
               (7, 1800, 5400, "[]", 0))
    await seed(pool, "INSERT INTO mc_players (user_id, gamertag, blocks, linked_at) VALUES (%s,%s,%s,%s)", (7, "GAMERTAG", 120, 1))
    await seed(pool, "INSERT INTO records (guild_id, category, value, holder_id, holder_name, extra_json) VALUES (%s,%s,%s,%s,%s,%s)",
               (1, "slots", 9000, 7, "HOLDER-NAME", json.dumps({"bet": 10})))
    await seed(pool, "INSERT INTO records (guild_id, category, value, holder_id, holder_name, extra_json) VALUES (%s,%s,%s,%s,%s,%s)",
               (1, "crime", 400, 8, "HOLDER-NAME", None))
    await seed(pool, "INSERT INTO bot_stats_history (snapshot_date, bucket, messages, commands, ai_responses, ai_up, memory_mb, ping_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
               ("2026-10-30", 2, 100, 20, 3, 1, 150.5, 40.0))
    await seed(pool, "INSERT INTO bot_command_usage_history (snapshot_date, bucket, cog_name, count) VALUES (%s,%s,%s,%s)",
               ("2026-10-30", 2, "EconomyCog", 12))
    await seed(pool, "INSERT INTO balance_history (snapshot_date, bucket, user_id, wallet, savings) VALUES (%s,%s,%s,%s,%s)",
               ("2026-10-30", 2, 7, 1400, 200))
    await seed(pool, "INSERT INTO crime_history (snapshot_date, bucket, guild_id, user_id, gained, lost) VALUES (%s,%s,%s,%s,%s,%s)",
               ("2026-10-30", 2, 1, 7, 50, 0))
    await seed(pool, "INSERT INTO gambling_history (snapshot_date, bucket, guild_id, user_id, gained, lost) VALUES (%s,%s,%s,%s,%s,%s)",
               ("2026-10-30", 2, 1, 7, 0, 80))
    await seed(pool, "INSERT INTO levelup_history (snapshot_date, bucket, guild_id, user_id, count) VALUES (%s,%s,%s,%s,%s)",
               ("2026-10-30", 2, 1, 7, 1))
    await seed(pool, "INSERT INTO guild_settings (guild_id, settings_json) VALUES (%s,%s)",
               (1, json.dumps({"features": {"economy": True}, "ai_channels": [5], "story_aliases": {"x": "STORY-PROMPT"},
                               "nsfw_enabled": True, "ask_model": "m"})))
    await seed(pool, "INSERT INTO counters (guild_id, name, description, user_required, kind, created_by, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
               (1, "afk", "TYPED-DESCRIPTION", 1, "number", 7, 1))
    await seed(pool, "INSERT INTO counter_values (guild_id, name, user_id, value) VALUES (%s,%s,%s,%s)", (1, "afk", 7, 3))
    await seed(pool, "INSERT INTO counter_values (guild_id, name, user_id, value) VALUES (%s,%s,%s,%s)", (1, "afk", 8, 2))
    await seed(pool, "INSERT INTO command_perms (command_name, tier, hidden) VALUES (%s,%s,%s)", ("godmode", "global_admin", 1))
    await seed(pool, "INSERT INTO idle_characters (guild_id, user_id, class_name, level, last_seen, created_at, items_json, loot_json, titles_json, gold) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
               (1, 7, "Rogue", 12, 1, 1, json.dumps({"ring": 5}), "[]", "[]", 40))
    await seed(pool, "INSERT INTO idle_quests (guild_id, members_json, description, ends_at, not_before, kind, stage) VALUES (%s,%s,%s,%s,%s,%s,%s)",
               (1, json.dumps([7]), "A quest", 99, 0, "journey", 1))
    await seed(pool, "INSERT INTO idle_guild_events (guild_id, kind, detail, cast_by, stage, starts_at, ends_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
               (1, "blessing", "", 7, 1, 1, 2))
    await seed(pool, "INSERT INTO chess_reports (guild_id, channel_id, white_id, black_id, winner_id, result, pgn, final_fen, finished_at, elo, flag_user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
               (1, 9, 7, 0, 7, "1-0", "1. e4", "fen", 5, 1200, 7))
    await seed(pool, "INSERT INTO mc_daily_player_stats (stat_date, max_concurrent, total_joins, player_seconds) VALUES (%s,%s,%s,%s)", ("2026-10-30", 3, 5, 600))
    await seed(pool, "INSERT INTO mc_daily_ping_stats (stat_date, avg_ping, min_ping, max_ping) VALUES (%s,%s,%s,%s)", ("2026-10-30", 20.0, 10.0, 30.0))
    await seed(pool, "INSERT INTO mc_server_versions (ts, version) VALUES (%s,%s)", (1, "1.21.0"))
    await seed(pool, "INSERT INTO issues (guild_id, channel_id, message_id, reporter_id, report, status, created_at, kind, mute_key) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
               (1, 2, 3, 7, "TYPED-REPORT", "open", 1, "bug", "cmd:Err:msg"))
    await seed(pool, "INSERT INTO feature_requests (guild_id, channel_id, message_id, reporter_id, description, status, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
               (1, 2, 4, 7, "TYPED-DESCRIPTION", "open", 1))
    await seed(pool, "INSERT INTO bounties (guild_id, channel_id, message_id, author_id, amount, condition, status, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
               (1, 2, 5, 7, 100, "TYPED-CONDITION", "open", 1))
    await seed(pool, "INSERT INTO rigged_slots (user_id, symbol) VALUES (%s,%s)", (7, "CHEAT"))
    await seed(pool, "INSERT INTO ai_threads (thread_id, kind, owner_id, invited_ids_json, history_json) VALUES (%s,%s,%s,%s,%s)", (1, "story", 7, "[]", "TRANSCRIPT"))


SAMPLE_ARGS = {
    "bot_status": {}, "live_activity": {}, "guilds": {}, "economy_summary": {},
    "user_economy": {"user_id": 7},
    "leaderboard": {"metric": "balance"},
    "records": {"guild_id": 1},
    "history": {"series": "bot_stats"},
    "guild_settings": {"guild_id": 1},
    "active_effects": {},
    "counters": {"guild_id": 1},
    "command_permissions": {}, "migrations": {},
    "idle_overview": {"guild_id": 1},
    "idle_character": {"guild_id": 1, "user_id": 7},
    "chess_stats": {"user_id": 7},
    "minecraft_status": {},
    "issues": {}, "feature_requests": {},
}


def test_every_tool_has_sample_args():
    assert set(SAMPLE_ARGS) == {fn.__name__ for fn in tools.TOOLS}


def _keys(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k).lower())
            _keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, out)
    return out


HIDDEN_COLUMNS = {c for t in catalog.readable().values() for c in t.hidden}


@pytest.mark.parametrize("name", sorted(SAMPLE_ARGS))
async def test_tool_output_withholds_everything_sensitive(pool, bot_state, name):
    await seed_everything(pool)
    fn = next(f for f in tools.TOOLS if f.__name__ == name)
    result = redact.scrub(await fn(**SAMPLE_ARGS[name]))
    text = json.dumps(result, default=str)
    for value in SENSITIVE_VALUES:
        assert value not in text, f"{name} leaked {value}"
    keys = _keys(result, set())
    assert not (keys & redact.DENIED_KEYS), f"{name} returned denied keys {keys & redact.DENIED_KEYS}"
    assert not (keys & HIDDEN_COLUMNS), f"{name} returned hidden columns {keys & HIDDEN_COLUMNS}"


async def test_bot_status_combines_bridge_and_schema(pool, bot_state):
    out = await tools.bot_status()
    assert out["overview"]["uptime_seconds"] == 120
    assert out["health"]["status"] == "healthy"
    assert out["schema"]["version"] >= 1


async def test_bridge_down_is_reported_not_raised(pool, bot_state):
    bot_state.clear()
    out = await tools.live_activity()
    assert "unavailable" in out
    g = await tools.guilds()
    assert g["bot_reachable"] is False


async def test_economy_summary_sums(pool, bot_state):
    await seed_everything(pool)
    out = await tools.economy_summary()
    assert out["users"] == 2
    assert out["wallet_total"] == 1590
    assert out["savings_deposited_total"] == 200
    assert out["slots_jackpot"] == 777
    assert out["insurance_policies"] == 1
    assert out["properties"] == {"owned": 1, "listed": 0}


async def test_user_economy_projects_savings_and_levels(pool, bot_state):
    await seed_everything(pool)
    out = await tools.user_economy(7)
    assert out["economy"]["balance"] == 1500
    assert out["economy"]["savings_deposited"] == 200
    assert out["economy"]["in_jail"] is False
    assert out["levels"] == [{"guild_id": 1, "xp": 500, "level": 4}]
    assert out["insurance"]["insurance_tier"] == "basic"
    assert out["minecraft_blocks"] == {"blocks": 120, "linked_at": 1}
    assert (await tools.user_economy(999))["economy"] is None


async def test_leaderboards(pool, bot_state):
    await seed_everything(pool)
    assert [r["user_id"] for r in (await tools.leaderboard("balance"))["rows"]] == [7, 8]
    level = await tools.leaderboard("level", guild_id=1)
    assert level["rows"][0] == {"user_id": 7, "xp": 500, "level": 4}
    assert (await tools.leaderboard("properties"))["rows"] == [{"owner_id": 7, "properties": 1}]
    assert (await tools.leaderboard("idle", guild_id=1))["rows"][0]["class_name"] == "Rogue"
    with pytest.raises(tools.ToolInputError):
        await tools.leaderboard("level")


async def test_records_prefix_filter(pool, bot_state):
    await seed_everything(pool)
    out = await tools.records(1, "sl")
    assert [r["category"] for r in out["rows"]] == ["slots"]
    assert out["rows"][0]["extra"] == {"bet": 10}
    with pytest.raises(tools.ToolInputError):
        await tools.records(1, "sl%' OR 1=1")


async def test_history_series(pool, bot_state):
    await seed_everything(pool)
    assert (await tools.history("bot_stats"))["rows"][0]["messages"] == 100
    assert (await tools.history("commands", since="2026-10-01"))["rows"][0]["cog_name"] == "EconomyCog"
    assert (await tools.history("balance", user_id=7))["rows"][0]["wallet"] == 1400
    assert (await tools.history("crime", guild_id=1, user_id=7))["rows"][0]["gained"] == 50
    assert (await tools.history("gambling", until="2026-10-01"))["rows"] == []
    assert (await tools.history("levelups"))["rows"][0]["count"] == 1
    with pytest.raises(tools.ToolInputError):
        await tools.history("balance")
    with pytest.raises(tools.ToolInputError):
        await tools.history("bot_stats", since="yesterday")


async def test_guild_settings_projection(pool, bot_state):
    await seed_everything(pool)
    out = await tools.guild_settings(1)
    assert out["settings"] == {"features": {"economy": True}, "ai_channels": [5], "ask_model": "m"}
    assert out["omitted_keys"] == ["nsfw_enabled", "story_aliases"]
    assert (await tools.guild_settings(2))["settings"] is None


async def test_counters_total(pool, bot_state):
    await seed_everything(pool)
    out = await tools.counters(1)
    assert out["rows"][0]["name"] == "afk" and out["rows"][0]["total"] == 5


async def test_idle_character_parses_json(pool, bot_state):
    await seed_everything(pool)
    out = await tools.idle_character(1, 7)
    assert out["character"]["items"] == {"ring": 5}
    assert "items_json" not in out["character"]
    assert (await tools.idle_character(1, 8))["character"] is None


async def test_issues_filter_and_validation(pool, bot_state):
    await seed_everything(pool)
    assert (await tools.issues(status="open", kind="bug"))["count"] == 1
    assert (await tools.issues(status="closed"))["count"] == 0
    with pytest.raises(tools.ToolInputError):
        await tools.issues(status="open' OR 1=1")


async def test_minecraft_days_validation(pool, bot_state):
    with pytest.raises(tools.ToolInputError):
        await tools.minecraft_status(days=0)


async def test_tools_over_mcp_protocol(pool, bot_state):
    """The in-memory client sees every tool read-only-annotated, and a call
    returns the same structured content the function does."""
    await seed_everything(pool)
    mcp = server.build_mcp()
    async with Client(mcp) as client:
        listed = await client.list_tools()
        names = {t.name for t in listed.tools}
        assert names == set(SAMPLE_ARGS)
        for t in listed.tools:
            assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False
            assert t.description
        result = await client.call_tool("user_economy", {"user_id": 7})
        assert result.is_error is False
        assert result.structured_content["economy"]["balance"] == 1500
        assert "TYPED-JAIL-REASON" not in json.dumps(result.model_dump(), default=str)
        bad = await client.call_tool("history", {"series": "balance"})
        assert bad.is_error is True
        resources = await client.list_resources()
        assert any(str(r.uri) == "schema://tables" for r in resources.resources)
        schema = await client.read_resource("schema://tables")
        body = json.loads(schema.contents[0].text)
        assert "ai_threads" in body["never"] and "jail_reason" in body["hidden_columns"]["economy_users"]
