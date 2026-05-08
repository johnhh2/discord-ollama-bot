"""Tiny localhost-only HTTP server exposing /healthz and /metrics.

Binds to 127.0.0.1:9090 inside the container — never reachable from the host or
LAN. The /healthz endpoint reports three dependencies:

  - discord  (gateway): hard-fail if down  -> overall 503
  - db       (mariadb): hard-fail if down  -> overall 503
  - ollama   (ai):      soft-fail if down  -> overall 200, status="degraded"

Soft-failing on Ollama matches reality: the bot has dozens of non-AI commands
that work fine without it, and restarting the bot doesn't fix Ollama anyway.

/metrics emits Prometheus text-format. Keeping it on the same loopback port
(rather than exposing 9090 to the host) means a Prometheus scrape needs to
either run inside the same container/network namespace or hit it via an
exec-based exporter — a deliberate choice over leaking process internals.
"""
import asyncio
import logging
import math

from aiohttp import web

from src import ai, metrics
from src.db import get_pool

HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 9090
DB_TIMEOUT_SECS = 2.0
DISCORD_LATENCY_CEILING_SECS = 30.0


async def _check_discord(bot) -> tuple[str, str]:
    """('ok', '') | ('down', reason). Considers the gateway dead if the bot
    isn't ready or its heartbeat latency is non-finite/extreme."""
    if bot is None or not bot.is_ready():
        return "down", "not ready"
    latency = bot.latency
    if not math.isfinite(latency):
        return "down", "no heartbeat"
    if latency > DISCORD_LATENCY_CEILING_SECS:
        return "down", f"latency {latency:.1f}s"
    return "ok", ""


async def _check_db() -> tuple[str, str]:
    async def _probe():
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()
    try:
        await asyncio.wait_for(_probe(), timeout=DB_TIMEOUT_SECS)
        return "ok", ""
    except asyncio.TimeoutError:
        return "down", "timeout"
    except Exception as e:
        return "down", type(e).__name__


async def _check_ollama() -> tuple[str, str]:
    if await ai.check_ollama_connected():
        return "ok", ""
    return "down", "unreachable"


def _render(state: tuple[str, str]) -> str:
    code, reason = state
    return code if not reason else f"{code}: {reason}"


_BOT_KEY: web.AppKey = web.AppKey("bot", object)


async def _healthz(request: web.Request) -> web.Response:
    bot = request.app[_BOT_KEY]
    discord_state, db_state, ollama_state = await asyncio.gather(
        _check_discord(bot), _check_db(), _check_ollama(),
    )

    if discord_state[0] != "ok" or db_state[0] != "ok":
        status, code = "unhealthy", 503
    elif ollama_state[0] != "ok":
        status, code = "degraded", 200
    else:
        status, code = "healthy", 200

    return web.json_response({
        "status": status,
        "discord": _render(discord_state),
        "db": _render(db_state),
        "ollama": _render(ollama_state),
    }, status=code)


async def _metrics(request: web.Request) -> web.Response:
    # CONTENT_TYPE_LATEST includes a charset parameter, which aiohttp
    # rejects on the `content_type=` kwarg — set the full header instead.
    body, content_type = metrics.render()
    return web.Response(body=body, headers={"Content-Type": content_type})


def build_app(bot) -> web.Application:
    app = web.Application()
    app[_BOT_KEY] = bot
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/metrics", _metrics)
    return app


async def start_health_server(bot, host: str = HEALTH_HOST, port: int = HEALTH_PORT) -> web.AppRunner:
    """Start the /healthz + /metrics server. Returns the runner so callers can clean up."""
    app = build_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logging.info("health server listening on http://%s:%d (/healthz, /metrics)", host, port)
    return runner
