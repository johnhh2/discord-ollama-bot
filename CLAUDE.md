# discord-ollama-bot

A Discord bot backed by a local Ollama LLM, with economy, gambling, games, and moderation features.

## Running the Bot

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in DISCORD_TOKEN and any optional vars
python bot.py
```

Key environment variables (all optional except `DISCORD_TOKEN`):

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_TOKEN` | — | **Required.** Your bot token |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama API endpoint |
| `OLLAMA_MODEL` | `dolphin3:8b` | Default model for `!ask` |
| `SYSTEM_PROMPT` | `You are a helpful assistant.` | Default system prompt |
| `HISTORY_LIMIT` | `20` | Per-channel message history depth |
| `RATE_LIMIT_SECONDS` | `5.0` | Per-user AI cooldown |
| `ACTIVE_CHANNEL_IDS` | _(all channels)_ | Comma-separated channel IDs for passive AI responses |
| `NSFW_API_URL` | — | Optional; base URL for the NSFW image API (enables `!nsfw`) |
| `NSFW_API_KEY` / `NSFW_API_USER_ID` | — | Optional; API credentials for the NSFW image endpoint |

### Adding a new env var

When adding a new env var that the bot reads from `os.getenv(...)`, you must update **all** of the following or it won't reach the deployed container:

1. `src/config.py` — the `os.getenv(...)` call
2. `.env.example` — placeholder + one-line comment
3. `docker-compose.yml` — add it to the `environment:` block as `MY_VAR: ${MY_VAR:-}` (the `:-` empty default avoids "variable not set" warnings in Portainer)
4. This `CLAUDE.md` env-var table (and the README's table if user-facing)

The production deploy reads env vars from the Portainer stack UI, which exports them to the shell that runs `docker compose up`. Vars listed only in `.env` will work locally but will be silently missing in production.

## Running Tests

Install dev dependencies (not in `requirements.txt`):

```bash
pip install pytest pytest-asyncio
```

Run all tests:

```bash
pytest
```

Run with verbose output:

```bash
pytest -v
```

Run a single test class or function:

```bash
pytest tests/test_bot.py::TestHandValue
pytest tests/test_bot.py::TestHandValue::test_blackjack
```

Tests do **not** require a Discord token or Ollama connection. No files are written to `data/` during the test run.

## Timezones

The bot's daily reset is **5am CT** (`DAILY_RESET_HOUR = 5` in `src/config.py`).

Always use `ZoneInfo("America/Chicago")` for CT — it handles CST/CDT automatically:

```python
from zoneinfo import ZoneInfo
import datetime

def _ct_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).astimezone(ZoneInfo("America/Chicago"))
```

Helpers already in `src/economy.py` (import from there, don't reimplement):
- `_ct_now()` — current datetime in CT
- `_ct_today()` — current "day" string in CT, where the day rolls over at 5am
- `next_daily_reset_ts()` — Unix timestamp of the next 5am CT reset (use for Discord `<t:...:R>` timestamps)

Never use `datetime.timezone.utc` with a hardcoded offset for CT — it won't respect DST.

## Command Permission System

Every command's access tier is controlled by `data/command_perms.json`. Commands not listed default to `everyone` (no restriction).

### Tiers

| Tier | Who can run it |
|------|----------------|
| `everyone` | All users |
| `server_admin` | Discord server administrators **or** bot admins |
| `bot_admin` | Bot admins only (user IDs in `data/bot_admins.json`) |

### `hidden` flag

When `hidden: true`, a denied command silently does nothing instead of sending `❌ No Permission`. Use this for sensitive bot-admin commands that should be invisible to regular users (e.g. `godmode`, `restart`, `admingive`).

### Example entry

```json
"mycommand": {"tier": "bot_admin", "hidden": false}
```

### Rules — follow these whenever touching commands

1. **New command** — add an entry to `data/command_perms.json`. If you don't add one, it defaults to `everyone`.
2. **Renamed command** — update the key in the JSON to match the new `name=` in `@commands.command(...)`. Aliases do **not** need their own entries; the check uses `ctx.command.name` (the canonical name).
3. **Changing a permission** — edit `src/command_perms.json` directly and commit the change. `!setperm` can also update it at runtime, but the file is the source of truth — always commit any permanent changes.
4. **Each command body** must start with:
   ```python
   if not await check_command_permission(ctx):
       return
   ```
   Import `check_command_permission` from `src.permissions`. Do **not** use bare `is_admin` / `can_manage_settings` guards for new commands — use the central check so the JSON stays authoritative.

### Relevant files

- `src/command_perms.json` — the permission config (committed to the repo, not in `data/`)
- `src/permissions.py` — `check_command_permission`, `get_command_perm`
- `src/persistence.py` — `load_command_perms`, `save_command_perms`
- `src/state.py` — `command_perms` dict (loaded at startup)

## Docker

```bash
docker build -t discord-ollama-bot .
docker run --env-file .env discord-ollama-bot
```
