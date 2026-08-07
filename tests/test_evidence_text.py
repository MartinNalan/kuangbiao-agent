from __future__ import annotations

import unittest

from mining_qa.evidence_text import (
    contains_evidence_anchor_group,
    extract_evidence_by_anchor_sequences,
)


class EvidenceTextTests(unittest.TestCase):
    def test_matches_a_synthetic_short_anchor_group(self) -> None:
        text = "甲机关处理本级测试许可证对应的评审备案。"

        self.assertTrue(
            contains_evidence_anchor_group(
                text,
                (("甲机关", "本级", "许可证", "评审备案"),),
            )
        )
        self.assertFalse(
            contains_evidence_anchor_group(
                text,
                (("乙机关", "本级", "许可证"),),
            )
        )

    def test_extracts_one_synthetic_sentence_without_stored_source_wording(self) -> None:
        text = (
            "前置测试说明。"
            "甲机关处理本级测试许可证对应的评审备案，省级乙机关负责其余测试事项。"
            "后续测试说明。"
        )

        selected = extract_evidence_by_anchor_sequences(
            text,
            (
                (("甲机关", "本级", "许可证", "评审备案", "省级"),),
            ),
        )

        self.assertEqual(
            selected,
            "甲机关处理本级测试许可证对应的评审备案，省级乙机关负责其余测试事项。",
        )

    def test_extracts_only_an_adjacent_synthetic_evidence_chain(self) -> None:
        text = (
            "前置测试说明。"
            "测试权转换应核对已备案的测试报告。"
            "大型样本采用甲级，其他样本采用乙级。"
            "后续测试说明。"
        )

        selected = extract_evidence_by_anchor_sequences(
            text,
            (
                (
                    ("测试权转换", "备案", "测试报告"),
                    ("大型样本", "甲级", "其他样本", "乙级"),
                ),
            ),
        )

        self.assertEqual(
            selected,
            "测试权转换应核对已备案的测试报告。大型样本采用甲级，其他样本采用乙级。",
        )

    def test_does_not_join_nonadjacent_synthetic_sentences(self) -> None:
        text = "测试权转换应核对备案报告。中间无关说明。大型样本采用甲级。"

        selected = extract_evidence_by_anchor_sequences(
            text,
            (
                (("测试权转换", "备案报告"), ("大型样本", "甲级")),
            ),
        )

        self.assertIsNone(selected)


if __name__ == "__main__":
    unittest.main()
