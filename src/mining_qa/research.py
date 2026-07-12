from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, dataclass, replace
from typing import Any

from .auth import get_account_store
from .config import Settings, get_settings
from .knowledge_client import KnowledgeClient
from .llm_client import LLMClient
from .query_understanding import QueryPlan, normalize_user_query, understand_query
from .schemas import (
    Limitations,
    QuotaInfo,
    ResearchCoverage,
    ResearchProgress,
    ResearchResult,
    ResearchTaskResponse,
    Source,
)
from .usage_log import UsageLogger


logger = logging.getLogger(__name__)

RESEARCH_CLASSIFICATIONS = {
    "consistent",
    "stricter",
    "looser",
    "equivalent_wording",
    "scope_differs",
    "special_provision",
    "not_covered",
    "insufficient_evidence",
    "possible_conflict",
}

CLASSIFICATION_LABELS = {
    "consistent": "一致",
    "stricter": "更严格",
    "looser": "更宽松",
    "equivalent_wording": "表述不同但实质等价",
    "scope_differs": "适用范围不同",
    "special_provision": "特别规定",
    "not_covered": "未发现直接规定",
    "insufficient_evidence": "证据不足",
    "possible_conflict": "疑似冲突，需人工复核",
}


@dataclass(frozen=True)
class ResearchPlan:
    canonical_question: str
    anchor_titles: tuple[str, ...] = ()
    anchor_standard_numbers: tuple[str, ...] = ()
    corpus_title_terms: tuple[str, ...] = ()
    corpus_standard_numbers: tuple[str, ...] = ()
    document_types: tuple[str, ...] = (
        "standard",
        "national_standard",
        "industry_standard",
        "policy_document",
        "law",
        "regulation",
        "department_rule",
        "guidance",
    )
    comparison_dimensions: tuple[str, ...] = ()
    evidence_queries: tuple[str, ...] = ()
    required_evidence_groups: tuple[tuple[str, ...], ...] = ()
    scope_note: str = ""
    planner_used: bool = False

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def _clean_list(value: object, *, limit: int, item_limit: int = 120) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items: list[str] = []
    for raw in value:
        item = normalize_user_query(str(raw or ""))[:item_limit]
        if item and item not in items:
            items.append(item)
        if len(items) >= limit:
            break
    return tuple(items)


def _clean_groups(value: object) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        return ()
    groups: list[tuple[str, ...]] = []
    for raw in value[:8]:
        group = _clean_list(raw, limit=10, item_limit=80)
        if group:
            groups.append(group)
    return tuple(groups)


def _explicit_titles(question: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in re.findall(r"《([^》]{2,80})》", question) if value.strip()))


def _projection_focus(question: str) -> tuple[str, tuple[str, ...]] | None:
    if "无限外推" in question:
        return (
            "无限外推 见矿工程向外无工程控制 见矿工程外无控制工程 边缘见矿工程外",
            (
                "无限外推",
                "见矿工程向外再没有工程控制",
                "见矿工程向外无工程控制",
                "见矿工程外无控制工程",
                "边缘见矿工程外",
                "边缘见矿工程向外",
            ),
        )
    if "有限外推" in question:
        return (
            "有限外推 相邻工程一个见矿一个不见矿 相邻工程未见矿",
            (
                "有限外推",
                "相邻工程一个见矿",
                "相邻的两个工程一个见矿",
                "相邻工程未见矿",
            ),
        )
    return None


