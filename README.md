# discord-ollama-bot

A feature-rich Discord bot that runs against a self-hosted [Ollama](https://ollama.com) LLM. Beyond chat, it ships with a full economy, gambling games, role/channel shop, lottery, level system, and a permission framework — all backed by MariaDB.

Built as a long-running personal project to explore Discord's API, async Python, LLM streaming, and small-scale game-economy design.

## Features

**LLM-backed chat**
- `!ask` — conversational Q&A, threaded so each conversation has its own context
- `!continue` — extend the previous response
- Per-guild model selection (separate models for different command modes)
- Channel-scoped passive responses, configurable history depth, per-user rate limiting
- Streaming output with a global semaphore so a single GPU isn't overloaded

**Economy & gambling**
- Daily rewards (resets at 5am CT, DST-aware)
- `!slots` with progressive jackpot, `!flip`, `!blackjack`, `!scratch` (scratchoffs)
- Weekly `!lottery` with per-ticket purchases and a scheduled draw
- `!savings` with compounding principal/interest tracking
- `!steal` / `!mug` / `!jail` / `!jailbreak` PvP economy actions with insurance you can buy in the shop
- `!leaderboard`, `!records`, `!economy` overview, `!graph` for visualizing balance history

**Games**
- `!hangman` (with a 60k-word list and rarity-weighted payouts)
- `!ttt` (tic-tac-toe), `!c4` (connect 4), `!chess` (persistent games), `!race`
- Multi-player support with channel-scoped sessions

**Shop**
- Spend currency on real Discord effects: nicknames, role create/assign/color/move, channel create/rename/lock, mute, mock/curse/ragebait text effects, tax-other-user, insurance, UNO-reverse cards
- All costs configured centrally in [src/config.py](src/config.py)

**Levels & unlocks**
- Per-guild XP from messages
- Commands gated behind level thresholds — see [src/level_unlocks.py](src/level_unlocks.py)
- `!lvl`, `!levels` leaderboard

**Moderation & admin**
- Three-tier permission system (`everyone`, `server_admin`, `bot_admin`) with optional `hidden` flag for stealth admin commands
- All command perms in a single JSON file ([src/command_perms.json](src/command_perms.json)) — runtime-editable via `!setperm`
- Bot-admin commands: `!restart` (Docker-aware), `!godmode`, `!audit`, `!clearbot`, `!admingive`, `!adminunlock`
- Full audit log of admin actions

**Other**
- Riddles, quotes, dog/cat image commands
- Ephemeral message auto-deletion
- VRAM/uptime/memory `!stats`

## Architecture

```
src/
├── core.py            # Bot factory, extension loading, level-gate check
├── config.py          # All env vars and tunable constants
├── persistence.py     # MariaDB-backed save/load layer (~970 lines)
├── db.py              # aiomysql connection pool
├── schema.sql         # MariaDB schema
├── economy.py         # Balance, daily reset, savings, jail logic
├── ai.py              # Ollama streaming, cost enforcement, system prompts
├── permissions.py     # check_command_permission, tier resolution
├── level_unlocks.py   # Per-command level requirements
├── helpers.py         # Embed builders, font transforms, shared utilities
├── events.py          # Message dispatch, XP, ragebait/mock/curse handlers
├── state.py           # In-memory caches loaded at startup
├── cogs/              # Command groups (admin, ai, economy, shop, …)
├── games/             # Blackjack, hangman, chess, ttt/c4, race
└── gambling/          # Slots, flip, scratchoff
```

Roughly **12.4k lines of source** and **3.9k lines of tests**.

The persistence layer was [migrated from JSON files to MariaDB](src/persistence.py) mid-project — the schema lives in [src/schema.sql](src/schema.sql) and a SQLite-flavored version lives in [tests/fakes/schema_sqlite.sql](tests/fakes/schema_sqlite.sql) so the test suite can run against an in-memory DB with no external dependencies.

## Quick start (Docker)

The bot is designed to run in Docker against an Ollama instance running on the host (or another machine on the LAN).

```bash
git clone https://github.com/johnhh2/discord-ollama-bot.git
cd discord-ollama-bot
cp .env.example .env       # fill in DISCORD_TOKEN, DB_*, etc.
docker compose up -d
docker compose logs -f
```

You'll need a MariaDB instance reachable from the container. Initialize it once with:

```bash
mysql -h $DB_HOST -u $DB_USER -p $DB_NAME < src/schema.sql
```

## Running locally (without Docker)

```bash
pip install -r requirements.txt
cp .env.example .env       # fill in values
python main.py
```

## Configuration

All configuration is via environment variables. See [.env.example](.env.example) for the full list. Highlights:

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | — | **Required.** Bot token from the Discord Developer Portal |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | — | **Required.** MariaDB connection |
| `DISCORD_CLIENT_ID` | — | Application client ID; required only for `!botinvitelink` |
| `BOT_ADMIN_IDS` | — | Comma-separated Discord user IDs who get bot-admin tier |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama API endpoint |
| `OLLAMA_MODEL` | `dolphin3:8b` | Default model for `!ask` |
| `SYSTEM_PROMPT` | `You are a helpful assistant.` | Default character prompt |
| `HISTORY_LIMIT` | `20` | Per-channel history depth fed to the model |
| `RATE_LIMIT_SECONDS` | `5.0` | Per-user AI cooldown |
| `ACTIVE_CHANNEL_IDS` | _(all)_ | Comma-separated channel IDs where the bot responds passively |

## Tests

The test suite uses an in-memory SQLite database and a fake `discord` module — no Discord token, Ollama instance, or MariaDB needed.

```bash
pip install pytest pytest-asyncio
pytest
```

Test layout:
- [tests/test_bot.py](tests/test_bot.py) — game logic, hand evaluation, lottery math
- [tests/test_economy_flows.py](tests/test_economy_flows.py) — daily reset, savings, transfers
- [tests/test_persistence.py](tests/test_persistence.py) — DB round-trips
- [tests/test_shop.py](tests/test_shop.py) — shop charge/refund flows
- [tests/test_permissions.py](tests/test_permissions.py) — tier resolution and `hidden` flag
- [tests/test_downtime.py](tests/test_downtime.py) — recovery from missed scheduled events

## Deployment

The bot runs in production on a Synology NAS, deployed via:
1. GitHub Actions builds and pushes the Docker image to GHCR ([.github/workflows/docker.yml](.github/workflows/docker.yml))
2. A Portainer webhook on the NAS pulls the new image and restarts the stack

`docker-compose.yml` is configured for this setup (named volume at `/volume1/docker/ollama_discord_bot`, memory cap, log rotation).

## License

MIT — see [LICENSE](LICENSE).
