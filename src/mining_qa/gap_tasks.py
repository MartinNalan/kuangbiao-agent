import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .config import PROJECT_ROOT
from .domain_gate import DomainDecision
from .schemas import KnowledgeGapTask


class KnowledgeGapTaskStore:
    def __init__(self, path: Path | None = None):
        self.path = path or PROJECT_ROOT / "data" / "knowledge_gap_tasks.jsonl"

    def create(self, question: str, decision: DomainDecision, source_count: int) -> KnowledgeGapTask:
        task = KnowledgeGapTask(
            task_id="kgap_" + uuid4().hex[:12],
            status="queued",
            message="已记录为知识库缺口任务，后台将低优先级补充官方来源和 OCR 候选。",
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "task_id": task.task_id,
            "type": task.type,
            "status": task.status,
            "question": question,
            "detected_domain": decision.reason,
            "detected_keywords": decision.matched_terms,
            "source_count": source_count,
            "priority": "normal",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "processed_at": None,
            "result_candidate_id": None,
        }
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
        return task
