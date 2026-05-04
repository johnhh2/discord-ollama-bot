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
pip install pytest pytest-asyncio ruff
```

CI runs `ruff check src/ tests/` on every push/PR. The active ruleset (`E`, `F`, `W` minus `E501`) is configured in `pyproject.toml`. Run it locally before committing.

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

Every command's access tier is controlled by `src/command_perms.json` (committed to the repo, not under `data/`). Commands not listed default to `everyone` (no restriction). The JSON seeds the `command_perms` table on every boot via `INSERT IGNORE` (see `init_db_state` in `src/persistence.py`); `!setperm` mutates the DB at runtime but the JSON is the source of truth — always commit permanent changes there.

### Tiers

| Tier | Who can run it |
|------|----------------|
| `everyone` | All users |
| `server_admin` | Discord server administrators **or** bot admins |
| `bot_admin` | Bot admins only — user IDs come from the `BOT_ADMIN_IDS` env var (comma-separated), seeded into `state.bot_admins` at startup |

### `hidden` flag

When `hidden: true`, a denied command silently does nothing instead of sending `❌ No Permission`. Use this for sensitive bot-admin commands that should be invisible to regular users (e.g. `godmode`, `restart`, `admingive`).

### Example entry

```json
"mycommand": {"tier": "bot_admin", "hidden": false}
```

### Rules — follow these whenever touching commands

1. **New command** — add an entry to `src/command_perms.json`. If you don't add one, it defaults to `everyone`.
2. **Renamed command** — update the key in the JSON to match the new `name=` in `@commands.command(...)`. Aliases do **not** need their own entries; the check uses `ctx.command.qualified_name` (the canonical name, with subgroup space-prefixed for `!settings X`-style subcommands).
3. **Changing a permission** — edit `src/command_perms.json` directly and commit the change. `!setperm` can also update it at runtime, but the file is the source of truth — always commit any permanent changes.
4. **Each command body** must use the `@requires_perm` decorator (from `src.permissions`):
   ```python
   from src.permissions import requires_perm

   @commands.command(name="mycommand")
   @requires_perm
   async def cmd_mycommand(self, ctx):
       ...
   ```
   Decorator order matters: `@commands.command` outermost, `@requires_perm` directly above the def. Do **not** use bare `is_admin` / `can_manage_settings` guards for new commands.

### Relevant files

- `src/command_perms.json` — the permission config (committed to the repo, not in `data/`)
- `src/permissions.py` — `check_command_permission`, `get_command_perm`
- `src/persistence.py` — `load_command_perms`, `save_command_perms`
- `src/state.py` — `command_perms` dict (loaded at startup)

## Schema migrations

Schema changes ship as numbered SQL files in [migrations/](migrations/). The bot applies pending migrations at boot, before loading state — there is no manual `mysql < schema.sql` step in production.

### Adding a schema change — follow these steps every time

1. **Create a new file** `migrations/NNNN_short_description.sql` where `NNNN` is the next zero-padded sequential number after the current highest. No gaps, no duplicates — the runner refuses to start if either appears.
2. **Write idempotent MariaDB SQL.** Prefer `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, `ALTER TABLE … ADD COLUMN IF NOT EXISTS`. If a migration fails halfway through, idempotent SQL lets the operator retry by simply rebooting.
3. **Do NOT add a self-healing `CREATE TABLE IF NOT EXISTS` in `init_db_state`.** That pattern existed once (the `bot_command_usage_history` block) and was deliberately removed when migrations landed. New tables go in a migration file.
4. **Do NOT edit `src/schema.sql` by hand.** It's autogenerated. Run `python scripts/regenerate_schema.py` after adding a migration and commit the regenerated file alongside the migration.
5. **Do NOT edit an already-applied migration file.** The runner stores a sha256 checksum per applied migration and refuses to boot if a file's contents change. To fix a mistake in migration N, write migration N+1 that corrects it.
6. **Add the new table's PK to `tests/fakes/db.py:_TABLE_PKS`** if the table will ever be the target of `INSERT … ON DUPLICATE KEY UPDATE`. The SQLite test translator needs the PK list to rewrite that into `ON CONFLICT(...) DO UPDATE`. If you skip this and a test exercises an upsert against the new table, you'll get a clear `No PK mapping for table 'xxx'` error — but it's easier to add it up front.
7. **Run the suite**: `python -m pytest -q`. The test fake builds its in-memory DB by running these same migration files, so any MariaDB syntax SQLite can't translate fails immediately. If that happens, extend `_translate()` in `tests/fakes/db.py` rather than rewriting the migration to be SQLite-friendly — production must keep speaking real MariaDB.
8. **Do NOT touch `src/migrations.py:_done`** or the heuristic-baseline logic to "fix" a deploy issue. The heuristic only fires when `schema_migrations` is empty AND `economy_users` exists, exactly once per DB. If a deploy is wrong, fix it with a forward-fix migration; never edit history.

### Reverse migrations (optional)

Opt-in, operator-only. To make a migration revertible, drop a paired `migrations/NNNN_name.down.sql`; revert with `python -m src.migrations down N`. Never invoked automatically. Don't write no-op down files — absence is meaningful.

### How the heuristic baseline works (relevant context)

On the very first boot after the migration system landed, prod already had every table from the old `schema.sql`. The runner detects this state (`schema_migrations` empty + sentinel table `economy_users` present) and records `0001_baseline` as already-applied **without executing it**, then proceeds with later migrations normally. This is the only reason `0001_baseline.sql` is allowed to be a 200-line file — fresh DBs run it; existing DBs skip it.

If you ever clone the prod DB to dev and want a fresh migrations history, drop both `schema_migrations` and all the bot's tables, then boot — the runner will execute the baseline against the empty DB.

### Relevant files

- [migrations/](migrations/) — the source of truth. Numbered SQL files.
- [src/migrations.py](src/migrations.py) — runner. Called from `init_db_state` before any SELECT.
- [src/schema.sql](src/schema.sql) — autogenerated snapshot. Read-only for humans.
- [scripts/regenerate_schema.py](scripts/regenerate_schema.py) — rebuilds `src/schema.sql` from migrations.
- [tests/test_migrations.py](tests/test_migrations.py) — covers ordering, checksum, baseline-detection, and that real migrations apply against a fresh DB.
- [tests/fakes/db.py](tests/fakes/db.py) — the MariaDB→SQLite translator. Extend `_translate()` when migrations introduce new MariaDB syntax tests can't yet handle.

## Docker

```bash
docker build -t discord-ollama-bot .
docker run --env-file .env discord-ollama-bot
```
