from __future__ import annotations

import unittest

from mining_qa.agent import MiningQAAgent
from mining_qa.query_understanding import understand_query
from mining_qa.schemas import Source


class EngineeringDistanceAnswerTests(unittest.TestCase):
    @staticmethod
    def _source(*, first_row: str) -> Source:
        return Source(
            title="矿产地质勘查规范 岩金",
            standard_no="DZ/T 0205-2020",
            chapter="附录F(资料性附录)",
            quote=(
                "表 F.1 参考基本勘查工程间距\n"
                "控制资源量勘查工程间距/m\n坑探\n钻探\n勘查类型\n"
                "穿脉\n沿脉\n走向\n倾斜\n"
                f"I\n{first_row}\n"
                "II\n40~80\n40~80\n40~80\n40~80\n"
                "III\n20~40\n20~40\n20~40\n20~40\n"
                "注1:勘查工程间距是指沿矿体走向线和倾斜线的实际距离。"
            ),
            source_type="local_kb",
            text_access="ocr_text",
        )

    @staticmethod
    def _answer(source: Source) -> str:
        question = "金矿勘查Ⅰ类型的推荐工程间距是多少？"
        agent = object.__new__(MiningQAAgent)
        return agent._fast_answer(
            question,
            [source],
            understand_query(question),
        ) or ""

    def test_raw_matrix_keeps_both_equal_geometric_dimensions(self) -> None:
        answer = self._answer(
            self._source(first_row="80~160\n80~160\n80~160\n80~160")
        )

        self.assertIn("**沿矿体走向线：80～160 m**", answer)
        self.assertIn("**沿矿体倾斜线：80～160 m**", answer)
        self.assertIn("80～160 m × 80～160 m（走向 × 倾斜）", answer)
        self.assertIn("坑探**：穿脉 80～160 m；沿脉 80～160 m", answer)
        self.assertIn("钻探**：走向 80～160 m；倾斜 80～160 m", answer)

    def test_unequal_strike_and_dip_are_not_merged(self) -> None:
        answer = self._answer(
            self._source(first_row="70~140\n80~150\n90~160\n60~120")
        )

        self.assertIn("**沿矿体走向线：90～160 m**", answer)
        self.assertIn("**沿矿体倾斜线：60～120 m**", answer)
        self.assertNotIn("90～160 m × 90～160 m", answer)


if __name__ == "__main__":
    unittest.main()
