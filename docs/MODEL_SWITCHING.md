# ChatGPT Model Switching

CatGPT discovers concrete models from the logged-in account's live ChatGPT
picker. It does not ship a fixed model catalog, so Free, Plus, Pro, Business,
and Enterprise accounts can expose different model ids.

## Quick Use

List the models currently visible to the account:

```bash
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer dummy123"
```

Then pass one of the returned ids:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer dummy123" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<id-from-v1-models>",
    "messages": [{"role": "user", "content": "Reply with one sentence."}]
  }'
```

## Discovery and naming

CatGPT opens the composer picker, follows its structural concrete-model submenu,
and reads its visible `menuitemradio` rows. Labels are converted to lowercase
API-style slugs: spaces and punctuation become hyphens while dotted versions
are preserved. A label shaped like `GPT-X.Y Variant` therefore becomes
`gpt-x.y-variant`.

Selection is confirmed by reopening the concrete submenu and checking that the
requested row has `aria-checked="true"` or `data-state="checked"`. The visible
composer pill is used only to find the picker; intelligence labels such as
`High`, `Medium`, or `Instant` are not treated as proof of the concrete model.

When a base GPT family id has one discovered suffixed variant, CatGPT can match
that unique variant automatically. If several variants share the same base,
use the concrete id returned by `/v1/models` or configure an explicit alias.

## Configuration

`catgpt-browser`, `auto`, `default`, and `browser` keep the current selection
unless `CHATGPT_DEFAULT_MODEL` is set to a model id returned by `/v1/models`.

`CHATGPT_MODEL_ALIASES` is an optional override for unusual or legacy layouts:

```text
public_id=Primary UI Label|Alternate UI Label|Another Alternate
```

It is empty by default. Reasoning/intelligence rows are discovered per model
from the same nested picker; there is no model-specific settings catalog.

Chat Completions accepts the official `reasoning_effort` field. Responses
accepts `reasoning: {"effort": "..."}`. `/v1/models` also lists generated
`<base-model>-<effort>` ids for every live-discovered combination. A suffix in
the model id wins over an explicit field. Aliases such as `instant`, `light`,
`standard`, `deep`, and `maximum` are normalized, and requests outside a
model's visible range clamp to its nearest available row.

By default, an unavailable requested model logs the visible options and keeps
the current browser model. To fail the request instead:

```env
CHATGPT_MODEL_SWITCH_STRICT=true
```

`CHATGPT_MODEL_SWITCH_TIMEOUT` is measured in milliseconds:

```env
CHATGPT_MODEL_SWITCH_TIMEOUT=10000
```

## Notes

- Model availability depends on the logged-in ChatGPT account and plan.
- `/v1/models` refreshes live discovery before returning cached model ids.
- Selection depends only on the current nested menu roles, ARIA linkage, and
  checked radio state; obsolete Configure-layout heuristics are not used.
- The CLI supports `/model <name>` for ids returned by `/v1/models`.
