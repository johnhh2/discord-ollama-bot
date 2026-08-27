from src.db import with_transaction


async def save_property_owner(property_id: str, row: dict):
    """Upsert one property's ownership row from its state mirror
    (state.property_owners[property_id]). Takes the whole row so call sites
    can't silently drop a column (listing, upgrade, custom name)."""
    list_price = row.get("list_price")
    listed_at = row.get("listed_at")
    custom_name = row.get("custom_name")
    async with with_transaction() as cur:
        await cur.execute(
            "INSERT INTO property_owners"
            " (property_id, owner_id, acquired_at, list_price, listed_at, upgraded, custom_name)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE owner_id=VALUES(owner_id), acquired_at=VALUES(acquired_at),"
            " list_price=VALUES(list_price), listed_at=VALUES(listed_at),"
            " upgraded=VALUES(upgraded), custom_name=VALUES(custom_name)",
            (property_id, int(row["owner_id"]), float(row["acquired_at"]),
             int(list_price) if list_price is not None else None,
             float(listed_at) if listed_at is not None else None,
             bool(row.get("upgraded", False)),
             custom_name if custom_name else None),
        )


async def delete_property_owner(property_id: str):
    """Remove a property's ownership row (it reverts to bank-owned)."""
    async with with_transaction() as cur:
        await cur.execute(
            "DELETE FROM property_owners WHERE property_id=%s", (property_id,)
        )
