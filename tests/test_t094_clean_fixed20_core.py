from __future__ import annotations

from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mining_qa import v4_fixed20_core as clean  # noqa: E402
from mining_qa.agent import MiningQAAgent  # noqa: E402
from mining_qa.knowledge_client import (  # noqa: E402
    T092_DECISION_HANDSHAKE_KEY,
    T092_DECISION_TRACE_KEY,
    T092_DECISION_VERSION,
    build_t092_decision_envelope,
)
from mining_qa.v4_retrieval_store_clean import (  # noqa: E402
    CleanResilientV4KnowledgeStore,
)
from mining_qa.v4_retrieval_store_t094 import (  # noqa: E402
    RUNTIME_ID,
    T094V4KnowledgeStore,
)


def synthetic_row(identifier: str, position: int) -> dict:
    return {
        "retrieval_unit_id": identifier,
        "document_id": f"synthetic-document-{position // 10}",
        "corpus": "synthetic",
        "title": "合成文件",
        "standard_no": "SYNTHETIC-0000",
        "unit_type": "clause",
        "section_path": "合成章节",
        "clause_no": f"1.{position + 1}",
        "citation_text": f"合成内容 {position + 1}",
        "citation_text_sha256": f"synthetic-sha-{position + 1}",
        "search_eligible": True,
        "citation_eligible": True,
        "document_type": "standard",
    }


def synthetic_inputs(*, application_list: bool = False) -> dict:
    base_rows = [synthetic_row(f"synthetic-unit-{index:02d}", index) for index in range(30)]
    row_by_id = {row["retrieval_unit_id"]: row for row in base_rows}
    manifests = {"新立": [], "延续": []}
    question = "普通合成问题"
    if application_list:
        question = "采矿权新立需要提交哪些材料？"
        for number in range(1, 15):
            identifier = f"synthetic-material-{number:02d}"
            row_by_id[identifier] = {
                **synthetic_row(identifier, 100 + number),
                "unit_type": "application_material_row",
                "standard_no": "SYNTHETIC-LIST-0000",
                "section_path": f"合成附件 > 新立 > 材料 {number}",
            }
            manifests["新立"].append({"unit_id": identifier})
    ids = [row["retrieval_unit_id"] for row in base_rows]
    return {
        "question": question,
        "original_candidate_ids": ids,
        "original_candidate_order": ids,
        "row_by_id": row_by_id,
        "child_catalog": {},
        "appendix_catalog": {},
        "rows_by_document": {},
        "list_manifests": manifests,
        "gap_families": [],
        "governed_families": [],
        "classification_catalog": {},
    }


class CleanFixed20CoreTest(unittest.TestCase):
    def test_non_triggering_synthetic_runtime_has_deterministic_fixed20(self) -> None:
        inputs = synthetic_inputs()
        production = clean.generic_fixed20_runner(**inputs)
        final = production["pools"][clean.BASE_POOL_KEY]
        expected = [f"synthetic-unit-{index:02d}" for index in range(20)]
        self.assertEqual(final, expected)
        self.assertEqual(production["reservations"], [])
        self.assertEqual(
            production["candidate_order"],
            [f"synthetic-unit-{index:02d}" for index in range(30)],
        )

    def test_structured_list_reserves_all_synthetic_rows_without_widening_final(self) -> None:
        inputs = synthetic_inputs(application_list=True)
        production = clean.generic_fixed20_runner(**inputs)
        expected_reservations = [
            f"synthetic-material-{number:02d}" for number in range(1, 15)
        ]
        self.assertEqual(production["reservations"], expected_reservations)
        final = production["pools"][clean.BASE_POOL_KEY]
        self.assertEqual(len(final), 20)
        self.assertEqual(len(set(final)), 20)
        self.assertTrue(
            set(expected_reservations).issubset(final)
        )

    def test_store_uses_only_the_clean_store_base(self) -> None:
        self.assertTrue(issubclass(T094V4KnowledgeStore, CleanResilientV4KnowledgeStore))
        modules = {base.__module__ for base in T094V4KnowledgeStore.__mro__}
        self.assertNotIn("mining_qa.v4_retrieval_store", modules)
        self.assertNotIn("mining_qa.v4_retrieval_store_v2", modules)

    def test_missing_decision_envelope_fails_closed_under_t094_identity(self) -> None:
        store = object.__new__(T094V4KnowledgeStore)
        response = store.search({"query": "合成问题"})
        self.assertEqual(response["results"], [])
        handshake = response["retrieval"]["technical_sufficiency_decision_handshake"]
        self.assertEqual(handshake["runtime_id"], RUNTIME_ID)
        self.assertEqual(handshake["reason"], "decision_envelope_required")

    def test_agent_accepts_t092_decision_handshake_from_t094_runtime(self) -> None:
        question = "普通合成问题"
        envelope = build_t092_decision_envelope(question, None)
        handshake = {
            "status": "verified",
            "decision_version": T092_DECISION_VERSION,
            "runtime_id": RUNTIME_ID,
            "decision_status": "not_applicable",
            "decision_sha256": None,
            "transport_sha256": envelope["transport_sha256"],
            "trace_decision_sha256": None,
            "trace_key": T092_DECISION_TRACE_KEY,
        }
        result = SimpleNamespace(
            retrieval={T092_DECISION_HANDSHAKE_KEY: handshake},
            coverage={
                T092_DECISION_HANDSHAKE_KEY: handshake,
                "query_plan": {"structure_traces": {}},
            },
        )

        self.assertIsNone(
            MiningQAAgent._t092_knowledge_integrity_error([result], envelope)
        )

        unknown = dict(handshake)
        unknown["runtime_id"] = "v4-hybrid-fixed20-p1fix-unknown-v1"
        unknown_result = SimpleNamespace(
            retrieval={T092_DECISION_HANDSHAKE_KEY: unknown},
            coverage={
                T092_DECISION_HANDSHAKE_KEY: unknown,
                "query_plan": {"structure_traces": {}},
            },
        )
        self.assertEqual(
            MiningQAAgent._t092_knowledge_integrity_error(
                [unknown_result], envelope
            ),
            "t092_runtime_id_mismatch",
        )

    def test_clean_runtime_sources_embed_no_real_evidence_identifier(self) -> None:
        for relative in (
            "src/mining_qa/v4_retrieval_primitives.py",
            "src/mining_qa/v4_fixed20_core.py",
            "src/mining_qa/v4_retrieval_store_clean.py",
            "src/mining_qa/v4_retrieval_store_t094.py",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotRegex(text, re.compile(r"\b(?:r?unit)-[0-9a-f]{12,}\b"))
            self.assertNotRegex(text, re.compile(r"(?:^|\n)\s*(?:from|import)\s+run_v4_"))


if __name__ == "__main__":
    unittest.main()
