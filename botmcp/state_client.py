"""Reads the bot's /state/* views over its loopback health server.

The sidecar runs in the bot container's network namespace, so 127.0.0.1
here is the bot. The bot being down is an answer, not an error: tools
return `{"unavailable": …}` and the DB-backed parts still work.
"""
from __future__ import annotations

import os

import httpx2

TIMEOUT_SECS = 3.0


def base_url() -> str:
    return (os.environ.get("BOT_STATE_URL") or "http://127.0.0.1:9090").rstrip("/")


async def fetch_json(path: str) -> dict:
    async with httpx2.AsyncClient(base_url=base_url(), timeout=TIMEOUT_SECS) as client:
        resp = await client.get(path)
        resp.raise_for_status()
        return resp.json()


async def view(name: str) -> dict:
    try:
        return await fetch_json(f"/state/{name}")
    except Exception as e:  # the bot is down or restarting
        return {"unavailable": f"bot state not reachable: {type(e).__name__}"}


async def health() -> dict:
    try:
        async with httpx2.AsyncClient(base_url=base_url(), timeout=TIMEOUT_SECS) as client:
            resp = await client.get("/healthz")
            body = resp.json()
            body["http_status"] = resp.status_code
            return body
    except Exception as e:
        return {"unavailable": f"bot health not reachable: {type(e).__name__}"}
