# CatGPT

CatGPT exposes a logged-in ChatGPT browser session as an OpenAI-compatible API.
It is meant for self-hosted use where you want apps such as Open WebUI,
LangChain, scripts, or internal tools to talk to ChatGPT through one persistent
browser profile.

## Table of Contents

1. [What is CatGPT](#what-is-catgpt)
2. [How to Setup on Docker](#how-to-setup-on-docker)
3. [Local Development with uv](#local-development-with-uv)
4. [Supported Models](#supported-models)
5. [Docker Environment Variables](#docker-environment-variables)
6. [Usage Examples](#usage-examples)
7. [Troubleshooting](#troubleshooting)

## What is CatGPT

CatGPT runs Chromium with a saved ChatGPT login, then wraps that browser session
with HTTP APIs that look like OpenAI and Ollama.

This version supports:

| Capability | Supported |
| --- | :---: |
| OpenAI `/v1/chat/completions` | Yes |
| OpenAI `/v1/responses` | Yes |
| Async chat jobs | Yes |
| OpenAI `/v1/images/generations` through ChatGPT image generation | Yes |
| Ollama `/api/chat`, `/api/generate`, `/api/tags` compatibility | Yes |
| Tool / function calling compatibility | Yes |
| Image input / vision requests | Yes |
| File attachments | Yes |
| App-scoped routes such as `/mealie/v1/chat/completions` | Yes |
| App-thread isolation using request identity | Yes |
| Durable conversation routing and Responses continuations | Yes |
| Model switching for current ChatGPT picker models | Yes |
| noVNC browser login UI | Yes |

The default browser-backed model id is `catgpt-browser`. You can also request
specific ChatGPT picker models returned by `GET /v1/models`.

## How to Setup on Docker

The repository includes a production-style `docker-compose.yml`. By default it
builds the checked-out source and tags it with this fork's image name:

```yaml
image: ghcr.io/nikitamgrimm/catgpt:latest
pull_policy: build
```

This means plain `docker compose up -d` uses the fork's code instead of pulling
the upstream image. To deploy a published image, set `CATGPT_IMAGE` and
`CATGPT_PULL_POLICY=always`. Use `docker compose up --build -d` whenever you
want to force an immediate local rebuild.

1. Clone the repo.

```bash
git clone <repo-url> catgpt
cd catgpt
```

2. Create the host variables used by `docker-compose.yml`.

```bash
export DOCKERDIR=/path/to/docker
export CATGPT_API_KEY=change-this-api-token
export CATGPT_VNC_PASSWORD=change-this-vnc-password
```

If you prefer a `.env` file beside `docker-compose.yml`:

```env
DOCKERDIR=/path/to/docker
CATGPT_API_KEY=change-this-api-token
CATGPT_VNC_PASSWORD=change-this-vnc-password
```

3. Start CatGPT.

```bash
docker compose up -d
```

4. Log in to ChatGPT once.

Open:

```text
http://localhost:6080
```

Enter the VNC password from `CATGPT_VNC_PASSWORD`, then log in to ChatGPT inside
the browser. The session is saved in:

```text
${DOCKERDIR}/appdata/catgpt/browser
```

5. Check the API.

The compose file maps the API to host port `8650`.

```bash
curl -H "Authorization: Bearer $CATGPT_API_KEY" \
  http://localhost:8650/v1/models
```

6. Send a message.

```bash
curl -X POST http://localhost:8650/v1/chat/completions \
  -H "Authorization: Bearer $CATGPT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "catgpt-browser",
    "messages": [
      {"role": "user", "content": "Say hello from CatGPT in one sentence."}
    ]
  }'
```

After code changes, rebuild instead of only restarting:

```bash
docker compose up --build -d
```

## Local Development with uv

The repository uses `pyproject.toml` and the committed `uv.lock` as its single
Python dependency source. Install the locked development environment and the
Patchright browser once:

```bash
uv sync --group dev
uv run patchright install chromium
cp .env.example .env
```

Then run CatGPT and its deterministic test suite through the same environment:

```bash
uv run python -m src.api.server
uv run python -m unittest discover -s tests
```

See [docs/SETUP.md](docs/SETUP.md) for login and provider details and
[docs/TESTING.md](docs/TESTING.md) for automated and live browser checks. The
login-free CI suite runs on Python 3.11 and 3.14 for pushes and pull requests.

## Supported Models

CatGPT exposes `catgpt-browser` plus the concrete models discovered from the
logged-in account's live picker. Different Free, Plus, Pro, Business, and
Enterprise accounts can therefore expose different `/v1/models` results.

Picker labels are converted to lowercase API-style slugs: punctuation and
spaces become hyphens while dotted versions are preserved. For example, a
label shaped like `GPT-X.Y Variant` becomes `gpt-x.y-variant`. Requests using a
base GPT family id can match a single discovered variant from that family.

`catgpt-browser`, `auto`, `default`, and `browser` keep the current browser
model unless `CHATGPT_DEFAULT_MODEL` is set. `CHATGPT_MODEL_ALIASES` remains an
optional `public-id=Visible Label|Alternate Label` override for unusual or
legacy picker layouts; it is empty by default. Reasoning rows are discovered
per model. Use Chat Completions `reasoning_effort`, Responses
`reasoning: {"effort": "high"}`, or a generated id such as
`gpt-5.6-sol-high` from `/v1/models`. A model-id suffix takes precedence over
an explicit reasoning field, and unsupported efforts clamp to the closest
visible row.

The first `GET /v1/models` call after startup can take roughly 20–30 seconds:
CatGPT temporarily visits each visible model to discover its reasoning rows,
then restores the picker state it found before discovery. Later calls use a
short-lived catalog and are normally fast.

## Docker Environment Variables

### Required by the included compose file

| Variable | Example | Purpose |
| --- | --- | --- |
| `DOCKERDIR` | `/mnt/user/appdata` | Host root for persisted browser data and logs |
| `CATGPT_API_KEY` | `change-me` | Value passed into container as `API_TOKEN` |
| `CATGPT_VNC_PASSWORD` | `change-me` | Value passed into container as `VNC_PASSWORD` |

### Browser and provider

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROVIDER` | `chatgpt` | Browser provider. Use `chatgpt` for CatGPT; `claude` is also wired in the codebase. |
| `CHATGPT_URL` | `https://chatgpt.com` | ChatGPT URL to open. |
| `CLAUDE_URL` | `https://claude.ai` | Claude URL when `PROVIDER=claude`. |
| `HEADLESS` | `false` | Keep `false` for Docker/noVNC. |
| `BROWSER_CHANNEL` | `chrome` | Browser channel. Use `chromium` to force bundled Chromium. |
| `BROWSER_DATA_DIR` | `browser_data` | Persistent browser profile path inside the container. |
| `SLOW_MO` | `25` | Browser automation delay in milliseconds. |
| `DISPLAY` | `:99` | X display used by the container. |
| `DISPLAY_WIDTH` | `1366` in compose | Virtual display width. |
| `DISPLAY_HEIGHT` | `768` in compose | Virtual display height. |
| `DISPLAY_DEPTH` | `24` in compose | Virtual display depth. |

### Model switching

| Variable | Default | Purpose |
| --- | --- | --- |
| `CHATGPT_DEFAULT_MODEL` | empty | Model to use when a request asks for `catgpt-browser`, `auto`, or `default`. |
| `CHATGPT_MODEL_ALIASES` | empty | Optional comma-separated `model=label|alias` override map for legacy or unusual picker labels. |
| `CHATGPT_PROJECT_URL` | empty | Optional ChatGPT project URL. New chats are created only in that project. Example: `https://chatgpt.com/g/g-p-00000000000000000000000000000000-example/project` (fake). |
| `CHATGPT_MODEL_SWITCH_TIMEOUT` | `10000` | Milliseconds to wait for a model label after switching. |
| `CHATGPT_MODEL_DISCOVERY_TTL_SECONDS` | `600` | Seconds before the live model catalog is rediscovered. |

### API

| Variable | Default | Purpose |
| --- | --- | --- |
| `API_HOST` | `0.0.0.0` | Server bind host inside the container. |
| `API_PORT` | `8000` | Server port inside the container. |
| `API_TOKEN` | empty in code, set from `CATGPT_API_KEY` in compose | Bearer token. Empty disables auth. |
| `API_TOKEN_OPTIONAL` | `false`, `true` in compose | Allow requests without a token while still accepting token auth. |
| `RATE_LIMIT_SECONDS` | `5` | Basic request pacing. |
| `API_THREAD_CONTRACT_MODE` | `false`, `true` in compose | Cache large system instructions in a thread and send compact reminders. |
| `API_THREAD_CONTRACT_TTL_SECONDS` | `3600` | Thread contract TTL. |
| `API_APP_THREAD_MODE` | `false`, `true` in compose | Route different apps/users to dedicated ChatGPT threads. |
| `API_APP_THREAD_TTL_SECONDS` | `86400` | App-thread mapping TTL. |
| `API_APP_THREAD_DELETE_EXPIRED` | `false`, `true` in compose | Delete expired CatGPT-owned app threads from ChatGPT UI. |
| `API_CONVERSATION_DB` | `state/conversations.sqlite3` | SQLite ledger for logical conversations, transcripts, browser threads, and Responses IDs. |
| `API_HEADER_ROW_MERGE_MODE` | `false`, `true` in compose | Merge header-only rows into the next row for structured JSON outputs. |

### Timeouts and interaction pacing

| Variable | Default | Purpose |
| --- | --- | --- |
| `RESPONSE_TIMEOUT` | `120000` | Max response wait in milliseconds. |
| `SELECTOR_TIMEOUT` | `10000` | DOM selector wait in milliseconds. |
| `POLL_INTERVAL_MS` | `300` | Response completion polling interval. |
| `TYPING_SPEED_MIN` | `50` | Minimum typing delay. |
| `TYPING_SPEED_MAX` | `150` | Maximum typing delay. |
| `THINKING_PAUSE_MIN` | `500`, `1000` in compose | Minimum pause before actions. |
| `THINKING_PAUSE_MAX` | `1500`, `3000` in compose | Maximum pause before actions. |

### Attachments, generated files, and Ollama compatibility

| Variable | Default | Purpose |
| --- | --- | --- |
| `ATTACHMENT_EXPAND_MULTIPAGE` | `true` | Expand supported multipage files for per-page extraction. |
| `ATTACHMENT_MAX_PAGES` | `24` | Max pages/frames to render from attachments. |
| `ATTACHMENT_RENDER_DPI` | `144` | Render DPI for attachment extraction. |
| `IMAGES_DIR` | `downloads/images` | Generated/downloaded image output path. |
| `AUDIO_DIR` | `downloads/audio` | Read-aloud audio output path. |
| `LOG_DIR` | `logs` | Log path. |
| `OLLAMA_EMBEDDING_MODELS` | `nomic-embed-text` | Names exposed as Ollama-compatible embedding models. |
| `OLLAMA_EMBEDDING_DIMENSIONS` | `768` | Dimension count for deterministic compatibility embeddings. |
| `OLLAMA_ACTIVE_MODEL_TTL_SECONDS` | `900` | TTL for `/api/ps` active model tracking. |

### VNC and logging

| Variable | Default | Purpose |
| --- | --- | --- |
| `VNC_PASSWORD` | `catgpt` | Password for `http://localhost:6080`. |
| `LOG_LEVEL` | `DEBUG` | Python logging level. |
| `VERBOSE` | `true` | Log to console as well as files. |

## Usage Examples

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8650/v1",
    api_key="change-this-api-token",
)

response = client.chat.completions.create(
    model="catgpt-browser",
    messages=[
        {"role": "user", "content": "Summarize why browser-backed APIs are useful."}
    ],
)

print(response.choices[0].message.content)
```

### Responses API

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8650/v1", api_key="change-this-api-token")

response = client.responses.create(
    model="catgpt-browser",
    instructions="Be concise.",
    input="Give me three setup checks for CatGPT.",
)

print(response.output[0].content[0].text)
```

Continue that Responses chain using the conventional response id:

```python
follow_up = client.responses.create(
    model="catgpt-browser",
    previous_response_id=response.id,
    input="Now reduce that to the single most important check.",
)
```

You may instead pass a stable `conversation="conv_my_app_chat_123"`. As in the
OpenAI Responses API, `conversation` and `previous_response_id` cannot be used
together.

### Durable Chat Completions conversations

Chat Completions normally expects the client to send canonical history. CatGPT
adds an optional `conversation_id` field (or `X-CatGPT-Conversation-ID` header)
to map that logical history to a persistent browser thread:

```json
{
  "model": "catgpt-browser",
  "conversation_id": "sillytavern:character-42:chat-7",
  "messages": [
    {"role": "user", "content": "Continue this story."}
  ]
}
```

When a client sends full history, CatGPT verifies that its stored message hashes
are an exact prefix and forwards only the new suffix. A rewind or changed prefix
starts a fresh browser thread, preventing two unrelated stories from being
mixed. A single new user message with a conversation id is treated as a delta.
The ledger is partitioned by ChatGPT project, app route, and conversation id.

### Stateless fresh browser threads

Clients that already send a complete Chat Completions history on every request
can require a new project-scoped ChatGPT thread for each API call:

```http
X-CatGPT-Thread-Mode: fresh
```

Fresh mode sends the complete current request once, returns the response, and
does not save a conversation route, app-thread mapping, response-cache entry,
or thread-contract entry. The ChatGPT thread remains visible in the configured
project, but CatGPT forgets its thread id after the request. This is useful for
stateless agent clients whose `tools` catalog is request metadata: converting
that catalog into browser text once per fresh thread avoids accumulating a
duplicate catalog on every turn of one browser conversation.

`fresh` cannot be combined with `conversation_id` or `thread_id`. Durable
conversation routing remains the better choice for callers that send true
single-turn deltas.

### Async job

```bash
JOB_ID="$(
  curl -s -X POST http://localhost:8650/v1/chat/completions/async \
    -H "Authorization: Bearer $CATGPT_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"catgpt-browser","messages":[{"role":"user","content":"Write a short checklist."}]}' |
  python -c "import json,sys; print(json.load(sys.stdin)['id'])"
)"

curl -H "Authorization: Bearer $CATGPT_API_KEY" \
  "http://localhost:8650/v1/chat/completions/async/$JOB_ID"
```

### Ollama-compatible clients

Point Ollama-compatible tools at:

```text
http://localhost:8650
```

Common endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/tags` | List chat and embedding model profiles |
| `POST /api/chat` | Chat request |
| `POST /api/generate` | Text generation request |
| `POST /api/embed` | Deterministic compatibility embedding shim |

### App-scoped routes

Use app-scoped paths when several apps share one CatGPT instance:

```text
http://localhost:8650/mealie/v1/chat/completions
http://localhost:8650/linkwarden/v1/chat/completions
http://localhost:8650/immich/v1/chat/completions
```

With `API_APP_THREAD_MODE=true`, CatGPT can keep legacy single-message traffic
from those apps in separate sticky threads. The app path is an application
namespace, not a conversation identity: use `conversation_id`, `conversation`,
or `previous_response_id` when an app has multiple chats. If
`CHATGPT_PROJECT_URL` is configured, new threads are created in that project and
mapped threads are reopened only through project-scoped URLs.

An app-scoped route may still use `X-CatGPT-Thread-Mode: fresh`; fresh mode
takes precedence over the app-level sticky fallback for that request and does
not update the app mapping.

### Useful things to run through CatGPT

| Use case | Why CatGPT helps |
| --- | --- |
| Self-hosted apps needing an OpenAI-compatible URL | Use `base_url=http://catgpt:8000/v1` inside Docker networks. |
| Open WebUI / Ollama-style clients | Use `/api/chat` and `/api/tags` without running a real Ollama model. |
| Structured extraction from files | Send PDFs/images and request JSON output. |
| App-specific assistants | Use app-scoped URLs and app-thread mode to isolate context. |
| Occasional image generation | Use `/v1/images/generations` and let ChatGPT create/download the result. |
| Tool-calling prototypes | Use OpenAI-style tools with browser-backed ChatGPT responses. |

## Troubleshooting

| Problem | Fix |
| --- | --- |
| API says the browser is not initialized | Wait 30-60 seconds after container start, then check `docker logs catgpt --tail 100`. |
| Login expired | Open `http://localhost:6080` and log in again. |
| noVNC opens but ChatGPT is logged out | Log in inside noVNC; the profile persists in the mounted browser volume. |
| Model switch fails | Check `/v1/models`; explicit model requests fail closed if CatGPT cannot confirm the selected picker row. |
| A code change is not visible | Run `docker compose up --build -d`. |
| Browser profile is stuck | Stop the container and remove stale browser lock files from the mounted browser directory. |
| Responses are slow | ChatGPT browser automation is serialized; one request uses the browser at a time. |

CatGPT cannot stream live browser tokens. For compatible clients, `stream=true`
returns an SSE stream after the complete browser response is available.
