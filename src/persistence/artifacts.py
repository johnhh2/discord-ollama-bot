from src.db import with_transaction


async def save_user_artifact(uid: int, artifact_id: str, quantity: int,
                             acquired_at: float | None = None):
    """Upsert one (user, artifact) row. `acquired_at` is written on the first
    insert only — a later quantity change never moves the acquisition time,
    which the savings-rate boost reads as its rate-switch boundary."""
    async with with_transaction() as cur:
        await cur.execute(
            "INSERT INTO user_artifacts (user_id, artifact_id, quantity, acquired_at)"
            " VALUES (%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE quantity=VALUES(quantity)",
            (int(uid), artifact_id, int(quantity),
             float(acquired_at) if acquired_at is not None else None),
        )
