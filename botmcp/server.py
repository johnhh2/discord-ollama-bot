"""The ASGI app: bearer auth on every request, the SDK's Host/Origin check,
and the MCP endpoint at /mcp.

Order of checks on a request: the bearer token (this module), then the
SDK's DNS-rebinding guard (Host allowlist → 421, Origin allowlist → 403),
then MCP. Sessions are never a credential — the token is checked on every
request, as the spec's session-hijacking guidance requires — and the server
is stateless, so there is no session to hijack anyway.

Why a pre-shared token and not OAuth: this server lives on one LAN with one
operator. The spec's authorization flow is a SHOULD for HTTP transports;
a 401 here carries no `WWW-Authenticate` discovery hint, so a client never
starts an OAuth dance it cannot finish.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from botmcp import db, tools

log = logging.getLogger("botmcp")

MIN_TOKEN_LEN = 32
SERVER_NAME = "discord-ollama-bot"
INSTRUCTIONS = (
    "Read-only view of a Discord bot's database and live state. Every tool is a fixed query; "
    "nothing here can change the bot, the database or Discord. Free text users typed (chat, AI "
    "transcripts, reports, bounty conditions), display names and all credentials are withheld by "
    "design — a missing field is a withheld field, not a bug. Discord ids are returned as-is. "
    "Read schema://tables for what each table exposes."
)


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    token: str
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...] = ()
    bind: str = "0.0.0.0"  # nosec B104 — inside the container's namespace; compose pins the published side to the LAN ip
    port: int = 8765
    health_paths: frozenset = field(default_factory=lambda: frozenset({"/healthz"}))

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        token = (env.get("MCP_BEARER_TOKEN") or "").strip()
        if len(token) < MIN_TOKEN_LEN:
            raise ConfigError(f"MCP_BEARER_TOKEN must be at least {MIN_TOKEN_LEN} characters "
                              "(python -c \"import secrets; print(secrets.token_urlsafe(48))\")")
        port = int(env.get("MCP_PORT") or 8765)
        # The SDK matches the whole `host:port` string, so a bare entry gets the
        # port appended. With MCP_ALLOWED_HOSTS unset, the address the port is
        # published on (MCP_BIND_IP) is the one clients will use; the loopback
        # names cover the container's healthcheck and a curl from the host.
        named = [h.strip() for h in (env.get("MCP_ALLOWED_HOSTS") or "").split(",") if h.strip()]
        bind_ip = (env.get("MCP_BIND_IP") or "").strip()
        if not named and bind_ip:
            named = [bind_ip]
        hosts = tuple(dict.fromkeys(
            [h if ":" in h else f"{h}:{port}" for h in named] + [f"127.0.0.1:{port}", f"localhost:{port}"]))
        origins = tuple(o.strip() for o in (env.get("MCP_ALLOWED_ORIGINS") or "").split(",") if o.strip())
        return cls(token=token, allowed_hosts=hosts, allowed_origins=origins,
                   bind=env.get("MCP_BIND_ADDR") or "0.0.0.0", port=port)  # nosec B104 — see `bind`


class BearerAuth:
    """401 for every request without the exact token. Constant-time compare;
    the health path is the one unauthenticated route, and it says nothing."""

    def __init__(self, app: ASGIApp, token: str, exempt: frozenset):
        self.app = app
        self._token = token.encode()
        self._exempt = exempt

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self._exempt:
            await self.app(scope, receive, send)
            return
        header = Headers(scope=scope).get("authorization", "")
        presented = header[7:].strip().encode() if header.startswith("Bearer ") else b""
        if not presented or not hmac.compare_digest(presented, self._token):
            client = scope.get("client") or ("?", 0)
            log.warning("auth_rejected client=%s path=%s", client[0], scope["path"])
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_mcp() -> MCPServer:
    mcp = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)
    tools.register(mcp)
    return mcp


async def _healthz(request: Request) -> Response:
    return Response("ok", media_type="text/plain")


def build_app(settings: Settings, mcp: "MCPServer | None" = None) -> Starlette:
    mcp = mcp or build_mcp()
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=list(settings.allowed_origins),
    )
    # Stateless: no Mcp-Session-Id to steal, any request stands alone. JSON
    # responses rather than SSE — nothing here streams.
    inner = mcp.streamable_http_app(transport_security=security, stateless_http=True, json_response=True)

    @asynccontextmanager
    async def lifespan(app):
        log.info("botmcp_started bind=%s port=%d allowed_hosts=%s tools=%d",
                 settings.bind, settings.port, ",".join(settings.allowed_hosts), len(tools.TOOLS))
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await db.close_pool()

    app = Starlette(
        routes=[Route("/healthz", _healthz), Mount("/", app=inner)],
        lifespan=lifespan,
    )
    return BearerAuth(app, settings.token, settings.health_paths)


def log_config() -> dict:
    """One JSON line per record on stdout, like the bot."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": "botmcp.server.JsonFormatter"}},
        "handlers": {"stdout": {"class": "logging.StreamHandler", "formatter": "json", "stream": "ext://sys.stdout"}},
        "root": {"level": "INFO", "handlers": ["stdout"]},
    }


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        body = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            body["exc"] = self.formatException(record.exc_info)
        return json.dumps(body, default=str)
