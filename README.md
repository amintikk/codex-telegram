<p align="center">
  <img src="./assets/logo.png" alt="Codex Telegram logo" width="160">
</p>

<h1 align="center">Codex Telegram</h1>

<p align="center">
  Run the official Codex CLI from Telegram, inside Docker, against your host workspace.
</p>

![Docker](https://img.shields.io/badge/runtime-Docker-2496ED?logo=docker&logoColor=white)
![Telegram](https://img.shields.io/badge/chat-Telegram-26A5E4?logo=telegram&logoColor=white)
![Codex CLI](https://img.shields.io/badge/engine-OpenAI%20Codex-111111)
![Host Access](https://img.shields.io/badge/mode-host%20workspace%20%2B%20docker-orange)

## Overview

`codex-telegram` is a clean bridge between Telegram and the official `@openai/codex` CLI.

It does not fork Codex and it does not wrap a fake shell around it. The container installs the official CLI, listens for Telegram messages, runs `codex exec`, and returns the result back to Telegram in readable chunks.

## What It Does

- Runs the official `@openai/codex` CLI inside Docker
- Forwards Telegram messages directly to `codex exec`
- Uses your mounted host workspace as the working directory
- Can operate host Docker workloads through `/var/run/docker.sock`
- Auto-updates Codex from the official npm channel
- Supports isolated login per Telegram chat with official device auth
- Splits long answers into clean Telegram-friendly parts
- Shows `typing...` while Codex is working

## Architecture

```text
Telegram chat
    |
    v
Telegram Bot API
    |
    v
codex-telegram bridge (Python)
    |
    v
official `codex exec` inside Docker
    |
    +--> mounted host workspace
    |
    +--> mounted host Docker socket
    |
    +--> mounted Codex config directory
```

### Important Design Detail

The runtime is containerized, but the workspace and identity can be shared on purpose:

- Code runs inside Docker
- Files come from the host
- Docker operations target the host Docker daemon
- Codex auth/config can be reused from the host by mounting `~/.codex`

That is why this instance can feel "the same" as your normal Codex CLI session even though it runs in a separate container.

## Quick Start

```bash
cd /home/ubuntu/docker/codex-telegram
cp .env.example .env
docker compose up -d --build
```

Then send a message to your Telegram bot:

```text
list my running docker containers
```

## Telegram Commands

| Command | What it does |
| --- | --- |
| any plain message | Sends the prompt to Codex |
| `/run <prompt>` | Explicitly runs a task |
| `/login` | Starts official Codex device login for this chat |
| `/login status` | Shows login state for this chat |
| `/login cancel` | Cancels the current device login |
| `/logout` | Removes stored credentials for this chat |
| `/new` | Starts a fresh Codex conversation for this Telegram chat |
| `/status` | Shows bridge status |
| `/version` | Shows the installed Codex CLI version |
| `/update` | Forces a Codex update check |
| `/help` | Shows quick help |

## Configuration

Main settings live in `.env`.

| Variable | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Comma-separated allowlist of Telegram chat IDs |
| `CODEX_AUTH_MODE` | `per_chat` or `shared` |
| `CODEX_AUTH_ROOT` | Root folder for per-chat Codex profiles |
| `CODEX_CONFIG_DIR` | Mounted Codex config directory |
| `HOST_WORKSPACE` | Host path exposed to Codex inside the container |
| `CODEX_CHANNEL` | npm channel or exact Codex version |
| `CODEX_MODEL` | Optional model override |
| `CODEX_EXTRA_ARGS` | Extra flags passed to `codex exec` |
| `AUTO_UPDATE` | Enable automatic Codex updates |
| `AUTO_UPDATE_MIN_INTERVAL_SECONDS` | Minimum time between update checks |

## What "Allowlist" Means

`TELEGRAM_ALLOWED_CHAT_IDS` is the list of Telegram chats that are allowed to talk to the bot.

If a message comes from any other chat, the bridge ignores it completely.

Example:

```env
TELEGRAM_ALLOWED_CHAT_IDS=2100983129,123456789
```

That is a simple but important protection layer. Without it, anyone who can reach the bot could try to use your Codex instance.

## How Login Works

### Recommended: Per-Chat Login

For public or multi-user deployments, use:

```env
CODEX_AUTH_MODE=per_chat
```

Then every Telegram chat gets its own Codex profile under:

```text
/data/auth/<chat_id>/home/.codex
```

The user logs in by sending:

```text
/login
```

The bot returns the official OpenAI device-auth URL and one-time code. Once the browser flow is completed, the bot confirms the login in Telegram and that chat is ready to use.

### Shared Login

This is the mode you were using locally.

The compose file can mount:

```text
<host config dir> -> /root/.codex
```

So if you are already logged in on the host, the container sees the same Codex credentials and config.

That is why Telegram did not need a separate login flow in your case.

If someone wants a shared profile without reusing the host session, they can point `CODEX_CONFIG_DIR` at a local folder such as:

```env
CODEX_CONFIG_DIR=./shared-codex
```

## Why This Instance Shares the Current Session

Because the session is not magically inherited. It is explicitly shared by the mounted config directory.

The separation looks like this:

- Process isolation: yes, Codex runs in Docker
- Filesystem identity/config isolation: no, not if you mount the same `~/.codex`
- Workspace isolation: no, not if you mount the host workspace

So the container is isolated as a runtime, but intentionally not isolated from the Codex identity and host project files.

## How Updates Work

By default, the bridge checks the configured npm channel before runs and updates Codex automatically if needed.

Examples:

```env
CODEX_CHANNEL=latest
CODEX_CHANNEL=alpha
CODEX_CHANNEL=0.121.0
```

You can also trigger an immediate check from Telegram with:

```text
/update
```

## Security Notes

This project can be very powerful depending on what you mount.

If you mount:

- the host workspace
- the host Docker socket
- the host Codex config

then the Telegram bot effectively has broad control over that environment.

That is useful, but it should be an explicit choice.

## Included Files

- `docker-compose.yml`: service definition
- `.env.example`: configuration template
- `services/bridge/Dockerfile`: runtime image
- `services/bridge/app.py`: Telegram bridge
- `services/bridge/entrypoint.sh`: container startup

## Production Notes

- Keep the Telegram bot private or protected by a strict chat allowlist
- Treat `/var/run/docker.sock` as highly privileged access
- Prefer `CODEX_AUTH_MODE=per_chat` for public deployments
- Pin `CODEX_CHANNEL` to an exact version if you need reproducibility

## Status

This stack is designed to stay close to the official Codex CLI:

- official package
- normal npm update path
- Dockerized runtime
- Telegram control layer kept separate from the Codex binary itself
