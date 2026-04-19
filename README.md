<p align="center">
  <img src="./assets/logo.png" alt="Codex Telegram Bridge logo" width="160">
</p>

<h1 align="center">Codex Telegram Bridge</h1>

<p align="center">
  Run the official Codex CLI from Telegram, inside Docker, against your host workspace.
</p>

<p align="center">
  <img alt="Docker" src="https://img.shields.io/badge/runtime-Docker-2496ED?logo=docker&logoColor=white">
  <img alt="Telegram" src="https://img.shields.io/badge/chat-Telegram-26A5E4?logo=telegram&logoColor=white">
  <img alt="Codex CLI" src="https://img.shields.io/badge/engine-OpenAI%20Codex-111111">
  <img alt="Host Access" src="https://img.shields.io/badge/mode-host%20workspace%20%2B%20docker-orange">
</p>

## Overview

`codex-telegram-bridge` is a lightweight control layer around the official `@openai/codex` CLI.

It runs Codex inside Docker, listens for Telegram messages, forwards them to `codex exec`, and sends the results back to Telegram in readable chunks. It does not patch Codex, fork Codex, or replace the official login flow.

This project is designed for people who want:

- the official Codex CLI
- Telegram as the control surface
- Docker as the runtime boundary
- optional access to the host workspace and host Docker daemon

## Highlights

- Official `@openai/codex` package inside Docker
- Telegram long-poll bridge with clean HTML formatting
- Official OpenAI device-auth login flow from Telegram
- Per-chat isolated Codex sessions, or optional shared mode
- Persistent Codex conversations with `/new`
- Automatic CLI updates from npm, plus manual `/update`
- Long output chunking that works well with Telegram tables and code blocks

## How It Works

```text
Telegram chat
    |
    v
Telegram Bot API
    |
    v
Bridge service (Python)
    |
    v
Official `codex exec` inside Docker
    |
    +--> mounted host workspace
    +--> mounted host Docker socket
    +--> mounted Codex auth/config
```

The important architectural detail is this:

- the runtime is containerized
- the workspace can still be the host workspace
- Docker operations can still target the host daemon
- authentication can be isolated per chat or shared intentionally

That is why the project feels powerful without modifying the Codex CLI itself.

## Quick Start

This is the recommended first-run flow.

### 1. Create a Telegram bot

Use BotFather and copy the bot token.

### 2. Get your Telegram chat id

The bridge uses an allowlist, so you need the numeric `chat_id` for the chats allowed to use it.

### 3. Copy the config template

```bash
cd /home/ubuntu/docker/codex-telegram
cp .env.example .env
```

### 4. Edit `.env`

