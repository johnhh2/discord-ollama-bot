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
| `RULE34_API_KEY` / `RULE34_USER_ID` | — | Optional; enables `!rule34` command |

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

## Docker

```bash
docker build -t discord-ollama-bot .
docker run --env-file .env discord-ollama-bot
```