class ResearchPlanner:
    def __init__(self, settings: Settings, llm: LLMClient):
        self.settings = settings
        self.llm = llm

    async def plan(self, question: str) -> ResearchPlan:
        fallback = self._fallback(question)
        if not self.llm.enabled:
            return fallback
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 geowiki 深度研究规划器。只制定本地私有知识库检索计划，不回答问题，"
                    "不使用互联网，不把模型记忆当作标准证据。识别基准文件、候选文件集合的标题模式、"
                    "文件类型、比较维度和最多3条证据查询。候选集合必须能够从标准目录枚举，"
                    "例如‘各分矿种规范’应使用‘矿产地质勘查规范’作为 corpus_title_terms，"
                    "不能只列出你记得的几个标准号。required_evidence_groups 组间为 AND、组内为 OR，"
                    "用于排除只共享普通关键词但没有目标关系的条款。明确区分基准文件和待审查文件。只返回 JSON。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        "deterministic_fallback": fallback.to_payload(),
                        "output_schema": {
                            "canonical_question": "专业且完整的问题表达",
                            "anchor_titles": ["用户指定的基准文件名称"],
                            "anchor_standard_numbers": [],
                            "corpus_title_terms": ["用于目录枚举的标题共同部分"],
                            "corpus_standard_numbers": [],
                            "document_types": ["industry_standard"],
                            "comparison_dimensions": ["比较维度"],
                            "evidence_queries": ["用于每份候选文件内部检索的查询"],
                            "required_evidence_groups": [["每组至少命中一个术语，组间为AND关系"]],
                            "scope_note": "候选范围说明",
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            raw = await self.llm.complete_json(
                messages,
                max_tokens=self.settings.research_planner_max_tokens,
            )
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                return fallback
        except Exception:
            return fallback

        allowed_types = {
            "standard",
            "national_standard",
            "industry_standard",
            "policy_document",
            "law",
            "regulation",
            "department_rule",
            "guidance",
            "service_guide",
            "administrative_service_guide",
            "policy_attachment",
        }
        document_types = tuple(
            value
            for value in _clean_list(payload.get("document_types"), limit=12)
            if value in allowed_types
        )
        plan = ResearchPlan(
            canonical_question=normalize_user_query(
                str(payload.get("canonical_question") or fallback.canonical_question)
            )[:600],
            anchor_titles=_clean_list(payload.get("anchor_titles"), limit=8) or fallback.anchor_titles,
            anchor_standard_numbers=_clean_list(payload.get("anchor_standard_numbers"), limit=8)
            or fallback.anchor_standard_numbers,
            corpus_title_terms=_clean_list(payload.get("corpus_title_terms"), limit=12)
            or fallback.corpus_title_terms,
            corpus_standard_numbers=_clean_list(payload.get("corpus_standard_numbers"), limit=20)
            or fallback.corpus_standard_numbers,
            document_types=document_types or fallback.document_types,
            comparison_dimensions=_clean_list(payload.get("comparison_dimensions"), limit=10)
            or fallback.comparison_dimensions,
            evidence_queries=_clean_list(payload.get("evidence_queries"), limit=3, item_limit=500)
            or fallback.evidence_queries,
            required_evidence_groups=_clean_groups(payload.get("required_evidence_groups"))
            or fallback.required_evidence_groups,
            scope_note=normalize_user_query(str(payload.get("scope_note") or fallback.scope_note))[:400],
            planner_used=True,
        )
        return self._enforce_protected_scope(question, plan)

    @staticmethod
    def _enforce_protected_scope(question: str, plan: ResearchPlan) -> ResearchPlan:
        focus = _projection_focus(question)
        if not focus:
            return plan
        focus_query, focus_group = focus
        groups = list(plan.required_evidence_groups)
        if focus_group not in groups:
            groups.append(focus_group)
        queries = list(plan.evidence_queries)
        if focus_query not in queries:
            queries.append(focus_query)
        return replace(
            plan,
            evidence_queries=tuple(dict.fromkeys(queries))[:3],
            required_evidence_groups=tuple(groups),
        )

    @staticmethod
    def _fallback(question: str) -> ResearchPlan:
        base = understand_query(question)
        title_terms: list[str] = []
        document_types = list(base.document_types)
        if any(term in question for term in ("分矿种规范", "单矿种规范", "各矿种规范", "矿种勘查规范")):
            title_terms.append("矿产地质勘查规范")
            document_types = ["standard", "national_standard", "industry_standard"]
        if "外推" in question:
            title_terms.extend(["矿产地质勘查规范", "固体矿产资源量估算规程"])
            document_types = ["standard", "national_standard", "industry_standard"]
        if not title_terms:
            title_terms.extend(base.candidate_title_terms)
        if not document_types:
            document_types = [
                "standard",
                "national_standard",
                "industry_standard",
                "policy_document",
                "law",
                "regulation",
                "department_rule",
                "guidance",
            ]
        dimensions = base.comparison_dimensions or (
            "适用范围",
            "条件和前提",
            "具体技术要求或数值",
            "例外和特别规定",
        )
        evidence_queries = (base.retrieval_query or base.normalized_query,)
        required_evidence_groups: tuple[tuple[str, ...], ...] = ()
        if "选冶" in question or "加工技术性能试验" in question:
            dimensions = (
                "勘查阶段对应的试验研究程度",
                "可选性、实验室流程、扩大连续、半工业和工业试验要求",
                "难选矿石和特殊矿石的加严条件",
                "例外和特别规定",
            )
            evidence_queries = (
                "矿石加工选冶技术性能试验研究程度 普查 详查 勘探 可选性试验 实验室流程试验",
                "扩大连续试验 半工业试验 工业试验 难选矿石 特殊矿石",
            )
            required_evidence_groups = (
                ("选冶", "加工技术性能试验", "加工选冶试验"),
                ("普查", "详查", "勘探", "试验研究程度", "可选性试验", "流程试验"),
            )
        elif "外推" in question:
            dimensions = ("外推类型", "所依据的工程间距", "尖推和平推比例", "适用条件和例外")
            evidence_queries = (
                "矿体有限外推 无限外推 工程间距 基本工程间距 实际工程间距 经验工程间距",
                "1/2尖推 1/4平推 2/3尖推 1/3平推",
            )
            required_evidence_groups = (
                ("外推", "尖推", "平推", "尖灭"),
                ("工程间距", "基本间距", "实际间距", "经验工程间距"),
                ("1/2", "1/4", "2/3", "1/3", "二分之一", "四分之一"),
            )
            focus = _projection_focus(question)
            if focus:
                focus_query, focus_group = focus
                evidence_queries = tuple(dict.fromkeys((*evidence_queries, focus_query)))[:3]
                required_evidence_groups = (*required_evidence_groups, focus_group)
        return ResearchPlan(
            canonical_question=base.normalized_query,
            anchor_titles=_explicit_titles(question),
            anchor_standard_numbers=tuple(
                number for number in base.standard_numbers if number in question
            ),
            corpus_title_terms=tuple(dict.fromkeys(title_terms)),
            corpus_standard_numbers=base.standard_numbers if not title_terms else (),
            document_types=tuple(dict.fromkeys(document_types)),
            comparison_dimensions=tuple(dimensions),
            evidence_queries=evidence_queries,
            required_evidence_groups=required_evidence_groups,
            scope_note="按知识库目录中的受控文件范围逐份检索。",
            planner_used=False,
        )


class ResearchAnalyzer:
    def __init__(self, settings: Settings, llm: LLMClient):
        self.settings = settings
        self.llm = llm

    async def analyze_batch(
        self,
        question: str,
        plan: ResearchPlan,
        indexed_sources: list[tuple[int, Source, str]],
        *,
        allow_split: bool = True,
    ) -> list[dict[str, Any]]:
        if not indexed_sources:
            return []
        if not self.llm.enabled:
            return self._fallback_facts(indexed_sources)
        evidence = [
            {
                "source_index": index,
                "document_id": document_id,
                "title": source.title,
                "standard_no": source.standard_no,
                "clause": source.chapter,
                "quote": source.quote,
            }
            for index, source, document_id in indexed_sources
        ]
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 geowiki 深度研究的证据事实抽取器。只能根据给定条款提取事实，"
                    "不能补充模型常识。每项事实必须引用 source_indices；没有直接证据时不要生成结论。"
                    "比较分类只能使用 consistent、stricter、looser、equivalent_wording、scope_differs、"
                    "special_provision、not_covered、insufficient_evidence、possible_conflict。"
                    "possible_conflict 只表示需要人工复核，不能直接断言法律冲突。"
                    "不能因为某个给定片段没有写到某项内容，就推断整份文件未规定或未提及；"
                    "not_covered 和 insufficient_evidence 由检索覆盖层判断，不在有直接引文的事实中使用。"
                    "每份文档最多提取3项事实，每项 finding 不超过180个汉字；严格区分有限外推和无限外推。"
                    "只返回 JSON。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        "anchor_titles": plan.anchor_titles,
                        "comparison_dimensions": plan.comparison_dimensions,
                        "evidence": evidence,
                        "output_schema": {
                            "facts": [
                                {
                                    "document_id": "文档ID",
                                    "classification": "consistent",
                                    "dimension": "比较维度",
                                    "finding": "由引文直接支持的具体差异或要求",
                                    "source_indices": [1],
                                }
                            ]
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        values = None
        for attempt in range(2):
            try:
                raw = await self.llm.complete_json(
                    messages,
                    max_tokens=self.settings.research_analysis_max_tokens,
                )
                payload = json.loads(raw)
                values = payload.get("facts") if isinstance(payload, dict) else None
                if isinstance(values, list):
                    break
            except Exception as error:
                if attempt == 1:
                    logger.warning("Research fact extraction failed: %s", type(error).__name__)
        if not isinstance(values, list):
            return await self._split_or_fallback(question, plan, indexed_sources, allow_split)

        valid_indices = {index for index, _, _ in indexed_sources}
        document_by_index = {index: document_id for index, _, document_id in indexed_sources}
        facts: list[dict[str, Any]] = []
        for value in values[:40]:
            if not isinstance(value, dict):
                continue
            classification = str(value.get("classification") or "").strip()
            if classification not in RESEARCH_CLASSIFICATIONS:
                continue
            if classification in {"not_covered", "insufficient_evidence"}:
                continue
            source_indices: list[int] = []
            for raw_index in value.get("source_indices") or []:
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    continue
                if index in valid_indices and index not in source_indices:
                    source_indices.append(index)
            cited_documents = {document_by_index[index] for index in source_indices}
            if not source_indices or len(cited_documents) != 1:
                continue
            document_id = next(iter(cited_documents))
            finding = _strip_out_of_scope_projection(
                _strip_unsupported_absence(
                    normalize_user_query(str(value.get("finding") or ""))[:700]
                ),
                question,
            )
            if not finding:
                continue
            if not (plan.anchor_titles or plan.anchor_standard_numbers):
                if classification == "consistent":
                    classification = "equivalent_wording"
                elif classification in {"stricter", "looser"}:
                    classification = "special_provision"
            facts.append(
                {
                    "document_id": document_id,
                    "classification": classification,
                    "dimension": normalize_user_query(str(value.get("dimension") or ""))[:160],
                    "finding": finding,
                    "source_indices": list(dict.fromkeys(source_indices))[:5],
                }
            )
        if facts:
            return facts
        return await self._split_or_fallback(question, plan, indexed_sources, allow_split)

    async def _split_or_fallback(
        self,
        question: str,
        plan: ResearchPlan,
        indexed_sources: list[tuple[int, Source, str]],
        allow_split: bool,
    ) -> list[dict[str, Any]]:
        if allow_split and len(indexed_sources) > 1:
            midpoint = len(indexed_sources) // 2
            left = await self.analyze_batch(
                question,
                plan,
                indexed_sources[:midpoint],
                allow_split=False,
            )
            right = await self.analyze_batch(
                question,
                plan,
                indexed_sources[midpoint:],
                allow_split=False,
            )
            return [*left, *right]
        return self._fallback_facts(indexed_sources)

    @staticmethod
    def _fallback_facts(indexed_sources: list[tuple[int, Source, str]]) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        for index, source, document_id in indexed_sources:
            quote = normalize_user_query(source.quote or "")[:500]
            if not quote:
                continue
            if any(
                term in quote
                for term in (
                    "无限外推",
                    "见矿工程向外再没有工程控制",
                    "见矿工程外无控制工程",
                    "边缘见矿工程外",
                )
            ):
                dimension = "无限外推规则"
            elif any(term in quote for term in ("有限外推", "相邻工程一个见矿", "相邻的两个工程一个见矿")):
                dimension = "有限外推规则"
            else:
                dimension = "直接条款"
            facts.append(
                {
                    "document_id": document_id,
                    "classification": "special_provision",
                    "dimension": dimension,
                    "finding": quote,
                    "source_indices": [index],
                }
            )
        return facts


def _source_from_hit(hit: dict[str, Any]) -> Source:
    return Source(
        title=hit.get("title") or "未知文件",
        standard_no=hit.get("standard_no"),
        chapter=hit.get("clause_no") or hit.get("section_path"),
        page=hit.get("page") or hit.get("page_start"),
        quote=hit.get("quote") or hit.get("evidence_text") or hit.get("text"),
        score=hit.get("score"),
        source_type=hit.get("source_type", "unavailable"),
        text_access=hit.get("text_access", "unavailable"),
        url=hit.get("url") or hit.get("source_url"),
        source_platform=hit.get("source_platform"),
        source_role=hit.get("source_role"),
        validation_status=hit.get("validation_status"),
    )


def _markdown_cell(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().replace("|", "\\|")


UNSUPPORTED_ABSENCE_PATTERN = re.compile(
    r"未提及|未规定|未说明|没有规定|没有提及|不包含|未采用|未给出"
)


def _strip_unsupported_absence(value: str) -> str:
    parts = re.split(r"([，,；;。])", value)
    kept: list[str] = []
    for index in range(0, len(parts), 2):
        clause = parts[index].strip()
        separator = parts[index + 1] if index + 1 < len(parts) else ""
        if not clause or UNSUPPORTED_ABSENCE_PATTERN.search(clause):
            continue
        clause = re.sub(r"^(?:仅|只)(?=规定|允许|采用|平推|尖推)", "", clause)
        kept.append(clause + separator)
    return "".join(kept).strip("，,；;。 ")


def _strip_out_of_scope_projection(value: str, question: str) -> str:
    opposite = None
    if "无限外推" in question:
        opposite = "有限外推"
    elif "有限外推" in question:
        opposite = "无限外推"
    if not opposite or opposite not in value:
        return value

    parts = re.split(r"([，,；;。])", value)
    kept: list[str] = []
    for index in range(0, len(parts), 2):
        clause = parts[index].strip()
        separator = parts[index + 1] if index + 1 < len(parts) else ""
        if not clause:
            continue
        if opposite in clause:
            clause = clause.split(opposite, 1)[0].rstrip("但而与和及、 ")
        if clause:
            kept.append(clause + separator)
    return "".join(kept).strip("，,；;。 ")


class ResearchTaskRunner:
    def __init__(self) -> None:
        self._running: dict[str, asyncio.Task[None]] = {}
        self._global_semaphore: asyncio.Semaphore | None = None
        self._usage = UsageLogger()

    def schedule(self, task_id: str) -> None:
        current = self._running.get(task_id)
        if current and not current.done():
            return
        task = asyncio.create_task(self._run_guarded(task_id), name=f"research:{task_id}")
        self._running[task_id] = task
        task.add_done_callback(lambda _: self._running.pop(task_id, None))

    async def _run_guarded(self, task_id: str) -> None:
        settings = get_settings()
        if self._global_semaphore is None:
            self._global_semaphore = asyncio.Semaphore(settings.research_global_concurrency)
        async with self._global_semaphore:
            await self._run(task_id, settings)

    async def _run(self, task_id: str, settings: Settings) -> None:
        store = get_account_store(settings)
        llm = LLMClient(settings)
        knowledge = KnowledgeClient(settings)
        try:
            task = store.get_research_task_internal(task_id)
            if task["status"] == "cancelled":
                return
            if not knowledge.enabled:
                raise RuntimeError("knowledge base is not configured")

            store.update_research_task(
                task_id,
                status="planning",
                percent=5,
                message="正在识别基准文件、候选范围和比较维度。",
            )
            plan = await ResearchPlanner(settings, llm).plan(task["retrieval_question"])
            store.update_research_task(
                task_id,
                status="retrieving",
                percent=15,
                message="正在从知识库目录枚举候选文件。",
                plan=plan.to_payload(),
            )
            corpus = await knowledge.research_corpus(
                {
                    "title_terms": list(dict.fromkeys((*plan.corpus_title_terms, *plan.anchor_titles))),
                    "standard_numbers": list(
                        dict.fromkeys((*plan.corpus_standard_numbers, *plan.anchor_standard_numbers))
                    ),
                    "document_types": list(plan.document_types),
                    "limit": settings.research_max_documents,
                }
            )
            documents = list(corpus.get("items") or [])
            total_documents = int(corpus.get("total") or len(documents))
            candidate_truncated = bool(corpus.get("truncated"))
            snapshot = corpus.get("knowledge_snapshot")
            store.update_research_task(
                task_id,
                status="retrieving",
                percent=20,
                message=f"已枚举 {total_documents} 份候选文件，开始逐份检索。",
                total_documents=total_documents,
                examined_documents=0,
                evidence_documents=0,
            )
            if not documents:
                await self._finish_insufficient(
                    store,
                    task,
                    plan,
                    snapshot,
                    total_documents,
                    candidate_truncated,
                    "知识库目录未枚举到符合研究范围的可问答文件。",
                    settings,
                )
                return

            sources_by_document, failed_documents = await self._retrieve_documents(
                store,
                task_id,
                task,
                plan,
                documents,
                total_documents,
                knowledge,
                settings,
            )
            sources: list[Source] = []
            source_documents: list[str] = []
            seen_sources: set[tuple[str, str, str]] = set()
            for document in documents:
                document_id = str(document.get("document_id") or "")
                for source in sources_by_document.get(document_id, []):
                    key = (document_id, source.chapter or "", source.quote or "")
                    if key in seen_sources:
                        continue
                    seen_sources.add(key)
                    sources.append(source)
                    source_documents.append(document_id)
                    if len(sources) >= 30:
                        break
                if len(sources) >= 30:
                    break
            if not sources:
                await self._finish_insufficient(
                    store,
                    task,
                    plan,
                    snapshot,
                    total_documents,
                    candidate_truncated,
                    "已逐份检索候选文件，但没有命中可用于比较的直接条款。",
                    settings,
                    examined_documents=len(documents),
                )
                return

            store.update_research_task(
                task_id,
                status="analyzing",
                percent=80,
                message="正在把直接条款转换为结构化事实并比较差异。",
                total_documents=total_documents,
                examined_documents=len(documents),
                evidence_documents=len(sources_by_document),
            )
            indexed_sources = [
                (index, source, source_documents[index - 1])
                for index, source in enumerate(sources, start=1)
            ]
            analyzer = ResearchAnalyzer(settings, llm)
            facts: list[dict[str, Any]] = []
            batch_size = settings.research_analysis_batch_size
            for start in range(0, len(indexed_sources), batch_size):
                facts.extend(
                    await analyzer.analyze_batch(
                        task["retrieval_question"],
                        plan,
                        indexed_sources[start : start + batch_size],
                    )
                )
            facts, sources = self._compact_fact_sources(facts, sources)
            store.update_research_task(
                task_id,
                status="analyzing",
                percent=92,
                message="正在生成研究结论、对比矩阵和覆盖说明。",
                total_documents=total_documents,
                examined_documents=len(documents),
                evidence_documents=len(sources_by_document),
            )

            answer = await self._render_answer(task["retrieval_question"], plan, facts, sources, llm, settings)
            no_evidence_documents = max(0, len(documents) - len(sources_by_document))
            notes: list[str] = []
            if candidate_truncated:
                notes.append(
                    f"候选目录共 {total_documents} 份，本次按服务器研究上限审查前 {len(documents)} 份。"
                )
            if no_evidence_documents:
                notes.append(f"{no_evidence_documents} 份候选文件未命中可比较的直接条款。")
            if failed_documents:
                notes.append(f"{failed_documents} 份候选文件检索失败。")
            final_status = (
                "partial"
                if candidate_truncated or no_evidence_documents or failed_documents
                else "completed"
            )
            quota = store.settle_qa_quota(
                task["request_id"],
                "answered",
                len(answer),
                settings.quota_timezone,
            )
            result = ResearchResult(
                task_id=task_id,
                request_id=task["request_id"],
                question=task["question"],
                session_id=task["conversation_id"],
                answer=answer,
                status=final_status,
                quota_cost=int(task["quota_cost"]),
                reserved_quota_units=int(task["reserved_quota_units"]),
                sources=sources[:30],
                limitations=Limitations(
                    has_clause_level_evidence=True,
                    notes=notes,
                ),
                coverage=ResearchCoverage(
                    examined_documents=len(documents),
                    total_documents=total_documents,
                    evidence_documents=len(sources_by_document),
                    candidate_truncated=candidate_truncated,
                    knowledge_snapshot=snapshot,
                    notes=notes,
                ),
                confidence="high" if final_status == "completed" else "medium",
                quota=QuotaInfo(**quota),
            )
            store.complete_research_task(task_id, final_status, result.model_dump(mode="json"))
            self._save_exchange(store, task, result)
            self._write_usage(task, result)
        except Exception as error:
            logger.exception("Deep research task %s failed", task_id)
            try:
                task = store.get_research_task_internal(task_id)
                store.fail_qa_quota(task["request_id"], settings.quota_timezone)
                store.complete_research_task(
                    task_id,
                    "failed",
                    None,
                    error_code=type(error).__name__,
                )
            except Exception:
                logger.exception("Unable to settle failed deep research task %s", task_id)
        finally:
            await knowledge.aclose()
            await llm.aclose()

    async def _retrieve_documents(
        self,
        store,
        task_id: str,
        task: dict[str, Any],
        plan: ResearchPlan,
        documents: list[dict[str, Any]],
        total_documents: int,
        knowledge: KnowledgeClient,
        settings: Settings,
    ) -> tuple[dict[str, list[Source]], int]:
        semaphore = asyncio.Semaphore(settings.research_document_concurrency)
        progress_lock = asyncio.Lock()
        examined = 0
        evidence_documents = 0
        failed_documents = 0
        results: dict[str, list[Source]] = {}
        combined_query = " ".join(
            dict.fromkeys(
                (
                    plan.canonical_question,
                    *plan.evidence_queries,
                    *plan.comparison_dimensions,
                    *(term for group in plan.required_evidence_groups for term in group),
                )
            )
        )[:1500]

        async def retrieve(document: dict[str, Any]) -> None:
            nonlocal examined, evidence_documents, failed_documents
            document_id = str(document.get("document_id") or "")
            async with semaphore:
                base = understand_query(combined_query)
                scoped_plan: QueryPlan = replace(
                    base,
                    original_query=task["retrieval_question"],
                    normalized_query=plan.canonical_question,
                    retrieval_query=combined_query,
                    intent="cross_document_audit",
                    candidate_title_terms=(),
                    standard_numbers=(),
                    document_types=(str(document.get("document_type") or "standard"),),
                    required_evidence_groups=plan.required_evidence_groups,
                    search_mode="scoped",
                    comparison_dimensions=plan.comparison_dimensions,
                    scope_origin="none",
                    planner_used=plan.planner_used,
                    exhaustive_search=False,
                )
                filters = dict(task.get("filters") or {})
                filters["document_id"] = document_id
                try:
                    response = await knowledge.search(
                        combined_query,
                        filters,
                        scoped_plan,
                        top_k=6,
                        allow_web_supplement=False,
                    )
                    sources = [
                        _source_from_hit(hit)
                        for hit in response.results
                        if (hit.get("quote") or hit.get("evidence_text"))
                        and (hit.get("clause_no") or hit.get("section_path"))
                        and self._hit_matches_evidence_groups(hit, plan.required_evidence_groups)
                        and not self._hit_is_normative_reference_list(
                            hit,
                            task["retrieval_question"],
                        )
                    ][:2]
                    if sources:
                        results[document_id] = sources
                except Exception:
                    failed_documents += 1
                    logger.exception("Research retrieval failed for document %s", document_id)
                async with progress_lock:
                    examined += 1
                    if document_id in results:
                        evidence_documents += 1
                    percent = 20 + int(55 * examined / max(1, len(documents)))
                    store.update_research_task(
                        task_id,
                        status="retrieving",
                        percent=percent,
                        message=f"正在逐份检索：已审查 {examined}/{len(documents)} 份。",
                        total_documents=total_documents,
                        examined_documents=examined,
                        evidence_documents=evidence_documents,
                    )

        await asyncio.gather(*(retrieve(document) for document in documents))
        return results, failed_documents

    @staticmethod
    def _compact_fact_sources(
        facts: list[dict[str, Any]],
        sources: list[Source],
    ) -> tuple[list[dict[str, Any]], list[Source]]:
        used_indices: list[int] = []
        for fact in facts:
            for index in fact.get("source_indices", []):
                if isinstance(index, int) and 1 <= index <= len(sources) and index not in used_indices:
                    used_indices.append(index)
        used_indices = used_indices[:24]
        remap = {old: new for new, old in enumerate(used_indices, start=1)}
        compact_facts: list[dict[str, Any]] = []
        for fact in facts:
            mapped = [remap[index] for index in fact.get("source_indices", []) if index in remap]
            if not mapped:
                continue
            compact = dict(fact)
            compact["source_indices"] = mapped
            compact_facts.append(compact)
        compact_sources = [sources[index - 1] for index in used_indices]
        return compact_facts, compact_sources

    @staticmethod
    def _hit_matches_evidence_groups(
        hit: dict[str, Any],
        groups: tuple[tuple[str, ...], ...],
    ) -> bool:
        if not groups:
            return True
        context = " ".join(
            str(hit.get(key) or "")
            for key in ("title", "standard_no", "clause_no", "section_path", "quote", "evidence_text")
        )
        return all(any(term and term in context for term in group) for group in groups)

    @staticmethod
    def _hit_is_normative_reference_list(hit: dict[str, Any], question: str) -> bool:
        if any(
            term in question
            for term in (
                "规范性引用文件",
                "引用了哪些",
                "引用哪些",
                "哪些规范引用",
                "哪些标准引用",
                "哪些文件引用",
                "引用了",
                "引用标准",
                "被引用",
                "引用关系",
                "引用情况",
                "参考标准",
            )
        ):
            return False
        chapter = re.sub(
            r"\s+",
            "",
            str(hit.get("clause_no") or hit.get("section_path") or ""),
        )
        quote = re.sub(
            r"\s+",
            " ",
            str(hit.get("quote") or hit.get("evidence_text") or hit.get("text") or ""),
        ).strip()
        reference_count = len(
            re.findall(
                r"\b(?:GB(?:/T)?|DZ/T|DZ|HJ|NB/T|MT/T|YS/T|JB/T|AQ|TD/T)\s*\d{2,}",
                quote,
                flags=re.IGNORECASE,
            )
        )
        if "规范性引用文件" in chapter:
            return True
        if chapter in {"2", "2.0", "第2章"} and reference_count >= 2:
            return True
        page = hit.get("page") or hit.get("page_start")
        try:
            early_page = int(page) <= 4
        except (TypeError, ValueError):
            early_page = bool(re.fullmatch(r"第?[1-4]页", chapter))
        return early_page and reference_count >= 3

    async def _render_answer(
        self,
        question: str,
        plan: ResearchPlan,
        facts: list[dict[str, Any]],
        sources: list[Source],
        llm: LLMClient,
        settings: Settings,
    ) -> str:
        summary = "本次研究已按知识库候选范围逐份检索，并仅使用命中的直接条款形成下列比较结果。"
        document_count = len({fact.get("document_id") for fact in facts if fact.get("document_id")})
        if document_count:
            summary = (
                f"本次研究在 {document_count} 份文件中形成了可回溯到直接条款的比较事实。"
                "下表分别列出所依据的工程间距、外推比例和适用条件；相同数值不代表适用前提相同。"
            )
        if llm.enabled and facts:
            try:
                completion = await llm.complete_detailed(
                    [
                        {
                            "role": "system",
                            "content": (
                                "你是 geowiki 深度研究摘要器。只概括给定结构化事实，不能增加新标准、"
                                "新数值或模型常识。用2至4句中文说明主要一致点、差异和不确定性。"
                                "必须保持用户问题限定的外推类型，严禁把有限外推当作无限外推，或反向替换。"
                                "只有至少两份文件在同一比较维度上有直接事实时，才能概括为一致。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "question": question,
                                    "comparison_dimensions": plan.comparison_dimensions,
                                    "facts": facts,
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    max_tokens=min(700, settings.research_answer_max_tokens),
                    temperature=settings.research_answer_temperature,
                )
                if completion.content and self._summary_matches_scope(question, completion.content):
                    summary = completion.content
            except Exception:
                pass

        source_by_index = {index: source for index, source in enumerate(sources, start=1)}
        document_meta: dict[str, Source] = {}
        for index, source in source_by_index.items():
            document_id = next(
                (
                    fact["document_id"]
                    for fact in facts
                    if index in fact.get("source_indices", [])
                ),
                f"source-{index}",
            )
            document_meta.setdefault(document_id, source)

        lines = ["**研究结论**", "", summary.strip(), "", "**对比结果**", ""]
        lines.extend(
            [
                "| 文件 | 判定 | 比较维度 | 具体发现 | 依据条款 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for fact in facts[:40]:
            indices = [index for index in fact.get("source_indices", []) if index in source_by_index]
            source = source_by_index[indices[0]] if indices else document_meta.get(fact["document_id"])
            file_label = (
                f"{source.standard_no or ''}《{source.title}》" if source else fact["document_id"]
            )
            clauses = "、".join(
                dict.fromkeys(source_by_index[index].chapter or "相关条款" for index in indices)
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        _markdown_cell(file_label),
                        _markdown_cell(CLASSIFICATION_LABELS.get(fact["classification"], fact["classification"])),
                        _markdown_cell(fact.get("dimension") or "未标注"),
                        _markdown_cell(fact.get("finding")),
                        _markdown_cell(clauses or "相关条款"),
                    ]
                )
                + " |"
            )

        return "\n".join(lines).strip()

    @staticmethod
    def _summary_matches_scope(question: str, summary: str) -> bool:
        if UNSUPPORTED_ABSENCE_PATTERN.search(summary):
            return False
        if "无限外推" in question and "有限外推" in summary:
            return False
        if "有限外推" in question and "无限外推" in summary:
            return False
        return True

    async def _finish_insufficient(
        self,
        store,
        task: dict[str, Any],
        plan: ResearchPlan,
        snapshot: str | None,
        total_documents: int,
        candidate_truncated: bool,
        reason: str,
        settings: Settings,
        *,
        examined_documents: int = 0,
    ) -> None:
        answer = (
            "**深度研究未形成可引用结论。**\n\n"
            f"{reason}\n\n"
            "系统没有使用模型常识替代标准正文。建议补充目标文件正文、缩小候选范围，"
            "或由知识库管理员完成缺失文件入库后重新研究。"
        )
        quota = store.settle_qa_quota(
            task["request_id"],
            "insufficient_evidence",
            len(answer),
            settings.quota_timezone,
        )
        notes = [reason, plan.scope_note]
        result = ResearchResult(
            task_id=task["task_id"],
            request_id=task["request_id"],
            question=task["question"],
            session_id=task["conversation_id"],
            answer=answer,
            status="insufficient_evidence",
            quota_cost=int(task["quota_cost"]),
            reserved_quota_units=int(task["reserved_quota_units"]),
            limitations=Limitations(has_clause_level_evidence=False, notes=notes),
            coverage=ResearchCoverage(
                examined_documents=examined_documents,
                total_documents=total_documents,
                evidence_documents=0,
                candidate_truncated=candidate_truncated,
                knowledge_snapshot=snapshot,
                notes=notes,
            ),
            confidence="low",
            quota=QuotaInfo(**quota),
        )
        store.complete_research_task(
            task["task_id"],
            "insufficient_evidence",
            result.model_dump(mode="json"),
        )
        self._save_exchange(store, task, result)
        self._write_usage(task, result)

    @staticmethod
    def _save_exchange(store, task: dict[str, Any], result: ResearchResult) -> None:
        try:
            store.save_exchange(
                task["user_id"],
                task["conversation_id"],
                task["request_id"],
                task["question"],
                result.answer,
                {
                    "mode": "deep",
                    "task_id": task["task_id"],
                    "status": result.status,
                    "confidence": result.confidence,
                    "sources": [source.model_dump(mode="json") for source in result.sources],
                    "limitations": result.limitations.model_dump(mode="json"),
                    "coverage": result.coverage.model_dump(mode="json"),
                    "quota": result.quota.model_dump(mode="json") if result.quota else None,
                },
            )
        except Exception:
            logger.exception("Unable to persist deep research conversation %s", task["task_id"])

    def _write_usage(self, task: dict[str, Any], result: ResearchResult) -> None:
        self._usage.write(
            {
                "user_id": task["user_id"],
                "credential_id": task.get("api_key_id"),
                "auth_type": "api_key" if task.get("api_key_id") else "session",
                "endpoint": "/api/research/tasks",
                "method": "BACKGROUND",
                "request_id": task["request_id"],
                "task_id": task["task_id"],
                "status": result.status,
                "source_count": len(result.sources),
                "quota_consumed_units": result.quota.consumed_units if result.quota else 0,
                "quota_remaining": result.quota.remaining if result.quota else None,
                "examined_documents": result.coverage.examined_documents,
                "total_documents": result.coverage.total_documents,
            }
        )


def research_task_response(task: dict[str, Any], quota: dict[str, Any] | None = None) -> ResearchTaskResponse:
    result_quota = (task.get("result") or {}).get("quota") if isinstance(task.get("result"), dict) else None
    effective_quota = result_quota or quota
    return ResearchTaskResponse(
        task_id=task["task_id"],
        request_id=task["request_id"],
        question=task["question"],
        session_id=task["conversation_id"],
        status=task["status"],
        quota_cost=int(task["quota_cost"]),
        reserved_quota_units=int(task["reserved_quota_units"]),
        progress=ResearchProgress(
            stage=task["stage"],
            percent=int(task["progress_percent"]),
            message=task["status_message"] or "",
            examined_documents=int(task["examined_documents"]),
            total_documents=int(task["total_documents"]),
            evidence_documents=int(task["evidence_documents"]),
        ),
        result_available=task.get("result") is not None,
        quota=QuotaInfo(**effective_quota) if effective_quota else None,
        created_at=task["created_at"],
        started_at=task.get("started_at"),
        finished_at=task.get("finished_at"),
    )


research_runner = ResearchTaskRunner()
