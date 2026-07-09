from uuid import uuid4

from .config import Settings
from .knowledge_client import KnowledgeClient
from .llm_client import LLMClient
from .schemas import AskRequest, AskResponse, Limitations, RetrievalStats, Source


SYSTEM_PROMPT = """你是矿产资源标准知识问答 agent。

必须遵守：
1. 只根据给定证据回答标准条款级问题。
2. 如果没有条款级证据，必须明确说明证据不足，不能编造标准条文。
3. 回答优先包含：结论、依据标准名称、标准号、条款、原文片段、适用条件、不确定性。
4. 不要输出大段标准全文；只输出必要引用和摘要。
5. 如果来源存在限制或冲突，必须提示。
"""


class MiningQAAgent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.knowledge = KnowledgeClient(settings)
        self.llm = LLMClient(settings)

    async def ask(self, request: AskRequest) -> AskResponse:
        session_id = request.session_id or str(uuid4())
        filters = request.filters.model_dump(exclude_none=True)
        kb_result = await self.knowledge.search(request.question, filters)

        sources = [self._source_from_hit(hit) for hit in kb_result.results]
        retrieval = RetrievalStats(
            full_text_hits=kb_result.retrieval.get("full_text_hits", 0),
            vector_hits=kb_result.retrieval.get("vector_hits", 0),
            graph_hits=kb_result.retrieval.get("graph_hits", 0),
            web_hits=kb_result.retrieval.get("web_hits", 0),
        )
        has_clause_evidence = bool(kb_result.coverage.get("has_clause_level_evidence", False))
        notes = list(kb_result.coverage.get("notes", []))
        if kb_result.coverage.get("needs_web_supplement"):
            notes.append("本地知识库证据不足，建议补充官方元数据、全文入口或 OCR 任务。")

        limitations = Limitations(has_clause_level_evidence=has_clause_evidence, notes=notes)

        if not has_clause_evidence:
            return AskResponse(
                answer=self._insufficient_answer(request.question, notes),
                session_id=session_id,
                sources=sources,
                retrieval=retrieval,
                limitations=limitations,
                confidence="low",
            )

        answer = await self.llm.complete(self._messages(request.question, sources, limitations))
        return AskResponse(
            answer=answer,
            session_id=session_id,
            sources=sources,
            retrieval=retrieval,
            limitations=limitations,
            confidence="medium" if sources else "low",
        )

    def _messages(self, question: str, sources: list[Source], limitations: Limitations) -> list[dict[str, str]]:
        evidence_lines = []
        for index, source in enumerate(sources, start=1):
            evidence_lines.append(
                "\n".join(
                    [
                        f"[{index}] {source.title}",
                        f"标准号: {source.standard_no or '未知'}",
                        f"章节/条款: {source.chapter or '未知'}",
                        f"页码: {source.page if source.page is not None else '未知'}",
                        f"来源类型: {source.source_type}",
                        f"正文访问: {source.text_access}",
                        f"原文片段: {source.quote or '无'}",
                    ]
                )
            )

        user_content = "\n\n".join(
            [
                f"用户问题：{question}",
                "证据：",
                "\n\n".join(evidence_lines) if evidence_lines else "无",
                "限制：",
                "\n".join(limitations.notes) if limitations.notes else "无",
            ]
        )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    def _source_from_hit(self, hit: dict) -> Source:
        return Source(
            title=hit.get("title") or "未知文件",
            standard_no=hit.get("standard_no"),
            chapter=hit.get("clause_no") or hit.get("section_path") or hit.get("chapter"),
            page=hit.get("page") or hit.get("page_start"),
            quote=hit.get("quote") or hit.get("text"),
            score=hit.get("score"),
            source_type=hit.get("source_type", "unavailable"),
            text_access=hit.get("text_access", "unavailable"),
            url=hit.get("url") or hit.get("source_url"),
            validation_status=hit.get("validation_status"),
        )

    def _insufficient_answer(self, question: str, notes: list[str]) -> str:
        details = "\n".join(f"- {note}" for note in notes) if notes else "- 当前没有可用条款级证据。"
        return (
            "当前不能给出条款级结论。\n\n"
            f"问题：{question}\n\n"
            "原因：\n"
            f"{details}\n\n"
            "建议：先确认知识库是否已入库相关标准；如果只找到官方元数据或图片型正文，"
            "需要补充 OCR/解析后再生成可引用答案。"
        )
