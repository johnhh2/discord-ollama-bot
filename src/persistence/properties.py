from src.db import with_transaction


async def save_property_owner(property_id: str, row: dict):
    """Upsert one property's ownership row from its state mirror
    (state.property_owners[property_id]). Takes the whole row so call sites
    can't silently drop a column (listing, upgrades, custom name)."""
    list_price = row.get("list_price")
    listed_at = row.get("listed_at")
    custom_name = row.get("custom_name")
    async with with_transaction() as cur:
        await cur.execute(
            "INSERT INTO property_owners"
            " (property_id, owner_id, acquired_at, list_price, listed_at, upgraded, secured, custom_name,"
            " damaged_at, repair_cost, repair_cover_pct, repair_cover_cap)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE owner_id=VALUES(owner_id), acquired_at=VALUES(acquired_at),"
            " list_price=VALUES(list_price), listed_at=VALUES(listed_at),"
            " upgraded=VALUES(upgraded), secured=VALUES(secured), custom_name=VALUES(custom_name),"
            " damaged_at=VALUES(damaged_at), repair_cost=VALUES(repair_cost),"
            " repair_cover_pct=VALUES(repair_cover_pct), repair_cover_cap=VALUES(repair_cover_cap)",
            (property_id, int(row["owner_id"]), float(row["acquired_at"]),
             int(list_price) if list_price is not None else None,
             float(listed_at) if listed_at is not None else None,
             bool(row.get("upgraded", False)),
             bool(row.get("secured", False)),
             custom_name if custom_name else None,
             float(row["damaged_at"]) if row.get("damaged_at") is not None else None,
             int(row.get("repair_cost", 0) or 0),
             int(row.get("repair_cover_pct", 0) or 0),
             int(row.get("repair_cover_cap", 0) or 0)),
        )


async def delete_property_owner(property_id: str):
    """Remove a property's ownership row (it reverts to bank-owned)."""
    async with with_transaction() as cur:
        await cur.execute(
            "DELETE FROM property_owners WHERE property_id=%s", (property_id,)
        )


async def save_property_release(property_id: str, released_at: float):
    """Record a pool deed's release. INSERT IGNORE: a release is permanent,
    and a re-run of the pass must not move its timestamp."""
    async with with_transaction() as cur:
        await cur.execute(
            "INSERT IGNORE INTO property_releases (property_id, released_at) VALUES (%s,%s)",
            (property_id, float(released_at)),
        )


async def save_property_supply_meta():
    """Persist the release pass's gameplay-day marker and the per-player cap
    high-water mark (economy_meta)."""
    from src import state
    async with with_transaction() as cur:
        for key in ("last_property_release", "property_cap"):
            value = state.economy.get(key)
            await cur.execute(
                "INSERT INTO economy_meta (key_name, value_text) VALUES (%s, %s)"
                " ON DUPLICATE KEY UPDATE value_text=VALUES(value_text)",
                (key, None if value is None else str(value)),
            )
