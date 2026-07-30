import importlib.util
from pathlib import Path
import sys
import unittest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "audit_v4_retrieval_preprocessing.py"
)
SPEC = importlib.util.spec_from_file_location(
    "audit_v4_retrieval_preprocessing", SCRIPT
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RetrievalPreprocessingAuditTest(unittest.TestCase):
    def test_length_bands_are_soft_risk_bands(self) -> None:
        self.assertEqual(MODULE.length_band(19), "<20")
        self.assertEqual(MODULE.length_band(20), "20-49")
        self.assertEqual(MODULE.length_band(1199), "600-1199")
        self.assertEqual(MODULE.length_band(1200), ">=1200")

    def test_unsafe_gold_boundary_blocks_direct_use(self) -> None:
        case = {
            "id": "QX",
            "family": "test",
            "question": "测试？",
            "required_groups": [["u1"]],
            "unsafe_for_direct_quote": ["u1"],
        }
        units = {
            "u1": {
                "unit_type": "clause",
                "char_length": 80,
                "page_start": 1,
                "page_end": 1,
                "eligible": True,
            }
        }
        result = MODULE.classify_gold_case(case, units)
        self.assertEqual(result["status"], "boundary_blocked")
        self.assertIn("unsafe_boundary", result["issues"])

    def test_table_and_rows_require_parent_link(self) -> None:
        case = {
            "id": "QY",
            "family": "test",
            "question": "材料？",
            "required_groups": [["table", "row"]],
        }
        units = {
            "table": {
                "unit_type": "table",
                "char_length": 1500,
                "page_start": None,
                "page_end": None,
                "eligible": True,
            },
            "row": {
                "unit_type": "application_material_row",
                "char_length": 150,
                "page_start": None,
                "page_end": None,
                "eligible": True,
            },
        }
        result = MODULE.classify_gold_case(case, units)
        self.assertEqual(result["status"], "needs_preprocessing")
        self.assertIn("table_row_parent_link", result["issues"])


if __name__ == "__main__":
    unittest.main()
