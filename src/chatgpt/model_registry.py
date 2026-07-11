"""Dynamic model and reasoning registry for ChatGPT's live model picker."""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.config import Config

PUBLIC_BROWSER_MODEL_ID = "catgpt-browser"
_AUTO_MODEL_IDS = {"", "auto", "default", "browser", PUBLIC_BROWSER_MODEL_ID}
_DYNAMIC_MODEL_ID = re.compile(r"^(?:gpt-[a-z0-9][a-z0-9._-]*|o\d[a-z0-9._-]*)$")
_DISCOVERED_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# OpenAI's public reasoning-effort order. A model may expose only a subset.
REASONING_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_REASONING_ALIASES: dict[str, tuple[str, ...]] = {
    "none": ("none", "off", "disabled", "no reasoning"),
    "minimal": ("minimal", "minimum", "min", "lowest"),
    "low": ("low", "light", "instant", "fast"),
    "medium": ("medium", "med", "balanced", "normal", "standard", "default", "auto"),
    "high": ("high", "deep", "extended", "thinking", "strong"),
    "xhigh": ("xhigh", "x-high", "extra high", "very high", "extreme"),
    "max": ("max", "maximum", "highest"),
}


@dataclass(frozen=True)
class BrowserModelOption:
    """A public API model id paired with the concrete ChatGPT picker label."""

    public_id: str
    ui_label: str
    alternate_labels: tuple[str, ...] = ()

    @property
    def ui_labels(self) -> tuple[str, ...]:
        return (self.ui_label, *self.alternate_labels)


@dataclass(frozen=True)
class ResolvedModelRequest:
    """A concrete model selection plus an optional canonical reasoning effort."""

    model: BrowserModelOption | None
    reasoning_effort: str | None = None
    reasoning_from_model_id: bool = False


_discovered_models: dict[str, BrowserModelOption] = {}
_discovered_reasoning: dict[str, tuple[str, ...]] = {}


def normalize_model_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def model_label_to_public_id(label: str) -> str:
    value = re.sub(r"[^a-z0-9.]+", "-", (label or "").strip().lower()).strip("-")
    return re.sub(r"-{2,}", "-", value)


def public_model_id_to_ui_label(model_id: str) -> str:
    value = (model_id or "").strip().lower()
    if value.startswith("gpt-"):
        parts = [part for part in value[4:].split("-") if part]
        return "GPT-" + (parts[0] if parts else "") + (
            " " + " ".join(part.capitalize() for part in parts[1:]) if len(parts) > 1 else ""
        )
    return value


def is_dynamic_model_id(model: str) -> bool:
    return bool(_DYNAMIC_MODEL_ID.fullmatch((model or "").strip().lower()))


def canonical_reasoning_effort(value: str | None) -> str | None:
    """Map official values, UI labels, and common aliases to one effort name."""
    raw = re.sub(r"[_:.\-]+", " ", (value or "").strip().lower())
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return None

    # Match the most specific aliases first (for example xhigh before high).
    priority = ("xhigh", "max", "none", "minimal", "medium", "high", "low")
    compact = normalize_model_token(raw)
    for effort in priority:
        for alias in _REASONING_ALIASES[effort]:
            alias_compact = normalize_model_token(alias)
            if alias in raw or (alias_compact and alias_compact in compact):
                return effort
    return None


def reasoning_aliases() -> tuple[str, ...]:
    """Return all accepted suffix tokens, longest first."""
    values = {alias for aliases in _REASONING_ALIASES.values() for alias in aliases}
    values.update(REASONING_EFFORT_ORDER)
    return tuple(sorted(values, key=lambda value: (-len(value), value)))


def choose_reasoning_label(requested: str, available_labels: list[str] | tuple[str, ...]) -> tuple[str, str]:
    """Choose the closest visible reasoning row and return ``(label, effort)``.

    Unsupported values never cause a picker error. Requests below/above a
    model's range clamp to the lowest/highest visible effort; gaps select the
    nearest effort, preferring the higher one on an exact tie.
    """
    labels = [label.strip() for label in available_labels if label and label.strip()]
    if not labels:
        return "", ""

    requested_token = normalize_model_token(requested)
    for label in labels:
        label_token = normalize_model_token(label)
        if requested_token and (requested_token in label_token or label_token in requested_token):
            effort = canonical_reasoning_effort(label) or canonical_reasoning_effort(requested) or "medium"
            return label, effort

    requested_effort = canonical_reasoning_effort(requested) or "medium"
    requested_rank = REASONING_EFFORT_ORDER.index(requested_effort)
    candidates: list[tuple[int, bool, int, str, str]] = []
    for position, label in enumerate(labels):
        effort = canonical_reasoning_effort(label)
        if not effort:
            continue
        rank = REASONING_EFFORT_ORDER.index(effort)
        candidates.append((abs(rank - requested_rank), rank < requested_rank, position, label, effort))

    if not candidates:
        return labels[0], requested_effort
    _, _, _, label, effort = min(candidates)
    return label, effort


def register_discovered_models(labels: list[str] | tuple[str, ...]) -> list[BrowserModelOption]:
    registered: list[BrowserModelOption] = []
    for label in labels:
        ui_label = (label or "").strip()
        public_id = model_label_to_public_id(ui_label)
        if not ui_label or not _DISCOVERED_MODEL_ID.fullmatch(public_id):
            continue
        option = BrowserModelOption(public_id=public_id, ui_label=ui_label)
        _discovered_models[normalize_model_token(public_id)] = option
        registered.append(option)
    return registered


