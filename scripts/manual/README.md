# Manual diagnostics

These tools inspect or mutate a live ChatGPT browser session. Stop other CatGPT
processes that use the same browser profile before running them.

## Model picker inspector

```bash
uv run python scripts/manual/inspect_model_picker.py
```

The script prints the visible model-picker structure and writes its screenshot
to `logs/manual/model-picker-open.png`.

## Thread deletion smoke test

```bash
uv run python scripts/manual/thread_deletion_smoke.py --confirm-delete
```

This creates one uniquely named ChatGPT thread and permanently deletes that
same thread. The confirmation flag is deliberately required.

## Sticky conversation smoke test

With a logged-in local CatGPT server running:

```bash
uv run python scripts/manual/sticky_conversation_smoke.py
```

The script creates one real thread, performs a four-response tool-call
conversation through a durable `conversation_id`, and checks the SQLite ledger
for the expected eight-message transcript without duplicated user messages.
