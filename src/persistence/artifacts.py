from src.db import with_transaction


async def save_user_artifact(uid: int, artifact_id: str, quantity: int):
    async with with_transaction() as cur:
        await cur.execute(
            "INSERT INTO user_artifacts (user_id, artifact_id, quantity)"
            " VALUES (%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE quantity=VALUES(quantity)",
            (int(uid), artifact_id, int(quantity)),
        )