def register_discovered_reasoning(model: str, labels: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Cache the live reasoning rows for a concrete model id or UI label."""
    option = _resolve_base_model(model)
    key = normalize_model_token(option.public_id if option else model)
    if not key:
        return ()
    cleaned = tuple(dict.fromkeys(label.strip() for label in labels if label and label.strip()))
    _discovered_reasoning[key] = cleaned
    return cleaned


def list_reasoning_labels(model: str) -> tuple[str, ...]:
    option = _resolve_base_model(model)
    key = normalize_model_token(option.public_id if option else model)
    return _discovered_reasoning.get(key, ())


def clear_discovered_models() -> None:
    _discovered_models.clear()
    _discovered_reasoning.clear()


def _parse_model_aliases(raw: str) -> list[BrowserModelOption]:
    options: list[BrowserModelOption] = []
    seen: set[str] = set()
    for chunk in (raw or "").split(","):
        item = chunk.strip()
        if not item:
            continue
        public_id, labels = item.split("=", 1) if "=" in item else (item, item)
        parsed = tuple(label.strip() for label in labels.split("|") if label.strip())
        normalized = normalize_model_token(public_id)
        if not normalized or not parsed or normalized in seen:
            continue
        options.append(BrowserModelOption(public_id.strip(), parsed[0], parsed[1:]))
        seen.add(normalized)
    return options


def list_switchable_models() -> list[BrowserModelOption]:
    options = _parse_model_aliases(Config.CHATGPT_MODEL_ALIASES)
    seen = {normalize_model_token(option.public_id) for option in options}
    options.extend(option for key, option in _discovered_models.items() if key not in seen)
    return options


def _family_aliases(options: list[BrowserModelOption]) -> dict[str, BrowserModelOption]:
    families: dict[str, list[BrowserModelOption]] = {}
    for option in options:
        match = re.match(r"^(gpt-\d+(?:\.\d+)+)-.+$", option.public_id.lower())
        if match:
            families.setdefault(match.group(1), []).append(option)
    return {family: matches[0] for family, matches in families.items() if len(matches) == 1}


def list_public_chat_models() -> list[str]:
    """Return base ids plus live-discovered ``<model>-<effort>`` variants."""
    options = list_switchable_models()
    aliases = _family_aliases(options)
    model_ids = [PUBLIC_BROWSER_MODEL_ID]
    for option in options:
        model_ids.append(option.public_id)
        model_ids.extend(
            f"{option.public_id}-{effort}"
            for effort in dict.fromkeys(
                canonical_reasoning_effort(label) for label in list_reasoning_labels(option.public_id)
            )
            if effort
        )
    for alias, option in aliases.items():
        model_ids.append(alias)
        model_ids.extend(
            f"{alias}-{effort}"
            for effort in dict.fromkeys(
                canonical_reasoning_effort(label) for label in list_reasoning_labels(option.public_id)
            )
            if effort
        )
    return list(dict.fromkeys(model_ids))


def _find_known_base_model(model: str) -> BrowserModelOption | None:
    normalized = normalize_model_token(model)
    options = list_switchable_models()
    for option in options:
        labels = {normalize_model_token(label) for label in option.ui_labels}
        if normalized in {normalize_model_token(option.public_id), *labels}:
            return option
    family = _family_aliases(options).get((model or "").strip().lower())
    if family:
        return family
    return None


def _resolve_base_model(model: str) -> BrowserModelOption | None:
    known = _find_known_base_model(model)
    if known:
        return known
    requested = (model or "").strip().lower()
    if is_dynamic_model_id(requested):
        return BrowserModelOption(requested, public_model_id_to_ui_label(requested))
    return None


def _split_reasoning_suffix(model: str) -> tuple[str, str | None]:
    value = (model or "").strip()
    lowered = value.lower()
    for alias in reasoning_aliases():
        suffix = re.sub(r"[^a-z0-9]+", "-", alias.lower()).strip("-")
        for separator in ("-", ":"):
            marker = separator + suffix
            if lowered.endswith(marker) and len(value) > len(marker):
                return value[: -len(marker)], canonical_reasoning_effort(alias)
    return value, None


def resolve_model_request(model: str, reasoning_effort: str | None = None) -> ResolvedModelRequest:
    """Resolve a base model and reasoning request; a model suffix wins."""
    normalized = normalize_model_token(model)
    if normalized in {normalize_model_token(value) for value in _AUTO_MODEL_IDS}:
        default_model = (Config.CHATGPT_DEFAULT_MODEL or "").strip()
        if not default_model:
            explicit = canonical_reasoning_effort(reasoning_effort) or (
                (reasoning_effort or "").strip().lower() or None
            )
            return ResolvedModelRequest(None, explicit, False)
        model = default_model

    # A real/discovered model whose name happens to end in an alias wins over
    # suffix parsing. Only split when the full id does not resolve directly.
    known_direct = _find_known_base_model(model)
    direct = known_direct or _resolve_base_model(model)
    explicit = canonical_reasoning_effort(reasoning_effort) or (
        (reasoning_effort or "").strip().lower() or None
    )
    if known_direct:
        return ResolvedModelRequest(known_direct, explicit, False)
    base, embedded_effort = _split_reasoning_suffix(model)
    if embedded_effort and normalize_model_token(base) != normalize_model_token(model):
        base_option = _resolve_base_model(base)
        if base_option:
            return ResolvedModelRequest(base_option, embedded_effort, True)
    return ResolvedModelRequest(direct, explicit, False)


def resolve_requested_model(model: str) -> BrowserModelOption | None:
    """Backward-compatible base-model resolver."""
    return resolve_model_request(model).model


def is_supported_chat_model(model: str) -> bool:
    normalized = normalize_model_token(model)
    if normalized in {normalize_model_token(value) for value in _AUTO_MODEL_IDS}:
        return True
    if _resolve_base_model(model):
        return True
    base, effort = _split_reasoning_suffix(model)
    return bool(effort and _resolve_base_model(base))
