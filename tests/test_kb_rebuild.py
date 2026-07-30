from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mining_qa.kb_rebuild import (
    clean_display_text,
    extract_measurements,
    init_db,
    normalized_search_text,
    remove_page_artifacts,
    unit_query_aliases,
)


class UnitNormalizationTests(unittest.TestCase):
    def test_equivalent_length_units_normalize_only_in_search_layer(self) -> None:
        raw = "推荐工程间距为80 m，采样线长80米。"
        clean, _ = clean_display_text(raw)
        measurements = extract_measurements(clean)

        self.assertEqual([item["canonical_text"] for item in measurements], ["80米", "80米"])
        self.assertEqual(raw, "推荐工程间距为80 m，采样线长80米。")
        self.assertEqual(clean, "推荐工程间距为80 m,采样线长80米。")
        self.assertEqual(normalized_search_text(clean, measurements), "推荐工程间距为80米,采样线长80米。")

    def test_compound_and_unrelated_units_are_not_misclassified_as_metres(self) -> None:
        text = "体积为3m³，面积为4m2，浓度为5mg/L，长度为6m。"
        units = extract_measurements(text)

        self.assertEqual([item["unit_key"] for item in units], ["volume_cubic_metre", "area_square_metre", "length_metre"])
        self.assertEqual(normalized_search_text(text, units), "体积为3立方米，面积为4平方米，浓度为5mg/L，长度为6米。")

    def test_query_aliases_are_bounded_and_preserve_original_expression(self) -> None:
        self.assertEqual(unit_query_aliases("80 m工程间距"), ["80 m工程间距", "80米工程间距"])
        self.assertEqual(unit_query_aliases("80米工程间距"), ["80米工程间距"])


class CorpusSchemaTests(unittest.TestCase):
    def test_schema_can_be_initialized_without_retrieval_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "corpus.sqlite"
            init_db(db_path)
            self.assertTrue(db_path.exists())

    def test_page_artifact_cleanup_keeps_original_semantic_lines(self) -> None:
        text, removed = remove_page_artifacts("- 843 -\n4.2.1 条款正文\n正文内容\n一")
        self.assertEqual(text, "4.2.1 条款正文\n正文内容")
        self.assertEqual(removed, ["- 843 -", "一"])


if __name__ == "__main__":
    unittest.main()
