"""The HTTP boundary: token on every request, Host and Origin allowlists,
nothing on the health route."""
import json
from contextlib import asynccontextmanager

import httpx2
import pytest

from botmcp import server

TOKEN = "t" * 40
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _settings(**over):
    base = dict(token=TOKEN, allowed_hosts=("192.168.1.10:8765", "127.0.0.1:8765", "localhost:8765"), allowed_origins=())
    base.update(over)
    return server.Settings(**base)


@asynccontextmanager
async def _client():
    # Entered inside each test, not a fixture: the SDK's session manager is an
    # anyio cancel scope and must open and close in the same task.
    app = server.build_app(_settings())
    async with app.app.router.lifespan_context(app.app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://192.168.1.10:8765") as c:
            yield c


async def test_health_needs_no_token():
    async with _client() as client:
        r = await client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


async def test_no_token_is_401_without_oauth_hint():
    async with _client() as client:
        r = await client.post("/mcp", headers=MCP_HEADERS, content=json.dumps(INIT))
    assert r.status_code == 401
    assert "www-authenticate" not in {k.lower() for k in r.headers}
    assert r.json() == {"error": "unauthorized"}


@pytest.mark.parametrize("header", ["Bearer " + "x" * 40, "Bearer " + TOKEN[:-1], "Basic " + TOKEN, TOKEN])
async def test_wrong_token_is_401(header):
    async with _client() as client:
        r = await client.post("/mcp", headers={**MCP_HEADERS, "Authorization": header}, content=json.dumps(INIT))
    assert r.status_code == 401


async def test_right_token_reaches_mcp():
    async with _client() as client:
        r = await client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}, content=json.dumps(INIT))
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == server.SERVER_NAME


async def test_unlisted_host_is_421_even_with_token():
    async with _client() as client:
        r = await client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}", "Host": "evil.example:8765"},
                          content=json.dumps(INIT))
    assert r.status_code == 421


async def test_browser_origin_is_403_even_with_token():
    async with _client() as client:
        r = await client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}", "Origin": "http://evil.example"},
                          content=json.dumps(INIT))
    assert r.status_code == 403


def test_settings_refuse_short_token():
    with pytest.raises(server.ConfigError):
        server.Settings.from_env({"MCP_BEARER_TOKEN": "short", "MCP_ALLOWED_HOSTS": "a:1"})
    s = server.Settings.from_env({"MCP_BEARER_TOKEN": TOKEN, "MCP_ALLOWED_HOSTS": "192.168.1.10:8765, bothost:8765", "MCP_PORT": "8765"})
    assert s.allowed_hosts == ("192.168.1.10:8765", "bothost:8765", "127.0.0.1:8765", "localhost:8765")
    assert s.port == 8765 and s.allowed_origins == ()


def test_settings_derive_allowed_hosts_from_bind_ip_and_port():
    s = server.Settings.from_env({"MCP_BEARER_TOKEN": TOKEN, "MCP_BIND_IP": "192.168.1.10", "MCP_PORT": "9000"})
    assert s.allowed_hosts == ("192.168.1.10:9000", "127.0.0.1:9000", "localhost:9000")
    # A bare name gets the port; an explicit list wins over MCP_BIND_IP.
    s = server.Settings.from_env({"MCP_BEARER_TOKEN": TOKEN, "MCP_BIND_IP": "192.168.1.10", "MCP_ALLOWED_HOSTS": "bothost.lan, other:81"})
    assert s.allowed_hosts == ("bothost.lan:8765", "other:81", "127.0.0.1:8765", "localhost:8765")
    # Neither set: loopback only, which is where compose publishes the port by default.
    assert server.Settings.from_env({"MCP_BEARER_TOKEN": TOKEN}).allowed_hosts == ("127.0.0.1:8765", "localhost:8765")
