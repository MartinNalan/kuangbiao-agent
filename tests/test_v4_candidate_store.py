from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from mining_qa.v4_candidate_store import V4CandidateStore


class V4CandidateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "candidates.sqlite"
        self.store = V4CandidateStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_candidate_contract_is_preserved_without_v3(self) -> None:
        created = self.store.create_candidate(
            {
                "triggering_question": "待补充问题",
                "standard_no": "DZ/T TEST-1",
                "title": "候选文件",
                "review_status": "candidate_found",
            }
        )
        listed = self.store.candidates()

        self.assertTrue(created["ok"])
        self.assertEqual(listed["pagination"]["total"], 1)
        self.assertEqual(listed["items"][0]["candidate_id"], created["candidate_id"])
        self.assertEqual(listed["items"][0]["title"], "候选文件")

    def test_candidate_state_persists_across_store_instances(self) -> None:
        self.store.create_candidate({"title": "持久化候选"})
        reopened = V4CandidateStore(self.path)
        try:
            self.assertEqual(reopened.health()["candidate_count"], 1)
        finally:
            reopened.close()

    def test_store_contains_no_answer_retrieval_tables(self) -> None:
        with sqlite3.connect(self.path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "select name from sqlite_master where type='table'"
                )
            }
        self.assertEqual(tables, {"candidates"})


if __name__ == "__main__":
    unittest.main()
