# botmcp — read-only MCP sidecar

A separate container that lets AI agents on your LAN read the bot's
database and live state over the [Model Context Protocol](https://modelcontextprotocol.io).
Nothing in it can write: the database account is SELECT-only on an explicit
list of columns, every tool is a fixed query, and the bot's in-memory state
arrives through read-only JSON views. Everything users typed (chat, AI
transcripts, bug reports, bounty conditions), display names and every
credential are withheld by design.

## What it exposes

Nineteen tools, all read-only: `bot_status`, `live_activity`, `guilds`,
`economy_summary`, `user_economy`, `leaderboard`, `records`, `history`,
`guild_settings`, `active_effects`, `counters`, `command_permissions`,
`migrations`, `idle_overview`, `idle_character`, `chess_stats`,
`minecraft_status`, `issues`, `feature_requests`. The resource
`schema://tables` lists every table it can read, column by column, and every
table it cannot, with the reason. `botmcp/catalog.py` is that list.

## Setup

1. **Decide the account.** Pick a name and a long random password for the
   SELECT-only MariaDB account; they go into `MCP_DB_USER` and
   `MCP_DB_PASSWORD` below. The grant script is generated from those same
   variables, so nothing is edited by hand (step 4 runs it).

2. **Generate a token** (32+ characters; the sidecar refuses to start with less):

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

3. **Set the environment** on the stack (Portainer env, or `.env` locally):

   | Variable | Value |
   |---|---|
   | `COMPOSE_PROFILES` | `mcp` — the sidecar is off without it |
   | `MCP_BEARER_TOKEN` | the token from step 2 |
   | `MCP_BIND_IP` | the host's LAN address; the port is published on that interface only, and it is the `Host` clients must use |
   | `MCP_DB_USER` / `MCP_DB_PASSWORD` | the account from step 1 (`DB_HOST`, `DB_PORT`, `DB_NAME` are shared with the bot) |
   | `MCP_DB_ACCOUNT_HOST` | optional, default `%`: the host part of the account, e.g. `172.18.0.%` |
   | `MCP_PORT` | optional, default `8765` |
   | `MCP_ALLOWED_HOSTS` | optional: replaces the default `Host` allowlist with a comma-separated list, e.g. `192.168.1.10,bothost.lan` (a name without a port gets `MCP_PORT`) |

4. **Create the account and deploy.** With the env in place, generate the
   grants from it and run them as the MariaDB admin, then start the stack.
   The sidecar shares the bot container's network namespace, so it is
   listed under the bot's `ports:`.

   ```bash
   docker compose --profile mcp build mcp
   docker compose --profile mcp run --rm mcp python -m botmcp.grants | mysql -h "$DB_HOST" -u root -p
   docker compose --profile mcp up -d
   ```

   `MCP_DB_ACCOUNT_HOST` (optional) narrows the account from `'%'` to, say,
   the Docker subnet. The committed [scripts/mcp_db_user.sql](../scripts/mcp_db_user.sql)
   is the same output with placeholders, kept so grant changes show up in
   review; re-run the command above whenever it changes.

5. **Verify from another machine on the LAN:**

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' http://192.168.1.10:8765/mcp \
     -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{}'
   # 401 — no token
   curl -s -o /dev/null -w '%{http_code}\n' http://192.168.1.10:8765/mcp -H 'Host: evil.example' \
     -X POST -H "Authorization: Bearer $MCP_BEARER_TOKEN" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{}'
   # 421 — Host not allowlisted
   ```

6. **Connect a client.** Claude Code, for one:

   ```bash
   claude mcp add --transport http --scope user bot http://192.168.1.10:8765/mcp \
     --header "Authorization: Bearer $MCP_BEARER_TOKEN"
   ```

   Or in `.mcp.json`, keeping the token in the environment:

   ```json
   {"mcpServers": {"bot": {"type": "http", "url": "http://192.168.1.10:8765/mcp",
                           "headers": {"Authorization": "Bearer ${BOT_MCP_TOKEN}"}}}}
   ```

## Security model

- **Two credentials, both minimal.** The SELECT-only account and the bearer
  token. The Discord token, the bot's DB password and every other secret
  stay in the bot container.
- **Token on every request**, compared in constant time; a 401 carries no
  OAuth discovery hint. The server is stateless, so there is no session to
  hijack. The token crosses the LAN in cleartext — fine on a switched home
  network; put an SSH port-forward or a TLS proxy in front if that changes.
- **Host and Origin allowlists** (the SDK's DNS-rebinding protection): a
  browser on the LAN that is tricked into calling the server gets 403, and
  a request addressed to any other name gets 421.
- **The database is the enforcement boundary.** Column-level grants, a
  read-only session, a 5-second statement limit, a connection cap. The
  query builder, the catalog and the output scrubber are defence in depth.
- **Every tool call is logged** as one JSON line (tool, arguments, outcome,
  duration) to the container's stdout.
- **Returned data is still untrusted input to the agent.** Nothing here
  carries user-written text, which is the usual prompt-injection carrier,
  but treat every value as data, not instructions.

## Adding a table or a tool

See CLAUDE.md: *Read-only MCP sidecar*. In short: classify every new table
and column in `catalog.py`, regenerate the committed SQL with
`python -m botmcp.grants --template > scripts/mcp_db_user.sql`, re-run the
grants on the database (step 4 above), and write tools in `tools.py` through `query.select` only.

## Running the tests

```bash
python -m venv .venv-mcp && .venv-mcp/bin/pip install -r botmcp/requirements.txt pytest pytest-asyncio
.venv-mcp/bin/pytest -q botmcp/tests
```

Run from the repository root: the tests build the schema from the real
migrations through `tests/fakes/db.py`.

## Local use without Docker

`python -m botmcp --stdio` runs the same tools over stdio for a client on
the same machine (no token, no Host check — the launching process is the
boundary). It still needs `DB_HOST`, `DB_PORT`, `DB_NAME`, `MCP_DB_USER`,
`MCP_DB_PASSWORD`, and `BOT_STATE_URL` pointing at the bot's health server
if the live-state tools should answer.
