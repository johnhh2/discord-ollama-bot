from src.db import with_transaction


async def save_property_owner(property_id: str, owner_id: int, acquired_at: float,
                              list_price: int = None, listed_at: float = None):
    """Upsert one property's ownership/listing row (mirror of
    state.property_owners[property_id])."""
    async with with_transaction() as cur:
        await cur.execute(
            "INSERT INTO property_owners (property_id, owner_id, acquired_at, list_price, listed_at)"
            " VALUES (%s,%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE owner_id=VALUES(owner_id), acquired_at=VALUES(acquired_at),"
            " list_price=VALUES(list_price), listed_at=VALUES(listed_at)",
            (property_id, int(owner_id), float(acquired_at),
             int(list_price) if list_price is not None else None,
             float(listed_at) if listed_at is not None else None),
        )


async def delete_property_owner(property_id: str):
    """Remove a property's ownership row (it reverts to bank-owned)."""
    async with with_transaction() as cur:
        await cur.execute(
            "DELETE FROM property_owners WHERE property_id=%s", (property_id,)
        )
