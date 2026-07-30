from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mining_qa.v4_source_audit import (
    identity_present,
    is_category_divider,
    open_sqlite_readonly,
    physical_page_class,
    sha256_text,
)


class V4SourceAuditTests(unittest.TestCase):
    def test_category_and_blank_page_classification_are_explicit(self) -> None:
        metrics = {"dark_pixel_ratio_lt_230": 0.0}
        self.assertEqual(physical_page_class("四、非金属矿产类", metrics), "category_divider")
        self.assertEqual(physical_page_class("", metrics), "blank_page")
        self.assertTrue(is_category_divider("二、能源矿产类"))

    def test_identity_matching_normalizes_standard_punctuation(self) -> None:
        text = "中华人民共和国地质矿产行业标准 DZ/T 0340—2020 矿产勘查矿石加工选冶技术性能试验研究程度要求"
        self.assertTrue(identity_present(text, "DZ/T 0340-2020", "矿产勘查矿石加工选冶技术性能试验研究程度要求"))

    def test_readonly_connection_rejects_writes(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.sqlite"
            with sqlite3.connect(path) as connection:
                connection.execute("create table sample(value text)")
            with open_sqlite_readonly(path) as connection:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("insert into sample values ('x')")

    def test_text_hash_is_deterministic(self) -> None:
        self.assertEqual(sha256_text("同一文本"), sha256_text("同一文本"))


if __name__ == "__main__":
    unittest.main()
