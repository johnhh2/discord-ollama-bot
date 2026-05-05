from src.db import with_cursor


async def save_command_perms():
    from src import state
    async with with_cursor() as cur:
        for cmd, data in state.command_perms.items():
            await cur.execute(
                "INSERT INTO command_perms (command_name, tier, hidden) VALUES (%s,%s,%s)"
                " ON DUPLICATE KEY UPDATE tier=VALUES(tier), hidden=VALUES(hidden)",
                (cmd, data["tier"], bool(data.get("hidden", False))),
            )
