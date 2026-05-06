import json

from src import state
from src.db import with_cursor


async def save_ai_threads():
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM ai_threads")
        for tid, t in state.ai_threads.items():
            await cur.execute(
                "INSERT INTO ai_threads "
                "(thread_id, kind, owner_id, guild_id, invited_ids_json, system_prompt, character_prompt, history_json) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    int(tid),
                    t["kind"],
                    int(t["owner_id"]),
                    int(t["guild_id"]) if t.get("guild_id") is not None else None,
                    json.dumps(list(t.get("invited_ids", set()))),
                    t.get("system_prompt"),
                    t.get("character_prompt"),
                    json.dumps(list(t.get("history", []))),
                ),
            )


async def save_channel_prompts(prompts: dict):
    state.channel_prompts = prompts
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM channel_prompts")
        for ch_id, prompt in prompts.items():
            await cur.execute(
                "INSERT INTO channel_prompts (channel_id, prompt_text) VALUES (%s,%s)",
                (int(ch_id), prompt),
            )
