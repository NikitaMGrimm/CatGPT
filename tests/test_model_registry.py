from __future__ import annotations

import unittest
from unittest.mock import patch

from src.chatgpt import model_registry


class ModelRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        model_registry.clear_discovered_models()

    def tearDown(self) -> None:
        model_registry.clear_discovered_models()

    def test_default_catalog_is_not_hardcoded(self) -> None:
        with patch.object(model_registry.Config, "CHATGPT_MODEL_ALIASES", ""):
            self.assertEqual(model_registry.list_public_chat_models(), ["catgpt-browser"])

    def test_discovery_exposes_models_and_unique_family_alias(self) -> None:
        model_registry.register_discovered_models(
            ["GPT-5.6 Sol", "GPT-5.5", "GPT-5.4", "GPT-5.3", "o3"]
        )
        with patch.object(model_registry.Config, "CHATGPT_MODEL_ALIASES", ""):
            models = model_registry.list_public_chat_models()
        self.assertTrue(
            {"catgpt-browser", "gpt-5.6-sol", "gpt-5.6", "gpt-5.5", "gpt-5.4", "gpt-5.3", "o3"}
            <= set(models)
        )

    def test_ambiguous_family_has_no_short_alias(self) -> None:
        model_registry.register_discovered_models(["GPT-5.6 Sol", "GPT-5.6 Terra"])
        self.assertNotIn("gpt-5.6", model_registry.list_public_chat_models())

    def test_reasoning_variants_are_generated_from_live_rows(self) -> None:
        model_registry.register_discovered_models(["GPT-5.6 Sol", "o3"])
        model_registry.register_discovered_reasoning(
            "GPT-5.6 Sol", ["Instant 5.5", "Medium", "High"]
        )
        model_registry.register_discovered_reasoning("o3", ["Medium"])
        models = model_registry.list_public_chat_models()
        self.assertIn("gpt-5.6-sol-low", models)
        self.assertIn("gpt-5.6-sol-medium", models)
        self.assertIn("gpt-5.6-sol-high", models)
        self.assertIn("o3-medium", models)
        self.assertNotIn("o3-high", models)

    def test_reasoning_aliases_and_substring_labels(self) -> None:
        self.assertEqual(model_registry.canonical_reasoning_effort("Instant 5.5"), "low")
        self.assertEqual(model_registry.canonical_reasoning_effort("light"), "low")
        self.assertEqual(model_registry.canonical_reasoning_effort("deep"), "high")
        self.assertEqual(model_registry.canonical_reasoning_effort("extra-high"), "xhigh")

    def test_reasoning_clamps_to_visible_range(self) -> None:
        self.assertEqual(
            model_registry.choose_reasoning_label("max", ["Instant 5.5", "Medium", "High"]),
            ("High", "high"),
        )
        self.assertEqual(
            model_registry.choose_reasoning_label("none", ["Medium"]),
            ("Medium", "medium"),
        )
        self.assertEqual(
            model_registry.choose_reasoning_label("high", ["Medium"]),
            ("Medium", "medium"),
        )

    def test_suffix_reasoning_overrides_explicit_field(self) -> None:
        model_registry.register_discovered_models(["GPT-5.6 Sol"])
        resolved = model_registry.resolve_model_request("gpt-5.6-sol-high", "low")
        self.assertEqual(resolved.model.public_id, "gpt-5.6-sol")
        self.assertEqual(resolved.reasoning_effort, "high")
        self.assertTrue(resolved.reasoning_from_model_id)

    def test_base_model_uses_explicit_reasoning(self) -> None:
        model_registry.register_discovered_models(["GPT-5.6 Sol"])
        resolved = model_registry.resolve_model_request("gpt-5.6-sol", "light")
        self.assertEqual(resolved.reasoning_effort, "low")
        self.assertFalse(resolved.reasoning_from_model_id)

    def test_environment_aliases_remain_supported(self) -> None:
        with patch.object(
            model_registry.Config,
            "CHATGPT_MODEL_ALIASES",
            "future-research=Future Research|Future Model",
        ):
            resolved = model_registry.resolve_requested_model("future-research")
        self.assertEqual(resolved.ui_label, "Future Research")
        self.assertEqual(resolved.alternate_labels, ("Future Model",))


if __name__ == "__main__":
    unittest.main()
