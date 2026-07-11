# Setup Guide

This guide covers every way to run CatGPT Gateway: Docker, local development, and Nix.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Docker Setup (recommended)](#docker-setup-recommended)
- [Local Setup (no Docker)](#local-setup-no-docker)
- [Nix Flake Setup](#nix-flake-setup)
- [First Login](#first-login)
- [Switching Providers](#switching-providers)
- [Authentication](#authentication)
- [Docker Internals](#docker-internals)
- [systemd Service (optional)](#systemd-service-optional)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

- **Python 3.11 through 3.14 and uv** (local setup only)
- **Docker + Docker Compose** (Docker setup only)
- A **ChatGPT** or **Claude** account (free or paid)

---

## Docker Setup (recommended)

Docker runs the entire stack in one container: virtual display, VNC, browser, and API server.

```bash
# 1. Clone the repo
git clone <your-fork-url> CatGPT
cd CatGPT

# 2. Configure the values consumed by docker-compose.yml
cat > .env <<'EOF'
DOCKERDIR=/path/to/persistent/storage
CATGPT_API_KEY=change-this-api-token
CATGPT_VNC_PASSWORD=change-this-vnc-password
CHATGPT_PROJECT_URL=
# Optional: image published by your fork
# CATGPT_IMAGE=ghcr.io/your-user/catgpt:latest
EOF

# 3. Build and start
docker compose up --build -d

# 4. First login (one-time) - open the browser UI
open http://localhost:6080
# Sign into ChatGPT in the browser window you see
# Close the noVNC tab when done - session is saved automatically

# 5. Verify it works (Docker publishes the API on host port 8650)
curl -H "Authorization: Bearer change-this-api-token" http://localhost:8650/v1/models
# {"object":"list","data":[{"id":"catgpt-browser",...}]}

# 6. Send your first message
curl -X POST http://localhost:8650/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer change-this-api-token" \
  -d '{
    "model": "catgpt-browser",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Docker Notes

- **Code is baked into the image.** After editing source files, rebuild:
  ```bash
  docker compose up --build -d   # rebuilds and restarts
  ```
  `docker restart catgpt` does NOT pick up code changes.

- **Browser session persists** in `${DOCKERDIR}/appdata/catgpt/browser`. You only need to log in once.

- **Logs and conversation state** are stored below `${DOCKERDIR}/appdata/catgpt/`.

- **noVNC** at `http://localhost:6080` lets you see and interact with the browser (useful for debugging, CAPTCHAs, or re-login). Default VNC password: `catgpt`.

---

## Local Setup (no Docker)

```bash
# 1. Clone and enter the repo
git clone <your-fork-url> CatGPT
cd CatGPT

# 2. Create the locked development environment
uv sync --group dev

# 3. Install Chromium for Patchright
uv run patchright install chromium

# 4. Copy and configure environment
cp .env.example .env
# Edit .env -> set PROVIDER=claude or PROVIDER=chatgpt

# 5. First login (one-time)
uv run python scripts/first_login.py
# A browser window opens. Sign into your provider. Press Enter when done.

# 6. Start the API server
uv run python -m src.api.server
# API is live at http://localhost:8000

# 7. (Optional) Start the terminal chat UI
uv run python -m src.cli.app
```

---

## Nix Flake Setup

This repo ships a `flake.nix` that packages Patchright and matching Chromium revisions.

```bash
# 1. Copy env template
cp .env.example .env

# 2. First login (one-time, interactive)
nix run .#login

# 3. Start the proxy
nix run .#proxy

# 4. Optional: run the TUI
nix run .#tui
```

Notes:
- The app reads `./.env` from your current working directory if present.
- Shell environment variables override values from `.env`.

---

## First Login

CatGPT Gateway uses your existing browser session. You sign in **once** and the browser profile is persisted.

> Google OAuth commonly rejects automated Chromium sessions. If it does, use
> email and password, Microsoft, Apple, or a magic link / OTP instead.

### Docker

1. Start the container: `docker compose up --build -d`
2. Wait ~30 seconds for startup
3. Open **http://localhost:6080/vnc.html** (noVNC) in your browser
4. You'll see a Chromium browser inside the VNC viewer
5. Sign into your provider using one of these methods:
   | Method | Works? |
   |---|---|
   | Email + password | ✅ Recommended |
   | Microsoft account | ✅ Works |
   | Apple ID | ✅ Works |
   | Magic link / OTP email | ✅ Works |
   | **Google / "Continue with Google"** | ❌ Blocked by Google |
6. Verify you see the chat interface
7. Close the noVNC tab. The bind-mounted browser profile survives container restarts.

### Local

1. Run `uv run python scripts/first_login.py`
2. A Chromium window opens and navigates to your provider
3. Sign in using **email + password** or a non-Google method (see table above)
4. Press Enter in the terminal when you see the chat page
5. The browser closes. Session is saved in `browser_data/` (or `browser_data_claude/`).

### Re-login

If your session expires (typically after days/weeks), repeat the login flow. The API returns a 503 error when the session is expired.

---

## Switching Providers

Edit your `.env` file:

```bash
# For Claude
PROVIDER=claude
BROWSER_DATA_DIR=./browser_data_claude

# For ChatGPT
PROVIDER=chatgpt
BROWSER_DATA_DIR=./browser_data
```

Each provider has its own browser data directory so your login sessions don't conflict. After switching, restart the server.

For Docker, also update the `PROVIDER` in `docker-compose.yml` under `environment:` and rebuild.

---

## Authentication

### API Bearer Token

All API endpoints require a Bearer token when `API_TOKEN` is set.

```bash
curl -H "Authorization: Bearer dummy123" http://localhost:8000/v1/models
```

With the OpenAI SDK or LangChain, pass the token as `api_key`:

```python
client = OpenAI(base_url="http://localhost:8000/v1", api_key="dummy123")
```

**Open paths** (no token required): `/docs`, `/redoc`, `/openapi.json`, `/healthz`

To disable auth, set `API_TOKEN=` (empty string) in `.env`.

### noVNC Password

The noVNC browser UI at `http://localhost:6080` is password-protected.

Default: `catgpt`. Change it via `VNC_PASSWORD` in `.env` or `docker-compose.yml`.

---

## Docker Internals

### Container Services (managed by supervisord)

| Service | Port | Purpose |
|---|---|---|
| Xvfb | `:99` | Virtual framebuffer. Chrome renders here. |
| x11vnc | `5900` | VNC server capturing the Xvfb display |
| noVNC | `6080` | WebSocket bridge. Browser-accessible VNC viewer. |
| FastAPI | `8000` | API server (OpenAI-compatible + custom REST) |

### Startup Sequence

1. Create directories (`browser_data`, `logs`, `downloads/images`, `downloads/audio`)
2. Clean stale Chrome lock files
3. Set up VNC password
4. Pre-resolve DNS domains and write to `/etc/hosts` (Docker DNS workaround)
5. Verify Xvfb and Patchright Chromium
6. Start supervisord (manages all 4 services)

### Volumes

| Volume | Purpose |
|---|---|
| `${DOCKERDIR}/appdata/catgpt/browser:/app/browser_data` | Persistent browser session (cookies, login) |
| `${DOCKERDIR}/appdata/catgpt/logs:/app/logs` | Logs accessible from host |
| `${DOCKERDIR}/appdata/catgpt/state:/app/state` | Durable conversation and Responses routing ledger |

### Health Check

The container has a built-in health check hitting `/healthz` every 30 seconds.

```bash
docker inspect --format='{{.State.Health.Status}}' catgpt
```

---

## systemd Service (optional)

For running as a background service with the Nix flake:

```ini
# ~/.config/systemd/user/catgpt.service
[Unit]
Description=CatGPT Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/Projects/CatGPT
ExecStart=/usr/bin/env nix run .#proxy
Restart=on-failure
RestartSec=5
Environment=HEADLESS=true
Environment=API_TOKEN=your-token-here
Environment=API_HOST=127.0.0.1
Environment=API_PORT=8000

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now catgpt
journalctl --user -u catgpt -f
```

---

## Troubleshooting

### "ChatGPT client not initialized" (503)

The browser hasn't finished starting. Wait 30-45 seconds after startup.

```bash
# Check logs
docker logs catgpt --tail 50      # Docker
cat logs/api_server.log            # Local
```

### "Not logged in" / session expired

Re-login:
- Docker: Open http://localhost:6080 and sign in
- Local: Run `uv run python scripts/first_login.py`

### Stale browser lock files

If the app crashes, orphan Chrome processes may leave lock files:

```bash
pkill -f "chrome-for-testing" 2>/dev/null
rm -f browser_data/SingletonLock browser_data/SingletonSocket browser_data/SingletonCookie
```

The app auto-cleans these on startup, but manual cleanup may be needed after hard crashes.

### Docker DNS issues

Chrome inside Docker sometimes fails to resolve domains. The entrypoint script pre-resolves domains via Python. If you still see DNS errors:

```bash
docker exec catgpt cat /etc/hosts
docker exec catgpt curl -s https://chatgpt.com
```

### Code changes not taking effect (Docker)

You must rebuild:

```bash
docker compose up --build -d   # correct
# NOT: docker restart catgpt   # this uses the old image
```

### Services not running

```bash
docker exec catgpt supervisorctl status
```

All 4 services (xvfb, vnc, novnc, catgpt) should show `RUNNING`.
