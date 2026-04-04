# Discord Ollama Bot - Setup Guide

## Prerequisites

- Discord bot token (get from [Discord Developer Portal](https://discord.com/developers/applications))
- Ollama running on your desktop/server with model loaded
- Docker installed on your target system (Synology NAS, Linux server, etc.)

---

## Quick Start (Docker)

### 1. Clone the Repository
```bash
git clone https://github.com/yourusername/discord-ollama-bot.git
cd discord-ollama-bot
```

### 2. Configure Environment
```bash
# Copy the template
cp .env.example .env

# Edit .env with your Discord token
nano .env
# OR on Windows:
# notepad .env
```

Replace `your_discord_token_here` with your actual Discord bot token.

### 3. Configure Ollama Connection

Edit `.config`:
```bash
nano .config
```

**If Ollama runs on your local machine (Docker):**
```
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

**If Ollama runs on another machine on your network:**
```
OLLAMA_BASE_URL=http://192.168.1.X:11434
# Replace 192.168.1.X with your Ollama server's IP
```

**If Ollama runs in a Docker container on the same host:**
```
OLLAMA_BASE_URL=http://ollama:11434
```

### 4. Start the Bot
```bash
docker compose up -d
```

Check logs:
```bash
docker logs -f discord-ollama-bot-bot-1
```

---

## Synology NAS Setup

### Prerequisites
- Synology NAS with Docker support (DSM 6.0+)
- SSH access enabled (or use File Station to upload files)

### Step-by-Step

#### 1. Enable Docker on Synology
1. Open **Package Center**
2. Search for "Docker"
3. Install Docker
4. Wait for installation to complete

#### 2. Upload Repository to NAS
```bash
# From your desktop, SSH into the NAS
ssh admin@192.168.1.NAS_IP

# Clone the bot repository
cd /volume1/docker  # or your preferred location
git clone https://github.com/yourusername/discord-ollama-bot.git
cd discord-ollama-bot
```

OR use **File Station** → upload the folder directly.

#### 3. Create .env File
```bash
cp .env.example .env
nano .env
# Paste your Discord token
```

#### 4. Update .config for Network Ollama
```bash
nano .config
```

Change `OLLAMA_BASE_URL` to your desktop's IP:
```
OLLAMA_BASE_URL=http://192.168.1.100:11434
```

(Replace `192.168.1.100` with your desktop's actual IP)

#### 5. Verify Ollama Accessibility
Before starting the bot, test connectivity from the NAS:
```bash
# SSH into NAS and run:
curl http://192.168.1.100:11434/api/tags

# Expected output: JSON with your loaded models
```

If it times out or refuses connection:
- ✅ Check Ollama is listening on `0.0.0.0` (see [Ollama Windows Setup](#ollama-windows-setup))
- ✅ Check Windows Firewall allows port 11434
- ✅ Verify the IP address is correct

#### 6. Start the Bot
```bash
docker compose up -d
```

#### 7. Monitor Logs
```bash
docker logs -f discord-ollama-bot-bot-1
```

Expected output:
```
discord.client: Logged in as YourBot#1234
Waiting for Discord connection...
```

---

## Persistent Storage

The bot uses a named Docker volume `bot_data` to store:
- Economy balances (`economy.json`)
- Bot roles (`bot_roles.json`)
- Bot admin IDs (`bot_admins.json`)
- Guild settings (`guild_settings.json`)
- Insurance data (`insurance.json`)
- Model settings (`models.json`)

**Volume Location on Synology:**
```
/var/lib/docker/volumes/[stack_name]_bot_data/_data/
```

Or via **Portainer UI**:
1. Go to **Volumes** → `bot_data`
2. Click the folder icon to browse contents

**Backup:**
```bash
# Backup to NAS
docker run --rm -v bot_data:/data -v /backup:/backup \
  busybox tar czf /backup/bot_data_backup.tar.gz /data

# Restore from backup
docker run --rm -v bot_data:/data -v /backup:/backup \
  busybox tar xzf /backup/bot_data_backup.tar.gz -C /data
```

---

## Optional: Portainer UI (Recommended)

Portainer gives you a web dashboard to manage Docker containers without CLI.

### Install Portainer on Synology

```bash
# SSH into NAS
docker run -d \
  -p 8000:8000 \
  -p 9000:9000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v portainer_data:/data \
  --restart=always \
  --name=portainer \
  portainer/portainer-ce:latest
```

### Access Portainer
1. Open browser: `http://192.168.1.NAS_IP:9000`
2. Create admin account on first login
3. Select "Local" environment
4. Go to **Containers** → manage your bot

### Deploy from Docker Compose in Portainer
1. **Stacks** → **Add Stack**
2. Name: `discord-ollama-bot`
3. Copy contents of `docker-compose.yml`
4. Add environment variables (or use `.env` file upload)
5. Deploy

---

## Auto-Update on Git Push (Optional)

### Setup: GitHub Webhook + Watchtower

#### Option A: Watchtower (Simplest)
If you push to Docker Hub:

```bash
docker run -d \
  --name watchtower \
  -v /var/run/docker.sock:/var/run/docker.sock \
  containrrr/watchtower \
  --cleanup
```

Watchtower will auto-pull and restart your container when the image updates.

#### Option B: Portainer Webhook (Recommended for GitHub)
1. In **Portainer UI** → **Endpoints** → select your endpoint
2. Go to your Stack `discord-ollama-bot`
3. Copy the **Webhook URL**
4. In GitHub repo → **Settings** → **Webhooks** → **Add webhook**
   - Payload URL: `[webhook_url_from_portainer]`
   - Content type: `application/json`
   - Trigger: `Push events`
5. When you push to `main`, Portainer will pull latest code and rebuild

#### Option C: Manual Update
```bash
cd /volume1/docker/discord-ollama-bot
git pull origin main
docker compose up --build -d
```

---

## Ollama Windows Setup

### Make Ollama Listen on All Interfaces

1. **Set Environment Variable**
   - Search: "Environment Variables"
   - Click **Edit the system environment variables**
   - **Environment Variables** button
   - New variable:
     - Name: `OLLAMA_HOST`
     - Value: `0.0.0.0:11434`
   - OK → OK → OK

2. **Restart Ollama**
   - Close Ollama (taskbar icon → Quit)
   - Reopen Ollama

3. **Verify (from NAS or another machine)**
   ```bash
   curl http://YOUR_DESKTOP_IP:11434/api/tags
   ```

   Should return JSON with your models.

---

## Troubleshooting

### Bot won't connect to Discord
- ✅ Check `DISCORD_TOKEN` in `.env` is valid (copy-paste carefully, no spaces)
- ✅ Check bot has **Message Content Intent** enabled in Discord Developer Portal
- ✅ Check bot has permissions in your server (at minimum: Send Messages, Read Messages/View Channels)

### Bot can't reach Ollama
- ✅ Test from NAS: `curl http://OLLAMA_IP:11434/api/tags`
- ✅ Check Ollama is running on your desktop
- ✅ Check `OLLAMA_BASE_URL` in `.config` is correct IP, not `localhost`
- ✅ Check Ollama is listening on `0.0.0.0` (not just `127.0.0.1`)

### Logs show errors
```bash
docker logs discord-ollama-bot-bot-1 --tail 50
```

### Volume permissions issues
```bash
# Fix ownership if needed
docker exec discord-ollama-bot-bot-1 chown -R 1000:1000 /app/data
```

### Need to reset data
```bash
# WARNING: This deletes all economy/settings data
docker volume rm [stack_name]_bot_data
docker compose up -d
```

---

## File Structure

```
discord-ollama-bot/
├── .config                 # Non-sensitive config (commit to repo)
├── .env                    # Secret token (git-ignored, local only)
├── .env.example            # Template for .env
├── docker-compose.yml      # Docker setup (Synology-optimized)
├── Dockerfile              # Container build
├── bot.py                  # Main bot code
├── SETUP.md                # This file
├── data/                   # Persistent storage (created on first run)
│   ├── economy.json
│   ├── bot_admins.json
│   ├── guild_settings.json
│   ├── insurance.json
│   ├── models.json
│   └── ... (other data files)
└── requirements.txt        # Python dependencies
```

---

## Support

For issues:
1. Check logs: `docker logs discord-ollama-bot-bot-1`
2. Verify `.config` settings
3. Test Ollama connectivity: `curl http://OLLAMA_IP:11434/api/tags`
4. Check Discord token and intents

