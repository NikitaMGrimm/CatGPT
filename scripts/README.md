# Scripts

The root `scripts/test_*.py` files are opt-in integration checks. They can call
a running CatGPT server or interact with a real logged-in browser and are not
collected by the automated unit-test suite.

Run scripts through the locked environment, for example:

```bash
uv run python scripts/test_responses_api.py
```

General setup and login helpers remain at the root of this directory. Narrow,
potentially destructive diagnostics live in `scripts/manual/` and document
their own safeguards.
