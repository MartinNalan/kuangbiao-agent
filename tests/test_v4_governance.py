from __future__ import annotations

import unittest

from mining_qa.v4_governance import (
    amendment_parent_title,
    choose_exact_record,
    effective_status_from_official,
    normalize_standard_no,
    parse_nrsis_search,
    parse_samr_search,
    priority_for_quality,
)


class OfficialSourceParserTests(unittest.TestCase):
    def test_nrsis_exact_record_parses_status_and_detail_url(self) -> None:
        html = """
        <table><tr><td>1</td><td>DZ/T 0430-2023</td>
        <td><a href="/portal/stdDetail/233724">固体矿产资源储量核实报告编写规范</a></td>
        <td>2023-04-19</td><td>2023-08-01</td><td>现行</td></tr></table>
        """
        records = parse_nrsis_search(html, "http://example/search")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].effective_status, "current")
        self.assertEqual(records[0].detail_url, "http://www.nrsis.org.cn/portal/stdDetail/233724")

    def test_samr_parser_separates_standard_from_amendment_plan(self) -> None:
        html = """
        <a href="#" tid="BV_GB" pid="standard"><span>GB</span>/<span>T</span> <span>33444-2016</span> 固体矿产勘查工作规范</a>
        <span class="s-status label">现行</span>
        <a href="#" tid="BV_GB_PLAN" pid="plan"><span>GB/T 33444-2016</span> 固体矿产勘查工作规范《第1号修改单》</a>
        <span class="s-status label">已发布</span>
        """
        records = parse_samr_search(html, "https://example/search")
        selected = choose_exact_record(records, "GB/T 33444-2016", "固体矿产勘查工作规范")
        self.assertEqual(len(records), 2)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.record_type, "BV_GB")
        self.assertEqual(selected.effective_status, "current")

    def test_status_and_identity_normalization_are_conservative(self) -> None:
        self.assertEqual(normalize_standard_no("DZ／T 0430—2023"), "DZ/T0430-2023")
        self.assertEqual(effective_status_from_official("废止"), "repealed")
        self.assertEqual(effective_status_from_official("即将实施"), "governance_conflict")


class GovernanceRuleTests(unittest.TestCase):
    def test_amendment_parent_title_is_explicit(self) -> None:
        self.assertEqual(amendment_parent_title("《钒矿地质勘查规范》修改单"), "钒矿地质勘查规范")
        self.assertEqual(
            amendment_parent_title("《固体矿产勘查工作规范》国家标准第1号修改单"),
            "固体矿产勘查工作规范",
        )

    def test_quality_priority_respects_effective_status(self) -> None:
        self.assertEqual(priority_for_quality("empty_source_page", "current"), ("P0", 0))
        self.assertEqual(priority_for_quality("low_ocr_confidence", "current"), ("P1", 1))
        self.assertEqual(priority_for_quality("low_ocr_confidence", "repealed"), ("P2", 2))


if __name__ == "__main__":
    unittest.main()
