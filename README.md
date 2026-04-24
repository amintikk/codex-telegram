<p align="center">
  <img src="./assets/logo.png" alt="Codex Telegram Bridge logo" width="160">
</p>

<h1 align="center">Codex Telegram Bridge</h1>

<p align="center">
  Official Codex CLI in Docker, controlled from Telegram.
</p>

<p align="center">
  <img alt="Docker" src="https://img.shields.io/badge/runtime-Docker-2496ED?logo=docker&logoColor=white">
  <img alt="Telegram" src="https://img.shields.io/badge/chat-Telegram-26A5E4?logo=telegram&logoColor=white">
  <img alt="Codex CLI" src="https://img.shields.io/badge/engine-OpenAI%20Codex-111111">
</p>

## What it is

This project runs the official `@openai/codex` CLI inside Docker and exposes it through a Telegram bot.

The bridge only:

- receives Telegram messages
- runs `codex exec`
- sends the result back to Telegram

It supports:

- per-chat login with official device auth
- persistent chat sessions with `/new`
- image input from Telegram photos and image documents
- host workspace access
- optional host Docker access
- automatic Codex updates

## Quick start

1. Create a Telegram bot with BotFather.
2. Get your Telegram `chat_id`.
3. Copy the config template:

```bash
cd /home/ubuntu/docker/codex-telegram
cp .env.example .env
```

4. Fill the minimum config:

```env
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_ALLOWED_CHAT_IDS=your_chat_id
CODEX_AUTH_MODE=per_chat
CODEX_AUTH_ROOT=/data/auth
HOST_WORKSPACE=/host/home/ubuntu
CODEX_CHANNEL=latest
```

5. Start it:

```bash
docker compose up -d --build
```

6. In Telegram, run:

```text
/login
```

Complete the OpenAI device-auth flow in your browser.

7. Send a task:

```text
list all running docker containers on this machine
```

## Commands

| Command | Purpose |
| --- | --- |
| any plain message | Run the prompt through Codex |
| photo or image document | Send the image to Codex, using the caption as the prompt when present |
| `/run <prompt>` | Run a task explicitly |
| `/login` | Start official Codex login for this chat |
| `/login status` | Show login state |
| `/login cancel` | Cancel a pending login |
| `/logout` | Remove stored credentials for this chat |
| `/new` | Start a fresh Codex conversation |
| `/limits` | Show the latest Codex quota state for this chat |
| `/status` | Show bridge status |
| `/cron` | View and manage scheduled tasks |
| `/version` | Show installed Codex CLI version |
| `/update` | Force a Codex update check |
| `/help` | Show quick help |

## Auth modes

### `per_chat` recommended

Each Telegram chat gets its own Codex profile under `/data/auth/<chat_id>/home/.codex`.

Use this for public or multi-user deployments.

### `shared`

All chats reuse the same Codex profile.

Use this only for a private single-operator setup.

## Important config

| Variable | Meaning |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Comma-separated allowlist of allowed chats |
| `CODEX_AUTH_MODE` | `per_chat` or `shared` |
| `CODEX_AUTH_ROOT` | Root directory for per-chat auth |
| `CODEX_CONFIG_DIR` | Shared Codex profile path |
| `HOST_WORKSPACE` | Workspace path exposed inside the container |
| `CODEX_CHANNEL` | npm channel or exact version |
| `CODEX_MODEL` | Optional model override |
| `CODEX_EXTRA_ARGS` | Extra flags passed to `codex exec` |

## Sessions

The bridge keeps one active Codex thread per Telegram chat.

- normal messages continue the current conversation
- `/new` starts a fresh one

## Cron jobs

You can schedule prompts to run automatically from Telegram.

Examples:

```text
/cron
```

```text
/cron add docker-check | */30 * * * * | list all running docker containers
```

```text
/cron pause <id>
/cron resume <id>
/cron delete <id>
/cron run <id>
```

Cron jobs are stored per chat and run using that chat's current login, model, and thinking settings.

## Updating

The bridge checks the configured npm channel and updates Codex automatically when needed.

You can also force it manually:

```text
/update
```

## Security

This bot can be very powerful depending on what you mount.

Be careful with:

- `TELEGRAM_ALLOWED_CHAT_IDS`
- `/var/run/docker.sock`
- shared Codex profiles
- aggressive `CODEX_EXTRA_ARGS`

For public use, prefer:

- `CODEX_AUTH_MODE=per_chat`
- a strict allowlist
- exact version pinning when stability matters

## Files

- `docker-compose.yml` - service definition
- `.env.example` - config template
- `services/bridge/Dockerfile` - runtime image
- `services/bridge/app.py` - Telegram bridge
- `services/bridge/entrypoint.sh` - container startup
- `assets/logo.png` - branding asset

## Troubleshooting

### Bot does not answer

Check:

```bash
docker compose logs -f
```

Then confirm:

- the bot token is correct
- the chat id is allowlisted
- the container is running

### Bot asks for login

Run:

```text
/login
```

### Tasks fail

Check:

- Codex login completed successfully
- the workspace mount is correct
- Docker socket access exists if your task needs Docker
- your `CODEX_EXTRA_ARGS` make sense for the environment
