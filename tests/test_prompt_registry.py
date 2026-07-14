from __future__ import annotations

import unittest

from mining_qa.config import Settings
from mining_qa.prompt_registry import PROMPT_REGISTRY_VERSION, prompt_text, registry_manifest


class PromptRegistryTests(unittest.TestCase):
    def test_baseline_is_versioned_and_intent_aware(self) -> None:
        content = prompt_text(
            Settings(PROMPT_REGISTRY_ENABLED=True),
            "answer",
            primary_intent="service_materials",
        )

        self.assertIn(PROMPT_REGISTRY_VERSION, content)
        self.assertIn("办理类型", content)
        self.assertNotIn("校准要求", content)

    def test_calibrated_variant_can_be_limited_to_selected_intents(self) -> None:
        settings = Settings(
            PROMPT_REGISTRY_ENABLED=True,
            PROMPT_CALIBRATION_ENABLED=True,
            PROMPT_CALIBRATION_VARIANT="calibrated",
            PROMPT_CALIBRATION_INTENTS="cross_document_comparison",
        )

        comparison = prompt_text(settings, "research_summary", primary_intent="cross_document_comparison")
        materials = prompt_text(settings, "answer", primary_intent="service_materials")

        self.assertIn("校准要求", comparison)
        self.assertNotIn("校准要求", materials)

    def test_manifest_lists_all_runtime_stages(self) -> None:
        manifest = registry_manifest()

        self.assertEqual(manifest["version"], PROMPT_REGISTRY_VERSION)
        self.assertTrue({"question_resolution", "retrieval_planner", "answer", "research_summary"}.issubset(manifest["stages"]))


if __name__ == "__main__":
    unittest.main()
