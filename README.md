# discord-ollama-bot

A Discord bot that uses a locally hosted [Ollama](https://ollama.com) server to respond to messages as a configurable character or persona.

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com) installed and running locally
- A Discord bot token

## Setup

### 1. Clone the repo

```powershell
git clone https://github.com/johnhh2/discord-ollama-bot.git
cd discord-ollama-bot
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Create a Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application**, give it a name
3. Go to **Bot** in the left sidebar
4. Click **Reset Token** and copy the token
5. Under **Privileged Gateway Intents**, enable **Message Content Intent**

### 4. Invite the bot to your server

1. Go to **OAuth2 → URL Generator**
2. Select scope: `bot`
3. Select permissions: `Read Messages/View Channels`, `Send Messages`, `Read Message History`
4. Open the generated URL and invite the bot to your server

### 5. Configure the bot

Copy `.env.example` to `.env` and fill in your values:

```powershell
cp .env.example .env
```

```env
DISCORD_TOKEN=your_discord_bot_token_here

OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2

# Optional: comma-separated channel IDs to restrict the bot to specific channels
# Leave empty to allow responses everywhere
ACTIVE_CHANNEL_IDS=

# Define your bot's character or goal here
SYSTEM_PROMPT=You are a helpful assistant.
```

Run `ollama list` to see which models you have available.

### 6. Run the bot

```powershell
python bot.py
```

---

## Docker setup (Docker Desktop)

This is the recommended way to run the bot persistently without keeping a terminal open.

### 1. Make sure your `.env` is configured

Same as above. One important difference — since the bot runs inside a container, it can't reach `localhost` on your machine directly. Change `OLLAMA_BASE_URL` in your `.env` to:

```env
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

`host.docker.internal` is a special hostname Docker Desktop provides on Windows to reach the host machine.

### 2. Build and start the container

Open Docker Desktop, then run:

```powershell
docker compose up -d
```

- `-d` runs it in the background (detached)
- Docker Desktop will show the container under the **Containers** tab

### 3. View logs

```powershell
docker compose logs -f
```

Or click the container in Docker Desktop and open the **Logs** tab.

### 4. Stop the bot

```powershell
docker compose down
```

### 5. Rebuild after code changes

```powershell
docker compose up -d --build
```

---

## Usage

- **Mention the bot** in any channel it can see: `@BotName hello!`
- **DM the bot** directly — no mention needed

The bot will query your local Ollama server and reply in the same channel.

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Your Discord bot token |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | URL of your Ollama server |
| `OLLAMA_MODEL` | `llama3.2` | Model to use (must be pulled in Ollama) |
| `ACTIVE_CHANNEL_IDS` | *(empty)* | Comma-separated channel IDs to restrict responses; empty = everywhere |
| `SYSTEM_PROMPT` | `You are a helpful assistant.` | Character definition or goal for the bot |

## Example characters

```env
SYSTEM_PROMPT=You are a grumpy pirate named Barnacle Pete. You speak in pirate slang and are easily annoyed.
```

```env
SYSTEM_PROMPT=You are a helpful server moderator. Keep responses concise and friendly.
```
