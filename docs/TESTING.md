# Testing

CatGPT has two testing layers: deterministic unit tests and opt-in live browser
smoke tests. Live tests use the configured ChatGPT account and can create real
threads, so they are never part of the default test command.

GitHub Actions runs the deterministic suite on Python 3.11 and 3.14 for pushes
to `main` and pull requests. It needs no repository secrets and no signed-in
browser session.

## Automated tests

Install the locked development environment and run the suite:

```bash
uv sync --group dev
uv run python -m unittest discover -s tests
```

The browser-DOM tests use a local headless Patchright browser when its runtime
is installed. Install it once with:

```bash
uv run patchright install chromium
```

Run a focused module while developing:

```bash
uv run python -m unittest tests.test_model_registry
uv run python -m unittest tests.test_conversation_store
```

## Live integration scripts

Files named `scripts/test_*.py` are manual integration checks, not unit tests.
They may require a logged-in browser or a running CatGPT server. Start the
server with:

```bash
uv run python -m src.api.server
```

Then run only the integration script relevant to the change, for example:

```bash
uv run python scripts/test_responses_api.py
uv run python scripts/test_langchain_tools.py
uv run python scripts/test_multi_turn.py
```

## Manual diagnostics

The narrowly scoped browser diagnostics are documented in
`scripts/manual/README.md`. The thread-deletion smoke test requires an explicit
`--confirm-delete` flag because it permanently deletes the test thread it
creates.

The sticky-conversation smoke test is non-destructive but creates one real
ChatGPT thread. It verifies a four-response tool round trip and checks the local
SQLite transcript for duplicate messages.

## Before a pull request

Run:

```bash
uv lock --check
uv sync --frozen --group dev
uv run python -m compileall -q src tests scripts
uv run python -m unittest discover -s tests
git diff --check
```

Live browser tests are optional unless the change touches selectors, login,
thread navigation, model switching, or response detection.
