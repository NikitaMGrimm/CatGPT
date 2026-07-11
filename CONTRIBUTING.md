# Contributing to CatGPT Gateway

Thanks for your interest in contributing! This project is open source and we welcome all kinds of contributions: bug fixes, new features, documentation improvements, and new provider integrations.

---

## Getting Started

1. **Fork** the repo on GitHub
2. **Clone** your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/CatGPT.git
   cd CatGPT
   ```
3. **Set up** the development environment:
   ```bash
   uv sync --group dev
   uv run patchright install chromium
   cp .env.example .env
   ```
4. **Create a branch** for your changes:
   ```bash
   git checkout -b feature/your-feature-name
   ```

---

## Development Workflow

### Running Locally

```bash
# Start the API server
uv run python -m src.api.server

# Run tests
uv run python -m unittest discover -s tests
uv run python scripts/test_langchain_tools.py
```

### Testing

Before submitting a PR, run the deterministic checks used by GitHub Actions:

```bash
uv lock --check
uv sync --frozen --group dev
uv run python -m compileall -q src tests scripts
uv run python -m unittest discover -s tests
git diff --check
```

The CI workflow runs the deterministic suite on the oldest and newest supported
Python versions. It deliberately does not install a browser or require a
ChatGPT login.

Run the relevant `scripts/test_*.py` integration check when changing browser
selectors, login, navigation, model switching, response detection, or API
translation. These scripts can create real provider threads and may require a
running API server. See `docs/TESTING.md` for their prerequisites and scope.

---

## What to Contribute

### Broken Selectors

ChatGPT and Claude update their web UIs frequently. When selectors break, they need updating.

- **ChatGPT selectors**: `src/selectors.py`
- **Claude selectors**: `src/claude/selectors.py`

Each selector is a list of CSS selectors tried in order. Add new selectors at the top and keep old ones as fallbacks.

### New Providers

Want to add support for Gemini, Copilot, or another web-based AI? Follow the pattern in `src/claude/`:

1. Create a new directory: `src/your_provider/`
2. Implement `client.py` with `send_message()`, `new_chat()`, and file upload
3. Implement `detector.py` for response completion detection
4. Implement `selectors.py` with the provider's DOM selectors
5. Add the provider option to `src/config.py`
6. Add provider handling to `src/api/openai_routes.py`
7. Add provider handling to `src/api/server.py`

### Bug Fixes

If you find a bug, please open an issue first describing the problem. If you have a fix, feel free to submit a PR directly.

### Documentation

Docs live in `docs/` and the root `README.md`. Improvements, corrections, and additional examples are always welcome.

---

## Code Style

- Python 3.11 through 3.14 compatible
- Use type hints where reasonable
- Keep functions focused and small
- Follow existing patterns in the codebase

---

## Pull Request Guidelines

1. **One feature per PR.** Keep changes focused.
2. **Describe what changed** in the PR description.
3. **Run the deterministic checks** above. Run relevant live checks for browser-facing changes.
4. **Don't commit sensitive data.** No `.env` files, no `browser_data/`, no cookies, no API keys.
5. **Don't break existing functionality.** Run the test suite on at least one provider.

---

## Reporting Issues

When opening an issue, please include:

- Your OS and Python version
- Provider (Claude or ChatGPT)
- Whether you're using Docker or running locally
- The error message or unexpected behavior
- Steps to reproduce

---

## Project Structure

Quick reference for where to find things:

| What | Where |
|---|---|
| API endpoints | `src/api/openai_routes.py`, `src/api/routes.py` |
| OpenAI schemas | `src/api/openai_schemas.py` |
| Durable conversation state | `src/api/conversation_store.py` |
| ChatGPT client | `src/chatgpt/client.py` |
| Dynamic model catalog | `src/chatgpt/model_registry.py` |
| Claude client | `src/claude/client.py` |
| DOM selectors | `src/selectors.py`, `src/claude/selectors.py` |
| Browser management | `src/browser/manager.py` |
| Configuration | `src/config.py` |
| Docker setup | `docker/entrypoint.sh`, `docker/supervisord.conf` |
| Automated tests | `tests/` |
| Live integration checks | `scripts/test_*.py` |
| Manual diagnostics | `scripts/manual/` |
| GitHub automation | `.github/workflows/` |
| Documentation | `docs/` |

---

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