Minimum setup:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_ALLOWED_CHAT_IDS=your_numeric_chat_id
CODEX_AUTH_MODE=per_chat
CODEX_AUTH_ROOT=/data/auth
HOST_WORKSPACE=/host/home/ubuntu
CODEX_CHANNEL=latest
```

### 5. Start the stack

```bash
docker compose up -d --build
```

### 6. Open Telegram and log in

In the bot chat:

```text
/login
```

The bot will return:

- the official OpenAI device-auth URL
- a one-time code

Finish the login in your browser. The bot will confirm when the session is ready.

### 7. Send your first task

```text
list all running docker containers on this machine
```

## First-Run Experience

Once the bot is running, the normal flow looks like this:

1. `/login`
2. Complete device auth in the browser
3. Send tasks normally
4. Use `/new` whenever you want a fresh Codex conversation

Commands like `/status` and `/login status` help confirm what state the bridge is in.

## Telegram Commands

| Command | Purpose |
| --- | --- |
| any plain message | Run the prompt through Codex |
| `/run <prompt>` | Run a task explicitly |
| `/login` | Start official Codex device login for this chat |
| `/login status` | Show login state for this chat |
| `/login cancel` | Cancel a pending device-auth login |
| `/logout` | Remove stored credentials for this chat |
| `/new` | Start a new Codex conversation for this chat |
| `/status` | Show bridge status, auth mode, and session info |
| `/version` | Show installed Codex CLI version |
| `/update` | Force a Codex update check |
| `/help` | Show quick help |

## Session Model

The bridge keeps an active Codex thread per Telegram chat.

That means:

- normal messages continue the current Codex conversation
- `/new` discards that active thread reference
- the next message starts a fresh Codex conversation

This makes the bot feel much closer to a real chat workflow instead of stateless one-shot commands.

## Auth Modes

### Recommended: `per_chat`

```env
CODEX_AUTH_MODE=per_chat
CODEX_AUTH_ROOT=/data/auth
```

Each Telegram chat gets its own isolated Codex profile under:

```text
/data/auth/<chat_id>/home/.codex
```

This is the right default for public or multi-user setups.

### Optional: `shared`

```env
CODEX_AUTH_MODE=shared
CODEX_CONFIG_DIR=./shared-codex
```

All chats reuse the same Codex identity.

This is useful for a private single-operator bot, but it is not the right default for public deployments.

## Configuration

Main settings live in `.env`.

| Variable | Meaning |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Comma-separated allowlist of allowed chats |
| `CODEX_AUTH_MODE` | `per_chat` or `shared` |
| `CODEX_AUTH_ROOT` | Root directory for per-chat auth profiles |
| `CODEX_CONFIG_DIR` | Mounted shared Codex profile |
| `HOST_WORKSPACE` | Workspace path exposed to Codex inside the container |
| `CODEX_CHANNEL` | npm channel or exact version |
| `CODEX_MODEL` | Optional model override |
| `CODEX_EXTRA_ARGS` | Extra flags passed to `codex exec` |
| `AUTO_UPDATE` | Enable automatic Codex updates |
| `AUTO_UPDATE_MIN_INTERVAL_SECONDS` | Minimum interval between update checks |
| `TELEGRAM_PARSE_MODE` | Telegram formatting mode |
| `TELEGRAM_POLL_SECONDS` | Long-poll timeout |

## Why the Allowlist Matters

`TELEGRAM_ALLOWED_CHAT_IDS` is a hard gate.

If a message comes from a chat that is not listed, the bridge ignores it. That matters because this bot can be wired to:

- your host files
- your host Docker daemon
- a real Codex identity

Without an allowlist, the bot would be far too exposed.

Example:

```env
TELEGRAM_ALLOWED_CHAT_IDS=2100983129,123456789
```

## Updating Codex

The bridge keeps the official Codex CLI current through npm.

Examples:

```env
CODEX_CHANNEL=latest
CODEX_CHANNEL=alpha
CODEX_CHANNEL=0.122.0-alpha.1
```

Normal behavior:

- the bridge checks for updates before runs
- it only updates when needed
- it respects the configured minimum interval

Manual refresh:

```text
/update
```

## Deployment Modes

### Private single-user setup

Good fit for a personal admin bot.

Recommended:

```env
TELEGRAM_ALLOWED_CHAT_IDS=<your_chat_id>
CODEX_AUTH_MODE=shared
CODEX_CONFIG_DIR=./shared-codex
```

### Public or multi-user setup

Good fit when each user should authenticate separately.

Recommended:

```env
TELEGRAM_ALLOWED_CHAT_IDS=<approved_chat_ids>
CODEX_AUTH_MODE=per_chat
CODEX_AUTH_ROOT=/data/auth
```

## Security Notes

This project can be very powerful depending on what you mount.

If you mount:

- the host workspace
- `/var/run/docker.sock`
- a shared Codex profile

then the bot effectively has broad access to that environment.

That may be intentional, but it should never be accidental.

Production recommendations:

- keep the Telegram bot private or tightly allowlisted
- prefer `CODEX_AUTH_MODE=per_chat` for public use
- treat the Docker socket as highly privileged
- pin `CODEX_CHANNEL` to an exact version if you need reproducibility
- review `CODEX_EXTRA_ARGS` carefully before exposing the bot

## Examples

Prompts that work well:

```text
show me every docker container on this machine
```

```text
tail the logs of the codex-telegram container
```

```text
check why my compose stack failed to start
```

```text
open a new conversation and help me review this repo
```

## Files

- `docker-compose.yml` - service definition
- `.env.example` - config template
- `services/bridge/Dockerfile` - runtime image
- `services/bridge/app.py` - Telegram bridge
- `services/bridge/entrypoint.sh` - container startup
- `assets/logo.png` - repository branding

## Troubleshooting

### The bot does not answer

Check:

- the container is running
- the bot token is correct
- the chat id is in `TELEGRAM_ALLOWED_CHAT_IDS`

```bash
docker compose logs -f
```

### The bot says login is required

Run:

```text
/login
```

Then complete the device-auth flow in the browser.

### The bot answers, but tasks fail

Check:

- the Codex login actually completed
- the mounted workspace path is correct
- Docker socket access is present if your task needs Docker
- `CODEX_EXTRA_ARGS` are appropriate for your environment

### The output is very long

That is expected for some tasks. The bridge splits long responses into multiple Telegram messages and labels them clearly.

## Notes on Naming

The product name used in the README is `Codex Telegram Bridge`.

The current repository name may still be `codex-telegram`, but the recommended public-facing naming is:

- product: `Codex Telegram Bridge`
- repo: `codex-telegram-bridge`

## Status

This stack stays intentionally close to the official Codex CLI:

- official package
- normal npm update path
- Dockerized runtime
- Telegram control layer kept separate from the Codex binary
