import asyncio
import json
import math
import re
from dataclasses import replace
from time import perf_counter
from uuid import uuid4

from .config import Settings
from .domain_gate import DomainGate
from .evidence_reranker import EvidenceReranker, RerankResult
from .gap_tasks import KnowledgeGapTaskStore
from .knowledge_client import KnowledgeClient
from .llm_client import LLMClient
from .prompt_registry import prompt_text
from .query_understanding import (
    PROJECTION_REFERENCE_STANDARD_NUMBERS,
    QueryPlan,
    TRANSFER_CONDITION_TERMS,
    TRANSFER_EQUIVALENT_TERMS,
    TRANSFER_REPORT_OBJECT_TERMS,
    is_post_filing_license_steps_query,
    normalize_user_query,
    understand_query,
)
from .retrieval_planner import QueryVariant, RetrievalPlanner
from .retrieval_trace import RetrievalTraceLogger
from .schemas import AskRequest, AskResponse, Limitations, RetrievalStats, Source
from .technical_test_hierarchy import (
    MINERAL_PROCESSING_TEST_LEVELS,
    actual_level_from_sufficiency_question,
    level_covers,
    levels_in_text,
    required_level_from_sufficiency_question,
)
from .web_supplement import WebSupplement


ANSWER_CACHE_ENABLED = False
ANSWER_CACHE: dict[str, AskResponse] = {}
CACHEABLE_COMPARISON_TERMS = ("不一致", "差异", "不同", "比较", "列举", "哪些标准", "哪些规范")
PROJECTION_DISTANCE_TERMS = ("外推所依据的距离", "外推依据", "外推距离", "依据的距离")
SERVICE_CHANGE_SECTIONS = {
    "expand_area": "扩大矿区范围",
    "shrink_area": "缩小矿区范围",
    "mineral_or_mining_method": "开采主矿种、开采方式",
    "holder_name": "采矿权人名称",
    "transfer": "转让",
}
DETERMINISTIC_FAST_INTENTS = {
    "engineering_distance_lookup",
    "projection_numeric_rule",
    "legal_responsibility",
    "service_materials",
    "service_procedure_basis",
    "service_time_limit",
    "authority_responsibility",
    "standard_selection",
    "exploration_to_mining_eligibility",
    "companion_resource_type",
    "exploration_type_factors",
    "basic_analysis_items",
    "projection_comparison",
    "definition_explanation",
    "technical_requirement_sufficiency",
}
SYSTEM_PROMPT = """你是矿产资源标准知识问答 agent。

必须遵守：
1. 只根据给定证据回答标准条款级问题。
2. 如果没有条款级证据，必须明确说明证据不足，不能编造标准条文。
3. 回答优先包含：结论、依据标准名称、标准号、条款、原文片段、适用条件、不确定性。
4. 不要输出大段标准全文；只输出必要引用和摘要。
5. 如果来源存在限制或冲突，必须提示。
6. 涉及表格时，必须按表头、子表头、行名和列名交叉读取，不得把多列数值合并成一个笼统结论。
7. 如果同一问题的高置信证据集中在某一个标准，不要引用低相关度的其他标准。
8. 如果用户询问“应使用哪个标准/适用哪个规范/采用哪个标准”，可以根据标准标题、标准号和目录证据回答，不强制要求条款号。
9. 必须区分勘查程度、报告名称和行政许可条件；不得把“达到详查程度”改写成任何名为“详查报告”的文件都满足转采条件。
10. 回答通常控制在600个汉字以内；比较类问题只保留代表性差异和直接相关短引文。
"""

UNRESOLVED_CONFIRMATION_LINE = re.compile(
    r"(?:请先确认办理类型|变更申请还需进一步说明具体变更事项|请选择更接近你实际需求的方向)"
)


class MiningQAAgent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.knowledge = KnowledgeClient(settings)
        self.llm = LLMClient(settings)
        self.web = WebSupplement(settings, self.llm)
        self.domain_gate = DomainGate()
        self.gap_tasks = KnowledgeGapTaskStore()
        self.planner = RetrievalPlanner(settings, self.llm)
        self.reranker = EvidenceReranker(settings, self.llm)
        self.trace = RetrievalTraceLogger(
            settings.retrieval_trace_path,
            enabled=settings.retrieval_trace_enabled,
        )

    async def aclose(self) -> None:
        await self.knowledge.aclose()
        await self.llm.aclose()

    async def ask(self, request: AskRequest) -> AskResponse:
        started = perf_counter()
        trace_id = "trace_" + uuid4().hex
        session_id = request.session_id or str(uuid4())
        question = request.retrieval_question
        cache_key = self._cache_key(question)
        if ANSWER_CACHE_ENABLED and cache_key in ANSWER_CACHE:
            cached = ANSWER_CACHE[cache_key].model_copy(deep=True)
            cached.session_id = session_id
            return cached

        domain_decision = self.domain_gate.check(question)
        if not domain_decision.in_scope:
            limitations = Limitations(
                has_clause_level_evidence=False,
                notes=["问题不属于矿产资源标准规范及相关政策技术服务范围，已拒绝处理。"],
            )
            response = AskResponse(
                answer="本服务仅回答矿产资源、地质勘查、矿山设计、自然资源管理、标准规范及相关政策技术问题，无法处理该问题。",
                session_id=session_id,
                status="out_of_scope",
                limitations=limitations,
                confidence="low",
            )
            response.retrieval.total_ms = round((perf_counter() - started) * 1000, 3)
            self._write_trace(trace_id, question, None, response, {})
            return response

        filters = request.filters.model_dump(exclude_none=True)
        base_plan = request.query_plan or understand_query(question)
        planner_result = await self.planner.plan(question, base_plan)
        plan = planner_result.plan
        rounds = max(1, min(2, int(self.settings.max_retrieval_rounds)))
        merged_hits: list[dict] = []
        kb_results = []
        rerank_result: RerankResult | None = None
        reranker_ms = 0.0
        knowledge_ms = 0.0
        generation_details: dict[str, object] = {"used": False}
        logical_rounds = 1
        multi_query_count = 0
        supplemental_error_count = 0

        kb_started = perf_counter()
        kb_result = await self.knowledge.search(
            question,
            filters,
            plan,
            retrieval_round=1,
        )
        knowledge_ms += (perf_counter() - kb_started) * 1000
        kb_results.append(kb_result)
        merged_hits = self._merge_hits(merged_hits, kb_result.results)

        if self.reranker.needs_model(plan):
            rerank_result = await self.reranker.judge(question, plan, merged_hits)
            reranker_ms += rerank_result.elapsed_ms
            if not rerank_result.sufficient and rounds > 1:
                supplemental = self._supplemental_plans(
                    plan,
                    planner_result.query_variants,
                    rerank_result,
                )
                if supplemental:
                    logical_rounds = 2
                    multi_query_count = sum(1 for _, is_multi_query in supplemental if is_multi_query)
                    batch_started = perf_counter()
                    batch_results = await asyncio.gather(
                        *(
                            self.knowledge.search(
                                question,
                                filters,
                                supplemental_plan,
                                retrieval_round=2,
                            )
                            for supplemental_plan, _ in supplemental
                        ),
                        return_exceptions=True,
                    )
                    knowledge_ms += (perf_counter() - batch_started) * 1000
                    for result in batch_results:
                        if isinstance(result, Exception):
                            supplemental_error_count += 1
                            continue
                        kb_results.append(result)
                        merged_hits = self._merge_hits(merged_hits, result.results)
                    rerank_result = await self.reranker.judge(question, plan, merged_hits)
                    reranker_ms += rerank_result.elapsed_ms

        kb_result = kb_results[-1]
        if self.reranker.needs_model(plan):
            if rerank_result is None:
                rerank_result = await self.reranker.judge(question, plan, merged_hits)
                reranker_ms += rerank_result.elapsed_ms
            evidence_hits = list(rerank_result.hits)
            if plan.intent in {"projection_comparison", "technical_requirement_sufficiency"}:
                concrete_hits = self._select_evidence_hits(merged_hits, question, plan)
                if concrete_hits:
                    evidence_hits = concrete_hits
        else:
            evidence_hits = self._select_evidence_hits(merged_hits, question, plan)
        sources = [self._source_from_hit(hit) for hit in evidence_hits]
        sources = self._trim_source_quotes(question, sources, plan)
        if rerank_result is not None and self.reranker.needs_model(plan):
            deterministic_usable, deterministic_clause = self._evaluate_evidence(
                question,
                kb_result.coverage,
                sources,
                plan,
            )
            has_usable_evidence = rerank_result.sufficient or (
                plan.intent in {"projection_comparison", "technical_requirement_sufficiency"}
                and deterministic_usable
            )
            has_clause_evidence = bool(sources) and (
                rerank_result.sufficient
                or (
                    plan.intent in {"projection_comparison", "technical_requirement_sufficiency"}
                    and deterministic_clause
                )
            )
        else:
            has_usable_evidence, has_clause_evidence = self._evaluate_evidence(
                question,
                kb_result.coverage,
                sources,
                plan,
            )
        notes = list(dict.fromkeys(
            note
            for result in kb_results
            for note in result.coverage.get("notes", [])
        ))
        if has_usable_evidence:
            notes = [note for note in notes if "未命中可引用证据" not in note]
        if logical_rounds > 1:
            notes.append("首轮证据不足，已按证据缺口执行第二轮受控检索。")
        if multi_query_count:
            notes.append(f"第二轮使用 {multi_query_count} 条按证据目标约束的子查询补充召回。")
        if supplemental_error_count:
            notes.append("部分补充查询执行失败，已使用其余检索结果继续完成证据审查。")
        if planner_result.error:
            notes.append("查询规划器不可用，本次已使用确定性理解方案降级检索。")
        if rerank_result and rerank_result.error:
            notes.append("证据审查器不可用，本次已使用证据关系组进行确定性审查。")

        retrieval = RetrievalStats(
            full_text_hits=sum(int(result.retrieval.get("full_text_hits", 0)) for result in kb_results),
            vector_hits=sum(int(result.retrieval.get("vector_hits", 0)) for result in kb_results),
            graph_hits=sum(int(result.retrieval.get("graph_hits", 0)) for result in kb_results),
            web_hits=sum(int(result.retrieval.get("web_hits", 0)) for result in kb_results),
            direct_evidence_hits=(
                max(rerank_result.direct_evidence_count, len(evidence_hits))
                if rerank_result
                else len(evidence_hits)
            ),
            retrieval_rounds=logical_rounds,
            planner_used=planner_result.used,
            reranker_used=bool(rerank_result and rerank_result.used),
            ann_used=any(bool(result.retrieval.get("ann_used")) for result in kb_results),
            query_count=len(kb_results),
            multi_query_used=multi_query_count > 0,
            multi_query_count=multi_query_count,
            mmr_used=any(bool(result.retrieval.get("mmr_used")) for result in kb_results),
            planner_ms=round(planner_result.elapsed_ms, 3),
            knowledge_ms=round(knowledge_ms, 3),
            reranker_ms=round(reranker_ms, 3),
            mmr_ms=round(
                sum(
                    float((result.retrieval.get("timings_ms") or {}).get("mmr") or 0.0)
                    for result in kb_results
                ),
                3,
            ),
        )
        needs_supplement = not has_usable_evidence or bool(kb_result.coverage.get("needs_web_supplement"))
        if needs_supplement and self.settings.enable_sync_web_supplement:
            notes.append("本地知识库证据不足，建议补充官方元数据、全文入口或 OCR 任务。")
            web_result = await self.web.search(question)
            sources.extend(web_result.sources)
            retrieval.web_hits = len(web_result.sources)
            notes.extend(web_result.notes)
            staged_count = await self.knowledge.create_candidates(
                question,
                [source.model_dump(exclude_none=True) for source in web_result.sources],
            )
            if staged_count:
                notes.append(f"已将 {staged_count} 条联网候选来源写入候选暂存区，等待管理员审核后入库。")
        elif needs_supplement:
            notes.append("本地知识库证据不足，已进入异步补库流程；本次请求不等待联网搜索或 OCR。")

        limitations = Limitations(has_clause_level_evidence=has_clause_evidence, notes=notes)

        if not has_usable_evidence:
            gap_task = self.gap_tasks.create(question, domain_decision, len(sources))
            response = AskResponse(
                answer=self._insufficient_answer(request.question, notes),
                session_id=session_id,
                status="queued_for_enrichment",
                sources=sources,
                retrieval=retrieval,
                limitations=limitations,
                knowledge_gap_task=gap_task,
                confidence="low",
                mode_recommendation=("deep" if self._should_recommend_deep(plan, question) else None),
                mode_recommendation_reason=(
                    "该问题需要扩大候选范围并逐文件核验，建议使用深度模式。"
                    if self._should_recommend_deep(plan, question)
                    else None
                ),
                query_classification=(
                    plan.classification.to_payload() if plan.classification else None
                ),
            )
            response.retrieval.total_ms = round((perf_counter() - started) * 1000, 3)
            self._write_trace(
                trace_id,
                question,
                plan,
                response,
                self._trace_details(planner_result, kb_results, rerank_result, generation_details),
            )
            return response

        synthesis_started = perf_counter()
        answer = self._fast_answer(question, sources, plan) if plan.intent in DETERMINISTIC_FAST_INTENTS else None
        if answer is not None:
            generation_details = {
                "used": False,
                "reason": (
                    "deterministic_definition_template"
                    if plan.intent == "definition_explanation"
                    else "deterministic_answer_template"
                ),
            }
        if answer is None and rerank_result and rerank_result.sufficient and rerank_result.grounded_answer:
            answer = rerank_result.grounded_answer
        if answer is None and (not self.llm.enabled or bool(rerank_result and rerank_result.error)):
            answer = self._fast_answer(question, sources, plan)
        if answer is None:
            try:
                max_tokens = (
                    self._definition_max_tokens(sources, plan)
                    if plan.intent == "definition_explanation"
                    else self.settings.answer_max_tokens
                )
                temperature = 0.0 if plan.intent == "definition_explanation" else None
                completion = await self.llm.complete_detailed(
                    self._messages(
                        question,
                        sources,
                        limitations,
                        plan,
                        rerank_result.facts if rerank_result else (),
                    ),
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                answer = completion.content
                generation_details = {
                    "used": True,
                    "finish_reason": completion.finish_reason,
                    "prompt_tokens": completion.prompt_tokens,
                    "completion_tokens": completion.completion_tokens,
                    "temperature": (
                        temperature if temperature is not None else self.settings.answer_temperature
                    ),
                    "max_tokens": max_tokens,
                    "truncated": completion.finish_reason == "length",
                }
            except Exception:
                answer = self._fast_answer(question, sources, plan) or self._evidence_summary_answer(sources)
                limitations.notes.append("回答模型调用超时或不可用，已按审查通过的证据生成确定性降级答案。")
                generation_details = {"used": False, "error": "answer_model_unavailable"}
        retrieval.synthesis_ms = round((perf_counter() - synthesis_started) * 1000, 3)
        answer = self._remove_stale_confirmation(answer, plan)
        answer = self._append_source_links(answer, sources)
        response = AskResponse(
            answer=answer,
            session_id=session_id,
            status="answered",
            sources=sources,
            retrieval=retrieval,
            limitations=limitations,
            confidence="high"
            if rerank_result and rerank_result.confidence >= 0.8
            else "medium" if sources else "low",
            mode_recommendation=("deep" if self._should_recommend_deep(plan, question) else None),
            mode_recommendation_reason=(
                "该问题涉及跨文件完整性核验或差异比较，深度模式会枚举候选文件并逐份审查。"
                if self._should_recommend_deep(plan, question)
                else None
            ),
            query_classification=(
                plan.classification.to_payload() if plan.classification else None
            ),
        )
        response.retrieval.total_ms = round((perf_counter() - started) * 1000, 3)
        if ANSWER_CACHE_ENABLED and self._is_cacheable_question(question):
            ANSWER_CACHE[cache_key] = response.model_copy(deep=True)
        self._write_trace(
            trace_id,
            question,
            plan,
            response,
            self._trace_details(planner_result, kb_results, rerank_result, generation_details),
        )
        return response

    @staticmethod
    def _remove_stale_confirmation(answer: str, plan: QueryPlan) -> str:
        classification = plan.classification
        if not classification or classification.missing_slots:
            return answer
        lines = [
            line
            for line in answer.splitlines()
            if not UNRESOLVED_CONFIRMATION_LINE.search(line)
        ]
        return "\n".join(lines).strip()

    def _merge_hits(self, existing: list[dict], incoming: list[dict]) -> list[dict]:
        merged: dict[tuple[str, str, str], dict] = {}
        order: list[tuple[str, str, str]] = []
        for hit in [*existing, *incoming]:
            key = (
                str(hit.get("chunk_id") or hit.get("document_id") or ""),
                str(hit.get("clause_no") or hit.get("section_path") or ""),
                str(hit.get("quote") or "")[:120],
            )
            if key not in merged:
                merged[key] = dict(hit)
                order.append(key)
                continue
            current = merged[key]
            if float(hit.get("score") or 0.0) > float(current.get("score") or 0.0):
                preserved_routes = current.get("hit_type") or []
                current = dict(hit)
                current["hit_type"] = preserved_routes
                merged[key] = current
            current["hit_type"] = sorted(
                set(current.get("hit_type") or []) | set(hit.get("hit_type") or [])
            )
        return sorted(
            (merged[key] for key in order),
            key=lambda hit: float(hit.get("score") or 0.0),
            reverse=True,
        )[:48]

    def _refined_plan(self, plan: QueryPlan, result: RerankResult) -> QueryPlan:
        refined_query = normalize_user_query(result.refined_query)
        refined_terms = tuple(
            dict.fromkeys(
                term
                for term in (*plan.alternative_terms, *result.refined_terms)
                if term
            )
        )
        parts = [refined_query, *refined_terms, *plan.required_terms, *plan.standard_numbers]
        retrieval_query = " ".join(dict.fromkeys(part for part in parts if part))[:1000]
        return replace(
            plan,
            retrieval_query=retrieval_query or plan.retrieval_query,
            alternative_terms=refined_terms,
            search_mode="exhaustive" if plan.search_mode == "comparison" else plan.search_mode,
            exhaustive_search=True,
        )

    def _supplemental_plans(
        self,
        plan: QueryPlan,
        variants: tuple[QueryVariant, ...],
        result: RerankResult,
    ) -> list[tuple[QueryPlan, bool]]:
        limit = max(1, min(3, int(self.settings.controlled_multi_query_max)))
        supplemental: list[tuple[QueryPlan, bool]] = []
        seen = {plan.retrieval_query}
        refined = self._refined_plan(plan, result)
        if refined.retrieval_query not in seen:
            supplemental.append((refined, False))
            seen.add(refined.retrieval_query)

        multi_query_allowed = (
            self.settings.controlled_multi_query_enabled
            and not plan.has_hard_candidate_scope
            and not plan.standard_numbers
            and (
                plan.search_mode in {"comparison", "exhaustive"}
                or plan.intent in {
                    "general",
                    "regulation_lookup",
                    "related_documents",
                    "projection_comparison",
                    "clause_comparison",
                }
            )
        )
        if multi_query_allowed:
            for variant in variants:
                retrieval_query = normalize_user_query(variant.query)
                if not retrieval_query or retrieval_query in seen:
                    continue
                seen.add(retrieval_query)
                supplemental.append(
                    (
                        replace(
                            plan,
                            retrieval_query=retrieval_query,
                            alternative_terms=tuple(
                                dict.fromkeys((*plan.alternative_terms, variant.target))
                            ),
                            exhaustive_search=True,
                        ),
                        True,
                    )
                )
                if len(supplemental) >= limit:
                    break
        return supplemental[:limit]

    def _trace_details(
        self,
        planner_result,
        kb_results: list,
        rerank_result: RerankResult | None,
        generation: dict[str, object] | None = None,
    ) -> dict:
        return {
            "planner": {
                "used": planner_result.used,
                "elapsed_ms": round(planner_result.elapsed_ms, 3),
                "error": planner_result.error,
                "query_variants": [
                    {"target": variant.target, "query": variant.query}
                    for variant in planner_result.query_variants
                ],
            },
            "knowledge_rounds": [
                {
                    "retrieval": result.retrieval,
                    "coverage": result.coverage,
                    "candidate_sources": [
                        {
                            "document_id": hit.get("document_id"),
                            "chunk_id": hit.get("chunk_id"),
                            "standard_no": hit.get("standard_no"),
                            "clause": hit.get("clause_no") or hit.get("section_path"),
                            "score": hit.get("score"),
                            "routes": hit.get("hit_type") or [],
                        }
                        for hit in result.results[:12]
                    ],
                }
                for result in kb_results
            ],
            "reranker": {
                "used": rerank_result.used,
                "elapsed_ms": round(rerank_result.elapsed_ms, 3),
                "sufficient": rerank_result.sufficient,
                "direct_evidence_count": rerank_result.direct_evidence_count,
                "refined_query": rerank_result.refined_query,
                "missing_evidence_groups": rerank_result.missing_evidence_groups,
                "grounded_answer_generated": bool(rerank_result.grounded_answer),
                "error": rerank_result.error,
            }
            if rerank_result
            else None,
            "generation": generation or {"used": False},
        }

    def _write_trace(
        self,
        trace_id: str,
        question: str,
        plan: QueryPlan | None,
        response: AskResponse,
        details: dict,
    ) -> None:
        self.trace.write(
            {
                "trace_id": trace_id,
                "session_id": response.session_id,
                "question": question,
                "plan": plan.to_payload() if plan else None,
                "status": response.status,
                "confidence": response.confidence,
                "retrieval": response.retrieval.model_dump(mode="json"),
                "selected_sources": [
                    {
                        "standard_no": source.standard_no,
                        "title": source.title,
                        "clause": source.chapter,
                        "score": source.score,
                    }
                    for source in response.sources
                ],
                **details,
            }
        )

    def _evaluate_evidence(
        self,
        question: str,
        coverage: dict,
        sources: list[Source],
        plan: QueryPlan | None = None,
    ) -> tuple[bool, bool]:
        effective_plan = plan or understand_query(question)
        if effective_plan.intent == "definition_explanation":
            matched = {
                term
                for source in sources
                for term in [self._definition_term_from_source(source, effective_plan)]
                if term
            }
            found = bool(effective_plan.definition_slots) and set(
                effective_plan.definition_slots
            ).issubset(matched)
            return found, found
        if effective_plan.intent == "standard_selection":
            found = bool(sources)
            return found, found

        if effective_plan.intent == "exploration_to_mining_eligibility":
            has_policy = any(
                "自然资规〔2023〕4号" in (source.standard_no or "")
                and "经评审备案的矿产资源储量报告" in (source.quote or "")
                and "详查（含）以上程度" in (source.quote or "")
                for source in sources
            )
            has_report_limit = any(
                "不能替代探矿权转采矿权" in (source.quote or "")
                for source in sources
            )
            found = has_policy and has_report_limit
            return found, found

        if effective_plan.intent == "companion_resource_type":
            clauses = {source.chapter for source in sources}
            found = {"9.2", "9.3", "9.4"}.issubset(clauses)
            return found, found

        if effective_plan.intent == "exploration_type_factors":
            tables = {
                match.group(1)
                for source in sources
                if (match := re.search(r"表\s*E\.([1-5])", source.chapter or "", flags=re.IGNORECASE))
            }
            found = tables == {"1", "2", "3", "4", "5"}
            return found, found

        if effective_plan.intent == "technical_requirement_sufficiency":
            actual_level = actual_level_from_sufficiency_question(question)
            explicit_required_level = required_level_from_sufficiency_question(question)
            stage_sources = [
                source
                for source in sources
                if self._is_technical_stage_requirement_text(source.quote or "", question)
            ]
            required_level = explicit_required_level or max(
                (
                    level
                    for source in stage_sources
                    for level in levels_in_text(source.quote or "")
                ),
                key=lambda item: item.rank,
                default=None,
            )
            actual_level_source = next(
                (
                    source
                    for source in sources
                    if actual_level and self._is_technical_test_level_source(source, actual_level)
                ),
                None,
            )
            found = bool(
                actual_level
                and required_level
                and actual_level_source
                and (stage_sources or explicit_required_level)
            )
            return found, found

        validators = {
            "engineering_distance_lookup": self._is_engineering_distance_source,
            "authority_responsibility": self._is_policy_authority_source,
            "service_materials": self._is_service_material_source,
            "service_procedure_basis": self._is_service_procedure_source,
            "service_time_limit": self._is_service_time_limit_source,
            "projection_numeric_rule": self._is_projection_numeric_source,
            "legal_responsibility": self._is_legal_responsibility_source,
            "basic_analysis_items": self._is_basic_analysis_source,
        }
        validator = validators.get(effective_plan.intent)
        if validator:
            found = any(validator(source) for source in sources)
            return found, found

        has_clause = bool(coverage.get("has_clause_level_evidence", False)) and bool(sources)
        return has_clause, has_clause

    async def _fallback_retrieval_query(self, question: str) -> str | None:
        if not self.llm.enabled:
            return None
        messages = [
            {
                "role": "system",
                "content": (
                    "你只负责把矿产资源、地质勘查、矿业权管理或标准规范问题改写为检索查询，"
                    "不得回答问题。保留矿种、文号、条款号、数值、办理事项和责任主体。"
                    "返回JSON：{\"rewritten_query\":\"...\",\"keywords\":[\"...\"]}。"
                ),
            },
            {"role": "user", "content": question},
        ]
        try:
            payload = json.loads(await self.llm.complete_json(messages))
        except (json.JSONDecodeError, TypeError, ValueError, OSError):
            return None
        except Exception:
            return None
        rewritten = str(payload.get("rewritten_query") or "").strip()
        keywords = payload.get("keywords") or []
        if not rewritten or len(rewritten) > 300:
            return None
        clean_keywords = [str(item).strip() for item in keywords if str(item).strip()][:8]
        return " ".join(dict.fromkeys([rewritten, *clean_keywords]))

    def _messages(
        self,
        question: str,
        sources: list[Source],
        limitations: Limitations,
        plan: QueryPlan | None = None,
        facts: tuple[dict, ...] = (),
    ) -> list[dict[str, str]]:
        evidence_lines = []
        for index, source in enumerate(sources, start=1):
            quote = self._evidence_quote_for_prompt(question, source.quote, plan)
            evidence_lines.append(
                "\n".join(
                    [
                        f"[{index}] {source.title}",
                        f"标准号: {source.standard_no or '未知'}",
                        f"章节/条款: {source.chapter or '未知'}",
                        f"页码: {source.page if source.page is not None else '未知'}",
                        f"来源类型: {source.source_type}",
                        f"证据角色: {source.source_role or '未标注'}",
                        f"正文访问: {source.text_access}",
                        f"官方链接: {source.url or '无'}",
                        f"原文片段: {quote or '无'}",
                    ]
                )
            )

        user_content = "\n\n".join(
            [
                f"用户问题：{question}",
                "检索计划：",
                json.dumps((plan or understand_query(question)).to_llm_payload(), ensure_ascii=False),
                "证据：",
                "\n\n".join(evidence_lines) if evidence_lines else "无",
                "证据审查提取事实：",
                json.dumps(facts, ensure_ascii=False) if facts else "无",
                "限制：",
                "\n".join(limitations.notes) if limitations.notes else "无",
            ]
        )
        return [
            {
                "role": "system",
                "content": "\n\n".join(
                    part
                    for part in (
                        SYSTEM_PROMPT,
                        prompt_text(
                            self.settings,
                            "answer",
                            primary_intent=(
                                plan.classification.primary_intent
                                if plan and plan.classification
                                else None
                            ),
                        ),
                    )
                    if part
                ),
            },
            {"role": "user", "content": user_content},
        ]

    @staticmethod
    def _is_technical_stage_requirement_text(text: str, question: str) -> bool:
        compact = re.sub(r"\s+", "", text or "")
        requested_stages = [
            stage
            for stage in ("普查阶段", "详查阶段", "勘探阶段")
            if stage in re.sub(r"\s+", "", question or "")
        ]
        stages = requested_stages or ["普查阶段", "详查阶段", "勘探阶段"]
        study_terms = (
            "类比研究",
            "工艺矿物学研究",
            "可选性试验",
            "实验室流程试验",
            "实验室扩大连续试验",
            "半工业试验",
            "工业试验",
            "初步测试研究",
            "基本测试研究",
            "详细测试研究",
        )
        return (
            any(stage in compact for stage in stages)
            and "应" in compact
            and any(term in compact for term in study_terms)
        )

    @staticmethod
    def _is_technical_study_hierarchy_text(text: str) -> bool:
        compact = re.sub(r"\s+", "", text or "")
        study_terms = (
            "类比研究",
            "工艺矿物学研究",
            "可选性试验",
            "实验室流程试验",
            "实验室扩大连续试验",
            "半工业试验",
            "工业试验",
            "初步测试研究",
            "基本测试研究",
            "详细测试研究",
        )
        return bool(
            re.search(r"在[^。；]{0,40}(?:试验|测试)[^。；]{0,16}基础上", compact)
            and sum(term in compact for term in study_terms) >= 2
        )

    @staticmethod
    def _is_technical_test_level_source(source: Source, level: object) -> bool:
        clause = str(source.chapter or "")
        standard_no = re.sub(r"\s+", "", source.standard_no or "").upper()
        source_clause = str(getattr(level, "source_clause", ""))
        source_standard_no = re.sub(
            r"\s+", "", str(getattr(level, "source_standard_no", ""))
        ).upper()
        return bool(
            source_clause
            and (
                clause == source_clause
                or clause.startswith(f"{source_clause} ")
                or source_clause in clause
            )
            and (not source_standard_no or standard_no == source_standard_no)
        )

    def _select_evidence_hits(
        self,
        hits: list[dict],
        question: str,
        plan: QueryPlan | None = None,
    ) -> list[dict]:
        if not hits:
            return []
        effective_plan = plan or understand_query(question)
        if effective_plan.intent == "definition_explanation" and effective_plan.definition_slots:
            selected: list[dict] = []
            for slot in effective_plan.definition_slots:
                candidates = [
                    hit
                    for hit in hits
                    if self._definition_term_from_text(self._hit_evidence_text(hit), effective_plan) == slot
                ]
                if candidates:
                    selected.append(
                        max(
                            candidates,
                            key=lambda hit: (
                                bool(hit.get("clause_no")),
                                float(hit.get("score") or 0.0),
                                len(self._hit_evidence_text(hit)),
                            ),
                        )
                    )
            if selected:
                return selected
        if effective_plan.intent == "technical_requirement_sufficiency":
            actual_level = actual_level_from_sufficiency_question(question)
            explicit_required_level = required_level_from_sufficiency_question(question)
            # When the user explicitly names the target level, the level relation
            # is answerable from the controlled hierarchy alone. Do not attach an
            # arbitrary stage clause from an unrelated mineral standard merely
            # because it also contains that level name.
            stage_hits = (
                []
                if explicit_required_level
                else [
                    hit
                    for hit in hits
                    if self._is_technical_stage_requirement_text(
                        self._hit_evidence_text(hit), question
                    )
                ]
            )
            hierarchy_hits = [
                hit
                for hit in hits
                if actual_level
                and self._is_technical_test_level_source(self._source_from_hit(hit), actual_level)
            ]
            selected = []
            if stage_hits:
                selected.append(
                    max(
                        stage_hits,
                        key=lambda hit: (
                            float(hit.get("score") or 0.0),
                            len(self._hit_evidence_text(hit)),
                        ),
                    )
                )
            if hierarchy_hits:
                hierarchy = max(
                    hierarchy_hits,
                    key=lambda hit: (
                        str(hit.get("clause_no") or hit.get("section_path") or "")
                        == actual_level.source_clause,
                        float(hit.get("score") or 0.0),
                        len(self._hit_evidence_text(hit)),
                    ),
                )
                if hierarchy not in selected:
                    selected.append(hierarchy)
            if selected:
                return selected
        if effective_plan.intent == "engineering_distance_lookup" and effective_plan.target_exploration_type:
            required_labels = ("坑探-穿脉", "坑探-沿脉", "钻探-走向", "钻探-倾斜")
            for hit in hits:
                quote = hit.get("quote") or ""
                title = hit.get("title") or ""
                title_matches = not effective_plan.candidate_title_terms or any(
                    term in title for term in effective_plan.candidate_title_terms
                )
                if (
                    title_matches
                    and f"{effective_plan.target_exploration_type}类型" in quote
                    and all(label in quote for label in required_labels)
                ):
                    return [hit]
        if self._is_standard_selection_question(question):
            catalog_hits = [hit for hit in hits if "catalog" in (hit.get("hit_type") or [])]
            if catalog_hits:
                return catalog_hits[:1]

        if effective_plan.intent == "exploration_to_mining_eligibility":
            policy = next(
                (
                    hit
                    for hit in hits
                    if "自然资规〔2023〕4号" in str(hit.get("standard_no") or "")
                    and "经评审备案的矿产资源储量报告" in self._hit_evidence_text(hit)
                    and "详查（含）以上程度" in self._hit_evidence_text(hit)
                ),
                None,
            )
            report_limit = next(
                (
                    hit
                    for hit in hits
                    if str(hit.get("standard_no") or "").replace(" ", "").upper() == "DZ/T0430-2023"
                    and "不能替代探矿权转采矿权" in self._hit_evidence_text(hit)
                ),
                None,
            )
            special_by_document: dict[str, dict] = {}
            special_order: list[str] = []
            for hit in hits:
                context = " ".join(
                    str(hit.get(key) or "")
                    for key in ("evidence_text", "quote", "text")
                )
                if self._is_transfer_equivalent_evidence(context):
                    prepared = dict(hit)
                    if hit.get("evidence_text"):
                        prepared["quote"] = hit["evidence_text"]
                    document_id = str(hit.get("document_id") or hit.get("standard_no") or "")
                    if document_id not in special_by_document:
                        special_by_document[document_id] = prepared
                        special_order.append(document_id)
                    else:
                        current = special_by_document[document_id]
                        prepared_quality = (
                            int(bool(prepared.get("clause_no"))),
                            int(not self._hit_evidence_text(prepared).lstrip().startswith("前言")),
                            float(prepared.get("score") or 0.0),
                        )
                        current_quality = (
                            int(bool(current.get("clause_no"))),
                            int(not self._hit_evidence_text(current).lstrip().startswith("前言")),
                            float(current.get("score") or 0.0),
                        )
                        if prepared_quality > current_quality:
                            special_by_document[document_id] = prepared
            special = [special_by_document[key] for key in special_order[:4]]
            selected = [hit for hit in (policy, *special, report_limit) if hit]
            if selected:
                return selected

        if effective_plan.intent == "companion_resource_type":
            selected = []
            for clause in ("9.2", "9.3", "9.4"):
                candidates = [
                    hit
                    for hit in hits
                    if str(hit.get("standard_no") or "").replace(" ", "").upper() == "GB/T25283-2023"
                    and str(hit.get("clause_no") or hit.get("section_path") or "") == clause
                ]
                if candidates:
                    selected.append(max(candidates, key=lambda hit: len(self._hit_evidence_text(hit))))
            if selected:
                return selected

        if effective_plan.intent == "exploration_type_factors":
            tables = []
            seen_tables: set[str] = set()
            for hit in hits:
                if str(hit.get("standard_no") or "").replace(" ", "").upper() != "DZ/T0205-2020":
                    continue
                chapter = str(hit.get("section_path") or hit.get("clause_no") or "")
                match = re.search(r"表\s*E\.([1-5])", chapter, flags=re.IGNORECASE)
                if not match or match.group(1) in seen_tables:
                    continue
                seen_tables.add(match.group(1))
                tables.append(hit)
            if tables:
                return sorted(
                    tables,
                    key=lambda hit: int(re.search(r"表\s*E\.([1-5])", str(hit.get("section_path") or ""), flags=re.IGNORECASE).group(1)),
                )

        if effective_plan.intent == "basic_analysis_items":
            exact = [
                hit
                for hit in hits
                if self._is_basic_analysis_source(self._source_from_hit(hit))
                and any(
                    marker in self._hit_evidence_text(hit)
                    for marker in ("铁矿石基本分析项目", "锰矿石基本分析项目", "铬矿石基本分析项目")
                )
            ]
            if exact:
                return [max(exact, key=lambda hit: len(self._hit_evidence_text(hit)))]

        strict_validators = {
            "service_materials": self._is_service_material_source,
            "service_procedure_basis": self._is_service_procedure_source,
            "service_time_limit": self._is_service_time_limit_source,
            "projection_numeric_rule": self._is_projection_numeric_source,
            "legal_responsibility": self._is_legal_responsibility_source,
        }
        strict_validator = strict_validators.get(effective_plan.intent)
        if strict_validator:
            matched = [hit for hit in hits if strict_validator(self._source_from_hit(hit))]
            if effective_plan.intent == "service_materials":
                prepared_matches = []
                for hit in matched:
                    prepared = dict(hit)
                    if hit.get("evidence_text"):
                        prepared["quote"] = hit["evidence_text"]
                    prepared_matches.append(prepared)
                matched = prepared_matches
            if (
                effective_plan.intent in {"service_materials", "service_procedure_basis", "service_time_limit"}
                and effective_plan.candidate_title_terms
                and any(
                    term in effective_plan.normalized_query
                    for term in ("压矿", "压覆审批", "压覆矿产资源")
                )
            ):
                matched = [
                    hit
                    for hit in matched
                    if any(
                        term in str(hit.get("title") or "")
                        for term in effective_plan.candidate_title_terms
                    )
                ]
            if effective_plan.intent == "service_materials":
                attachment_hits = [
                    hit
                    for hit in matched
                    if hit.get("source_role") == "policy_attachment"
                ]
                if attachment_hits:
                    return attachment_hits[:20]
            if effective_plan.intent == "service_procedure_basis":
                section_priority = {
                    "办理基本流程": 0,
                    "办理流程": 0,
                    "办理方式": 1,
                    "申请材料提交": 2,
                }
                matched.sort(
                    key=lambda hit: min(
                        (
                            priority
                            for section, priority in section_priority.items()
                            if section in str(hit.get("section_path") or "")
                        ),
                        default=9,
                    )
                )
            if effective_plan.intent in {"projection_numeric_rule", "legal_responsibility"}:
                return matched[:1]
            return matched[:3]
        if self._is_projection_distance_question(question, effective_plan):
            selected = self._projection_comparison_hits(hits, question)
            if selected:
                return selected
        if self._is_policy_authority_question(question, effective_plan):
            selected = []
            for hit in hits:
                quote = hit.get("quote") or hit.get("text") or ""
                standard_no = hit.get("standard_no") or ""
                clause = hit.get("clause_no") or hit.get("section_path") or ""
                is_target_policy = "自然资规〔2023〕6号" in standard_no
                has_responsible_party = (
                    "自然资源部负责本级已颁发勘查许可证或采矿许可证" in quote
                    or "其他由省级自然资源主管部门负责" in quote
                    or ("自然资源主管部门" in quote and "委托矿产资源储量评审机构" in quote)
                )
                if has_responsible_party:
                    selected.append(hit)
                elif is_target_policy and ("九、" in clause or "强化矿产资源储量评审备案" in quote):
                    selected.append(hit)
                if len(selected) >= 2:
                    break
            if selected:
                return selected

        if effective_plan.intent == "related_documents" and effective_plan.focus_terms:
            anchor = max(effective_plan.focus_terms, key=len)
            selected = []
            seen_documents: set[str | None] = set()
            for hit in hits:
                context = " ".join(
                    str(hit.get(key) or "")
                    for key in ("title", "section_path", "clause_no", "quote", "text")
                )
                if anchor not in context or hit.get("document_id") in seen_documents:
                    continue
                selected.append(hit)
                seen_documents.add(hit.get("document_id"))
                if len(selected) >= 5:
                    break
            if selected:
                return selected

        top = hits[0]
        top_score = float(top.get("score") or 0)
        top_document_id = top.get("document_id")
        is_comparison_question = (
            self._is_comparison_question(question)
            or effective_plan.exhaustive_search
            or effective_plan.search_mode in {"comparison", "exhaustive"}
        )
        focused_terms = ("金矿", "岩金")
        has_focused_title_match = any(term in question for term in focused_terms) and "岩金" in str(top.get("title") or "")

        selected: list[dict] = []
        seen: set[tuple[str | None, str | None]] = set()
        seen_documents: set[str | None] = set()
        for hit in hits:
            score = float(hit.get("score") or 0)
            same_document = hit.get("document_id") == top_document_id
            if has_focused_title_match and not same_document and not is_comparison_question:
                continue
            if is_comparison_question:
                if hit.get("document_id") in seen_documents:
                    continue
            elif not same_document and score < max(0.55, top_score - 0.18):
                continue
            elif same_document and score < max(0.45, top_score - 0.28):
                continue
            key = (hit.get("standard_no"), hit.get("section_path") or hit.get("clause_no"))
            if key in seen:
                continue
            seen.add(key)
            seen_documents.add(hit.get("document_id"))
            selected.append(hit)
            limit = 5 if is_comparison_question else 3
            if len(selected) >= limit:
                break
        return selected or hits[: min(3, len(hits))]

    @staticmethod
    def _hit_evidence_text(hit: dict) -> str:
        return str(hit.get("quote") or hit.get("evidence_text") or hit.get("text") or "")

    @staticmethod
    def _is_transfer_equivalent_evidence(text: str) -> bool:
        compact = re.sub(r"\s+", "", text or "")
        return bool(
            any(re.sub(r"\s+", "", term) in compact for term in TRANSFER_EQUIVALENT_TERMS)
            and any(re.sub(r"\s+", "", term) in compact for term in TRANSFER_REPORT_OBJECT_TERMS)
            and (
                "详终" in compact
                or any(re.sub(r"\s+", "", term) in compact for term in TRANSFER_CONDITION_TERMS)
            )
        )

    @classmethod
    def _projection_comparison_hits(cls, hits: list[dict], question: str = "") -> list[dict]:
        valid: list[dict] = []
        for hit in hits:
            quote = " ".join(
                str(hit.get(key) or "")
                for key in ("evidence_text", "quote", "text")
            ).strip()
            if not hit.get("standard_no"):
                continue
            if "具体要求按" in quote and not any(term in quote for term in ("1/2", "二分之一")):
                continue
            has_ratio = any(
                term in quote
                for term in (
                    "1/2",
                    "二分之一",
                    "１／２",
                    "1/4",
                    "四分之一",
                    "１／４",
                    "2/3",
                    "1/3",
                )
            )
            has_projection = any(term in quote for term in ("外推", "尖推", "平推", "尖灭"))
            has_distance_basis = any(
                term in quote
                for term in (
                    "工程间距",
                    "基本间距",
                    "实际间距",
                    "相应间距",
                    "理论工程间距",
                )
            )
            if has_ratio and has_projection and has_distance_basis:
                prepared = dict(hit)
                if hit.get("evidence_text"):
                    prepared["quote"] = hit["evidence_text"]
                valid.append(prepared)
        if not valid:
            return []

        def document_key(hit: dict) -> str:
            return str(hit.get("document_id") or hit.get("standard_no") or "")

        def standard_key(hit: dict) -> str:
            return re.sub(r"\s+", "", str(hit.get("standard_no") or "")).upper()

        def quality(hit: dict) -> tuple[int, int, int, float]:
            standard = standard_key(hit)
            clause = str(hit.get("clause_no") or hit.get("section_path") or "")
            exact_reference = int(
                (standard == "DZ/T0338.1-2020" and clause == "6.2.2.1")
                or (standard == "DZ/T0338.2-2020" and clause == "5.4.2")
            )
            quote = cls._hit_evidence_text(hit)
            focused_type = "无限外推" if "无限外推" in question else (
                "有限外推" if "有限外推" in question else ""
            )
            return (
                exact_reference,
                int(bool(focused_type) and focused_type in quote),
                int(bool(hit.get("clause_no"))),
                float(hit.get("score") or 0.0),
            )

        best_by_document: dict[str, dict] = {}
        order: list[str] = []
        for hit in valid:
            key = document_key(hit)
            if key not in best_by_document:
                best_by_document[key] = hit
                order.append(key)
            elif quality(hit) > quality(best_by_document[key]):
                best_by_document[key] = hit

        candidates = [best_by_document[key] for key in order]
        selected: list[dict] = []
        selected_documents: set[str] = set()

        for standard_no in PROJECTION_REFERENCE_STANDARD_NUMBERS:
            normalized = re.sub(r"\s+", "", standard_no).upper()
            match = next((hit for hit in candidates if standard_key(hit) == normalized), None)
            if match:
                selected.append(match)
                selected_documents.add(document_key(match))

        seen_signatures = {
            (cls._projection_type(cls._hit_evidence_text(hit)), cls._projection_distance_bucket(cls._hit_evidence_text(hit)))
            for hit in selected
        }
        for hit in candidates:
            if document_key(hit) in selected_documents:
                continue
            signature = (
                cls._projection_type(cls._hit_evidence_text(hit)),
                cls._projection_distance_bucket(cls._hit_evidence_text(hit)),
            )
            if signature in seen_signatures:
                continue
            selected.append(hit)
            selected_documents.add(document_key(hit))
            seen_signatures.add(signature)
            if len(selected) >= 7:
                return selected

        for hit in candidates:
            if document_key(hit) in selected_documents:
                continue
            selected.append(hit)
            selected_documents.add(document_key(hit))
            if len(selected) >= 7:
                break
        return selected

    def _is_comparison_question(self, question: str) -> bool:
        return any(term in question for term in CACHEABLE_COMPARISON_TERMS)

    def _is_cacheable_question(self, question: str) -> bool:
        return False

    def _cache_key(self, question: str) -> str:
        return " ".join(question.split())

    def _evidence_quote_for_prompt(
        self,
        question: str,
        quote: str | None,
        plan: QueryPlan | None = None,
    ) -> str | None:
        effective_plan = plan or understand_query(question)
        if not quote or not (
            self._is_comparison_question(question)
            or effective_plan.search_mode in {"comparison", "exhaustive"}
        ):
            return quote
        clean = " ".join(quote.split())
        if self._is_projection_distance_question(question, effective_plan):
            return self._direct_projection_quote(clean)
        anchors = ("矿体外推", "外推", "尖推", "平推", "工程间距")
        idx = -1
        for anchor in anchors:
            idx = clean.find(anchor)
            if idx >= 0:
                break
        if idx < 0:
            return clean[:260] + ("..." if len(clean) > 260 else "")
        start = max(0, idx - 90)
        end = min(len(clean), idx + 260)
        return ("..." if start else "") + clean[start:end] + ("..." if end < len(clean) else "")

    def _is_projection_distance_question(
        self,
        question: str,
        plan: QueryPlan | None = None,
    ) -> bool:
        effective_plan = plan or understand_query(question)
        if effective_plan.intent != "projection_comparison":
            return False
        dimensions = " ".join(effective_plan.comparison_dimensions)
        return (
            any(term in question for term in PROJECTION_DISTANCE_TERMS)
            or "间距" in effective_plan.normalized_query
            or "距离" in dimensions
            or "间距" in dimensions
            or any("工程间距" in group for group in effective_plan.required_evidence_groups)
        )

    def _is_policy_authority_question(
        self,
        question: str,
        plan: QueryPlan | None = None,
    ) -> bool:
        return (plan or understand_query(question)).intent == "authority_responsibility"

    def _is_engineering_distance_source(self, source: Source) -> bool:
        quote = source.quote or ""
        if all(label in quote for label in ("坑探-穿脉", "坑探-沿脉", "钻探-走向", "钻探-倾斜")):
            return True
        context = f"{source.title} {source.chapter or ''} {quote}"
        return (
            "岩金" in source.title
            and any(term in context for term in ("表 F.1", "表F.1", "参考基本勘查工程间距"))
            and "工程间距" in context
        )

    def _is_service_material_source(self, source: Source) -> bool:
        quote = source.quote or ""
        title = source.title or ""
        if "服务指南" in title and "申请材料" in f"{source.chapter or ''} {quote}":
            return True
        if source.source_role == "policy_attachment":
            return title == "采矿权申请资料清单及要求" and (source.chapter or "").startswith("附件4")
        if "采矿权" in title and "延续" in f"{title} {source.chapter or ''} {quote}":
            if any(term in f"{source.chapter or ''} {quote}" for term in ("申请材料", "申请资料", "材料清单")):
                return True
        return (
            "自然资规〔2023〕4号" in (source.standard_no or "")
            and "采矿权申请资料清单" in quote
            and "附件4" in quote
        )

    def _is_service_procedure_source(self, source: Source) -> bool:
        context = f"{source.title} {source.chapter or ''} {source.quote or ''}"
        if "服务指南" in source.title and any(
            term in context for term in ("办理基本流程", "办理方式", "申请材料提交")
        ):
            return True
        if "采矿权" in source.title and any(term in context for term in ("审批依据", "办理流程", "申请材料")):
            return True
        return (
            "自然资规〔2023〕4号" in (source.standard_no or "")
            and "矿产资源勘查开采登记管理" in source.title
            and "采矿权" in context
            and any(term in context for term in ("申请资料", "登记管理", "附件4"))
        )

    def _is_service_time_limit_source(self, source: Source) -> bool:
        context = f"{source.title} {source.chapter or ''} {source.quote or ''}"
        return "服务指南" in source.title and "办结时限" in context and any(
            term in context for term in ("工作日", "日内", "即时办结")
        )

    def _is_projection_numeric_source(self, source: Source) -> bool:
        quote = re.sub(r"\s+", "", source.quote or "")
        return (
            (source.standard_no or "").replace(" ", "").upper() == "DZ/T0338.1-2020"
            and (source.chapter == "6.2.2.1" or "6.2.2.1" in quote or "无限外推" in quote)
            and "无限外推" in quote
            and "经验工程间距1/2尖推" in quote
        )

    def _is_legal_responsibility_source(self, source: Source) -> bool:
        quote = re.sub(r"\s+", "", source.quote or "")
        return (
            "国令第839号" in (source.standard_no or "")
            and (source.chapter == "第四十三条" or "第四十三条" in quote)
            and "矿业权人" in quote
            and "储量报告的真实性负责" in quote
            and "不得弄虚作假" in quote
        )

    def _is_basic_analysis_source(self, source: Source) -> bool:
        context = f"{source.title} {source.chapter or ''} {source.quote or ''}"
        if "基本分析" not in context or "分析项目" not in context:
            return False
        return any(
            marker in context
            for marker in ("铁矿石基本分析项目", "锰矿石基本分析项目", "铬矿石基本分析项目")
        )

    def _is_policy_authority_source(self, source: Source) -> bool:
        quote = source.quote or ""
        return (
            "自然资源部负责本级已颁发勘查许可证或采矿许可证" in quote
            or "其他由省级自然资源主管部门负责" in quote
            or ("自然资源主管部门" in quote and "委托矿产资源储量评审机构" in quote)
        )

    def _trim_source_quotes(
        self,
        question: str,
        sources: list[Source],
        plan: QueryPlan | None = None,
    ) -> list[Source]:
        effective_plan = plan or understand_query(question)
        if (
            not self._is_projection_distance_question(question, effective_plan)
            and not self._is_policy_authority_question(question, effective_plan)
            and effective_plan.intent not in {
                "projection_numeric_rule",
                "legal_responsibility",
                "service_materials",
                "exploration_to_mining_eligibility",
            }
        ):
            return sources
        trimmed = []
        for source in sources:
            item = source.model_copy()
            if self._is_policy_authority_question(question, effective_plan):
                item.quote = self._direct_policy_authority_quote(source.quote or "")
            elif effective_plan.intent == "projection_numeric_rule":
                item.quote = self._direct_infinite_projection_quote(source.quote or "")
            elif effective_plan.intent == "legal_responsibility":
                item.quote = self._direct_legal_responsibility_quote(source.quote or "")
            elif effective_plan.intent == "service_materials":
                item.quote = self._direct_service_material_quote(source.quote or "")
            elif effective_plan.intent == "exploration_to_mining_eligibility":
                item.quote = self._direct_transfer_report_quote(source.quote or "")
            else:
                item.quote = self._evidence_quote_for_prompt(question, source.quote, effective_plan)
            trimmed.append(item)
        return trimmed

    def _direct_infinite_projection_quote(self, text: str) -> str:
        clean = re.sub(r"\s+", " ", text).strip()
        infinite_match = re.search(r"(b\)\s*无限外推：.*?经验工程间距\s*1/2\s*尖推。)", clean)
        if infinite_match:
            finite_match = re.search(r"(普查阶段.*?实际工程间距\s*的\s*1/4\s*平推处理。)", clean)
            return "".join(
                match.group(1).strip()
                for match in (finite_match, infinite_match)
                if match is not None
            )
        return clean[:260] + ("..." if len(clean) > 260 else "")

    def _direct_legal_responsibility_quote(self, text: str) -> str:
        clean = re.sub(r"\s+", " ", text).strip()
        match = re.search(r"(矿业权人应当对其报送的储量报告的真实性负责，不得弄虚作假。)", clean)
        if match:
            return match.group(1)
        return clean[:260] + ("..." if len(clean) > 260 else "")

    def _direct_service_material_quote(self, text: str) -> str:
        clean = re.sub(r"\s+", " ", text).strip()
        if "申请材料目录" in clean and "矿业权出让收益" in clean:
            return clean[:1400] + ("..." if len(clean) > 1400 else "")
        match = re.search(
            r"(自然资源部负责的矿业权.*?按照本通知附件2探矿权申请资料清单及要求、附件4采矿权申请资料清单及要求执行。)",
            clean,
        )
        if match:
            return match.group(1)
        return clean[:260] + ("..." if len(clean) > 260 else "")

    @staticmethod
    def _post_filing_license_steps_answer(sources: list[Source]) -> str | None:
        candidates = [
            source
            for source in sources
            if "采矿权变更（续期）登记临时服务指南" in source.title
            and "申请材料" in f"{source.chapter or ''} {source.quote or ''}"
            and "矿产资源储量评审备案文件" in (source.quote or "")
            and "矿业权出让收益" in (source.quote or "")
        ]
        if not candidates:
            return None
        source = max(candidates, key=lambda item: len(item.quote or ""))
        return "\n".join(
            [
                "**结论：资源储量评审备案完成后，还需要继续办理采矿权变更（续期）登记申请。**",
                "",
                f"根据《{source.title}》的申请材料目录，需要处理以下 5 项：",
                "",
                "1. **填写并提交采矿权登记申请书。**",
                "2. **确认企业法人营业执照信息可被在线核验。** 该材料由登记机关通过政府网站核查，申请人无需另行提交。",
                "3. **按适用情形提交不动产权证书（采矿权）或原采矿许可证。**",
                "4. **提交矿产资源储量评审备案文件或指南要求的矿山储量年报。** 非油气续期通常提交当年或上一年度矿山储量年报；累计查明资源量发生重大变化时提交评审备案文件。",
                "5. **完成矿业权出让收益（价款）缴纳或有偿处置，并取得相应证明材料。** 可使用缴款通知书、分期缴款批复、成交确认书、出让合同、缴纳票据或征收机关书面意见等证明。",
                "",
                "以上清单适用于该自然资源部采矿权变更（续期）办事指南覆盖的情形；其他登记类型或地方发证事项应以对应办事指南为准。",
            ]
        )

    def _direct_policy_authority_quote(self, text: str) -> str:
        clean = re.sub(r"\s+", " ", text).strip()
        patterns = [
            r"(自然资源部负责本级已颁发勘查许可证或采矿许可证的矿产资源储量评审备案工作，其他由省级自然资源主管部门负责。)",
            r"(自然资源主管部门可以委托矿产资源储量评审机构根据评审备案范围和权限组织开展评审备案工作，相关费用按照国家有关规定执行。)",
        ]
        for pattern in patterns:
            match = re.search(pattern, clean)
            if match:
                return match.group(1)
        anchors = ("自然资源部负责", "省级自然资源主管部门负责", "委托矿产资源储量评审机构")
        for anchor in anchors:
            idx = clean.find(anchor)
            if idx >= 0:
                end = clean.find("。", idx)
                if end >= 0:
                    return clean[idx : end + 1]
                return clean[idx : idx + 220].rstrip()
        return clean[:260] + ("..." if len(clean) > 260 else "")

    def _direct_transfer_report_quote(self, text: str) -> str:
        clean = re.sub(r"\s+", " ", text).strip()
        patterns = [
            r"(探矿权转采矿权，应当依据经评审备案的矿产资源储量报告。资源储量规模为大型的非煤矿山、大中型煤矿应当达到勘探程度，其他矿山应当达到详查（含）以上程度。)",
            r"(矿产资源储量核实报告不能替代探矿权转采矿权时应提交的地质勘查报告。)",
            r"((?:卤水.*?|深层固体盐类.*?|详查报告.*?)(?:可作为矿山设计开采依据|供矿山设计开采|作为矿山建设设计的依据).*?。)",
        ]
        for pattern in patterns:
            match = re.search(pattern, clean)
            if match:
                return match.group(1)
        for sentence in re.split(r"(?<=[。！？；;])\s*", clean):
            if self._is_transfer_equivalent_evidence(sentence):
                return sentence[:700].strip()
        return clean[:260] + ("..." if len(clean) > 260 else "")

    def _direct_projection_quote(self, text: str) -> str:
        normalized = re.sub(r"\s+", " ", text).strip()
        patterns = [
            r"(6\.2\.2\.1\s*采用几何法时.*?a\)有限外推：.*?b\)无限外推：.*?经验工程间距\s*1/2\s*尖推。)",
            r"(5\.4\.2\s*相邻的两个工程一个见矿.*?推断资源量工程间距.*?1/2尖推。)",
            r"(8\.3\.4\.5\.3\s*无限外推：.*?1/2尖推、1/4平推。)",
            r"(8\.2\.3\.2\s*无限外推原则：.*?1/2尖推或1/4平推。)",
            r"(J\.3\.2\.1\s*无限外推.*?1/2尖\s*推或1/4平推。)",
            r"(8\.3\.4\.5\.2\s*有限外推：\s*a\).*?实际间距1/2尖推、1/4平推。)",
            r"(8\.2\.3\.1\s*有限外推原则：.*?实际工程间距2/3尖推或1/3平推。)",
            r"(G\.1\.3\s*应根据矿体.*?基本勘查工程间距的四分之一平推。)",
            r"(a\)当见矿工程与相邻工程.*?按推断资源量.*?1/2尖推或1/4平推推断\s*资源量。.*?b\)当见矿工程与相邻工程.*?实际.*?1/2尖推或1/4平推推断资源量)",
            r"(\d+(?:\.\d+)+\s*矿体外推应合理.*?a\).*?)(?=b\)|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, normalized)
            if match:
                quote = match.group(1).strip()
                return quote[:520].rstrip() + ("..." if len(quote) > 520 else "")

        sentences = re.split(r"(?<=[。；;])\s*", text)
        candidates = []
        seen = set()
        for index, sentence in enumerate(sentences):
            sentence = sentence.strip()
            if not sentence:
                continue
            has_ratio = any(term in sentence for term in ("1/2", "二分之一", "１／２", "1/4", "四分之一", "１／４"))
            has_projection = any(term in sentence for term in ("外推", "尖推", "平推", "尖灭"))
            has_distance = "工程间距" in sentence or "基本间距" in sentence or "实际间距" in sentence
            if has_ratio and has_projection and has_distance:
                previous = sentences[index - 1].strip() if index > 0 else ""
                if previous and ("有限外推" in previous or re.search(r"\d+(?:\.\d+)+", previous)):
                    candidate = previous + sentence
                else:
                    candidate = sentence
                if candidate not in seen:
                    seen.add(candidate)
                    candidates.append(candidate)
            if len(candidates) >= 2:
                break
        if candidates:
            quote = "".join(candidates)
            return quote[:520].rstrip() + ("..." if len(quote) > 520 else "")
        anchors = ("5.4.2", "有限外推", "外推", "尖推", "平推", "工程间距")
        idx = -1
        for anchor in anchors:
            idx = text.find(anchor)
            if idx >= 0:
                break
        if idx < 0:
            return text[:180] + ("..." if len(text) > 180 else "")
        end_candidates = [pos for pos in (text.find("。", idx), text.find("；", idx), text.find("; ", idx)) if pos >= 0]
        end = min(end_candidates) + 1 if end_candidates else min(len(text), idx + 220)
        next_period = text.find("。", end)
        if next_period >= 0 and next_period - idx < 320:
            end = next_period + 1
        return text[idx:end].strip()

    def _is_standard_selection_question(self, question: str) -> bool:
        return any(
            term in question
            for term in (
                "使用哪个标准",
                "用哪个标准",
                "适用哪个标准",
                "采用哪个标准",
                "使用哪个规范",
                "用哪个规范",
                "适用哪个规范",
                "采用哪个规范",
                "应该使用",
                "应该用",
            )
        )

    def _fast_answer(
        self,
        question: str,
        sources: list[Source],
        plan: QueryPlan | None = None,
    ) -> str | None:
        effective_plan = plan or understand_query(question)
        definition_answer = self._definition_answer(effective_plan, sources)
        if definition_answer:
            return definition_answer
        engineering_answer = self._engineering_distance_answer(effective_plan, sources)
        if engineering_answer:
            return engineering_answer

        if effective_plan.intent == "technical_requirement_sufficiency":
            actual_level = actual_level_from_sufficiency_question(question)
            explicit_required_level = required_level_from_sufficiency_question(question)
            stage_source = next(
                (
                    source
                    for source in sources
                    if self._is_technical_stage_requirement_text(source.quote or "", question)
                ),
                None,
            )
            stage_required_level = max(
                (
                    level
                    for level in levels_in_text(stage_source.quote if stage_source else "")
                ),
                key=lambda item: item.rank,
                default=None,
            )
            required_level = explicit_required_level or stage_required_level
            actual_source = next(
                (
                    source
                    for source in sources
                    if actual_level and self._is_technical_test_level_source(source, actual_level)
                ),
                None,
            )
            if (
                actual_level
                and required_level
                and actual_source
                and (stage_source or explicit_required_level)
            ):
                satisfies = level_covers(actual_level, required_level)
                relation = "高于" if actual_level.rank > required_level.rank else "等同于"
                chain = " -> ".join(
                    item.label
                    for item in MINERAL_PROCESSING_TEST_LEVELS
                    if item.rank <= actual_level.rank
                )
                lines = [
                    f"**结论：{'满足' if satisfies else '不满足'}（仅作试验等级满足判断）。**",
                    "",
                    f"- **用户所述试验等级**：{actual_level.label}。",
                    f"- **对应要求等级**：{required_level.label}。",
                    f"- **等级比较**：{actual_level.label}{relation}{required_level.label}；"
                    + ("因此可以覆盖该要求。" if satisfies else f"因此仍需达到{required_level.label}或更高等级。"),
                    f"- **等级依据**：{actual_source.standard_no or actual_level.source_standard_no}《{actual_source.title}》"
                    f"（{actual_source.chapter or actual_level.source_clause}，{actual_level.label}等级条款）。",
                ]
                if stage_source:
                    lines.append(
                        f"- **阶段依据**：{stage_source.standard_no or '未知标准号'}《{stage_source.title}》"
                        f"（{stage_source.chapter or '相关条款'}）：{stage_source.quote}"
                    )
                if chain:
                    lines.append(f"- **本次比较的等级链**：{chain}。")
                lines.append(
                    "- **判断范围**：本结论只比较标准中的试验等级；试验是否满足该等级的具体技术条件，属于单独的试验符合性核验。"
                )
                return "\n".join(lines)

        if effective_plan.intent == "projection_numeric_rule":
            source = next((item for item in sources if self._is_projection_numeric_source(item)), None)
            if source:
                return "\n".join(
                    [
                        "**结论：无限外推采用经验工程间距的 1/2 尖推，不是 1/4 平推。**",
                        "",
                        f"- **依据文件**：{source.standard_no or '未知标准号'}《{source.title}》",
                        f"- **依据条款**：{source.chapter or '6.2.2.1'}",
                        f"- **直接依据**：{source.quote}",
                    ]
                )

        if effective_plan.intent == "legal_responsibility":
            source = next((item for item in sources if self._is_legal_responsibility_source(item)), None)
            if source:
                return "\n".join(
                    [
                        "**资源储量报告的真实性由报送该报告的矿业权人负责。**",
                        "",
                        f"- **依据文件**：{source.standard_no or '未知文号'}《{source.title}》",
                        f"- **依据条款**：{source.chapter or '第四十三条'}",
                        f"- **直接依据**：{source.quote}",
                    ]
                )

        if effective_plan.intent == "service_materials":
            if is_post_filing_license_steps_query(effective_plan.normalized_query):
                transition_answer = self._post_filing_license_steps_answer(sources)
                if transition_answer:
                    return transition_answer
            attachment_sources = [
                item
                for item in sources
                if item.source_role == "policy_attachment"
                and item.title == "采矿权申请资料清单及要求"
            ]
            if attachment_sources:
                application_label = None
                change_section = None
                classification = effective_plan.classification
                if classification:
                    application_label = {
                        "new": "新立",
                        "renewal": "延续",
                        "change": "变更",
                        "cancellation": "注销",
                    }.get(classification.application_type or "")
                    change_section = SERVICE_CHANGE_SECTIONS.get(
                        classification.change_subtype or ""
                    )
                if any(term in effective_plan.normalized_query for term in ("延续", "续期")):
                    application_label = application_label or "延续"
                elif "注销" in effective_plan.normalized_query:
                    application_label = application_label or "注销"
                elif any(term in effective_plan.normalized_query for term in ("首次", "新立")):
                    application_label = application_label or "新立"
                elif "变更" in effective_plan.normalized_query or any(
                    term in effective_plan.normalized_query for term in ("转让", "转移")
                ):
                    application_label = application_label or "变更"

                if application_label is None:
                    return None

                def material_sequence(item: Source) -> int:
                    match = re.search(r"材料\s*(\d+)", item.chapter or "")
                    return int(match.group(1)) if match else 999

                if application_label:
                    prefix = f"附件4 > {application_label}"
                    if application_label == "变更" and change_section:
                        prefix = f"附件4 > 变更 > {change_section}"
                    attachment_sources = [
                        item
                        for item in attachment_sources
                        if (item.chapter or "").startswith(prefix)
                    ]
                if not attachment_sources:
                    return None
                attachment_sources.sort(key=material_sequence)
                application_name = (
                    f"变更（{change_section}）"
                    if application_label == "变更" and change_section
                    else application_label
                )
                lines = [
                    f"采矿权{application_name}申请应按 **自然资规〔2023〕4号附件4《采矿权申请资料清单及要求》** 提交以下材料：",
                    "",
                ]
                lines.extend(f"- {item.quote}" for item in attachment_sources)
                lines.extend(
                    [
                        "",
                        "表中“要求”栏的特殊规定优先于▲/—标记；部分材料还区分油气、非油气或由主管部门通过系统报送。",
                    ]
                )
                return "\n".join(lines)
            guide_sources = [
                item
                for item in sources
                if "服务指南" in item.title
                and "申请材料" in f"{item.chapter or ''} {item.quote or ''}"
            ]
            guide_source = max(
                guide_sources,
                key=lambda item: (
                    ">" in (item.chapter or ""),
                    len(item.quote or ""),
                ),
                default=None,
            )
            if guide_source:
                return "\n".join(
                    [
                        f"应按《{guide_source.title}》的申请材料目录提交材料。",
                        "",
                        f"- **申请材料**：{guide_source.quote}",
                        f"- **官方来源**：{guide_source.url or '未提供'}",
                    ]
                )
            source = next((item for item in sources if self._is_service_material_source(item)), None)
            if source:
                return "\n".join(
                    [
                        "采矿权延续登记的申请资料应按 **自然资规〔2023〕4号附件4《采矿权申请资料清单及要求》** 执行。",
                        "",
                        f"- **依据文件**：{source.standard_no or '自然资规〔2023〕4号'}《{source.title}》",
                        f"- **依据条款**：{source.chapter or '三、精简矿业权申请资料'}",
                        f"- **直接依据**：{source.quote}",
                        "- **当前限制**：本次检索只命中了父政策中的附件指引，未召回附件4的结构化材料记录；不能据此推断附件内容不存在。",
                    ]
                )

        if effective_plan.intent == "service_procedure_basis":
            source = next((item for item in sources if self._is_service_procedure_source(item)), None)
            if source:
                if "服务指南" in source.title:
                    return "\n".join(
                        [
                            f"《{source.title}》规定的办理要求如下：",
                            "",
                            f"- **{source.chapter or '办理流程'}**：{source.quote}",
                            f"- **官方来源**：{source.url or '未提供'}",
                        ]
                    )
                return "\n".join(
                    [
                        "采矿权登记办理应首先依据 **自然资规〔2023〕4号《自然资源部关于进一步完善矿产资源勘查开采登记管理的通知》**。",
                        "",
                        f"- **依据条款**：{source.chapter or '相关条款'}",
                        f"- **直接依据**：{source.quote}",
                        "- **说明**：具体办理类型还应结合附件4的申请资料清单及对应办事指南确定。",
                    ]
                )

        if effective_plan.intent == "service_time_limit":
            source = next((item for item in sources if self._is_service_time_limit_source(item)), None)
            if source:
                return "\n".join(
                    [
                        f"《{source.title}》的办结时限为：{source.quote}",
                        "",
                        f"- **官方来源**：{source.url or '未提供'}",
                    ]
                )

        if self._is_policy_authority_question(question, effective_plan) and sources:
            authority_source = next((source for source in sources if self._is_policy_authority_source(source)), None)
            if authority_source:
                issuer = effective_plan.license_issuer_level
                granting = effective_plan.mining_right_granting_level
                if issuer == "ministry":
                    conclusion = "应由 **自然资源部** 负责矿产资源储量评审备案。"
                elif issuer == "province":
                    conclusion = "应向 **省级自然资源主管部门** 申请矿产资源储量评审备案。"
                else:
                    conclusion = (
                        "评审备案机关取决于当前有效勘查许可证或采矿许可证的 **颁发机关**："
                        "自然资源部本级颁发的，由自然资源部负责；其他由省级自然资源主管部门负责。"
                    )
                lines = [
                    conclusion,
                    "",
                    f"- **依据文件**：{authority_source.standard_no or '未知文号'}《{authority_source.title}》",
                    f"- **依据条款**：{authority_source.chapter or '相关条款'}",
                    f"- **直接依据**：{authority_source.quote}",
                ]
                if issuer == "unknown":
                    lines.append("- **需要确认**：请查看现有有效许可证落款或发证机关，而不是仅按矿种、矿山规模判断。")
                if granting != "unknown":
                    lines.append(
                        "- **权限区分**：矿业权出让或配置权限与储量评审备案权限不是同一概念；"
                        "本条以许可证颁发机关为判断依据。"
                    )
                delegated_source = next(
                    (
                        source
                        for source in sources
                        if source is not authority_source and source.quote and "委托矿产资源储量评审机构" in source.quote
                    ),
                    None,
                )
                if delegated_source:
                    lines.extend(
                        [
                            "",
                            "补充说明：自然资源主管部门可以委托矿产资源储量评审机构按评审备案范围和权限组织评审，"
                            "但责任主体仍按上述条款确定。",
                        ]
                    )
                return "\n".join(lines)

        if effective_plan.intent == "exploration_to_mining_eligibility":
            policy_source = next(
                (
                    source
                    for source in sources
                    if "探矿权转采矿权" in (source.quote or "")
                    and "经评审备案" in (source.quote or "")
                    and "详查" in (source.quote or "")
                ),
                None,
            )
            if policy_source:
                lines = [
                    "**不能简单理解为“只要文件名是详查报告就可以转采”。**",
                    "",
                    "现行政策要求同时满足两个条件：",
                    "- 转采所依据的文件应是 **经评审备案的矿产资源储量报告**；",
                    "- 勘查程度方面，大型非煤矿山和大中型煤矿应达到 **勘探程度**，其他矿山应达到 **详查（含）以上程度**。",
                    "",
                    f"- **直接依据**：{policy_source.standard_no or '未知文号'}《{policy_source.title}》"
                    f"（{policy_source.chapter or '相关条款'}）：{policy_source.quote}",
                ]
                limiting_source = next(
                    (
                        source
                        for source in sources
                        if "不能替代探矿权转采矿权" in (source.quote or "")
                    ),
                    None,
                )
                if limiting_source:
                    lines.extend(
                        [
                            "",
                            "**报告类型限制**：矿产资源储量核实报告不能替代转采时应提交的地质勘查报告。",
                            f"- **限制依据**：{limiting_source.standard_no or '未知标准号'}《{limiting_source.title}》"
                            f"（{limiting_source.chapter or '相关条款'}）：{limiting_source.quote}",
                        ]
                    )
                special_sources = [
                    source
                    for source in sources
                    if self._is_transfer_equivalent_evidence(source.quote or "")
                ]
                if special_sources:
                    lines.extend(["", "**分矿种特殊规定**："])
                    for source in special_sources[:4]:
                        lines.append(
                            f"- **{source.standard_no or '未知标准号'}《{source.title}》**"
                            f"（{source.chapter or '相关条款'}）：{source.quote}"
                        )
                    lines.append(
                        "上述“可作为/供矿山设计开采”及“作为矿山建设设计依据”等受控表述，"
                        "在本项目业务语义中属于满足条款条件时可以转采的正向依据。"
                    )
                return "\n".join(lines)

        if effective_plan.intent == "companion_resource_type":
            clauses = {source.chapter: source for source in sources if source.chapter in {"9.2", "9.3", "9.4"}}
            if all(clause in clauses for clause in ("9.2", "9.3", "9.4")):
                return "\n".join(
                    [
                        "根据 **GB/T 25283-2023《矿产资源综合勘查评价规范》**，伴生矿产资源量类型按分析方式和研究程度确定：",
                        "",
                        "- **进行了基本分析且研究程度满足要求**：资源储量类型可以与主要矿产相同。研究内容包括地质研究程度、矿石加工选冶试验研究程度和可行性评价。",
                        "- **进行了基本分析但未满足上述研究要求**：应降低资源储量类型。",
                        "- **只进行了组合分析、未做基本分析**：划为推断资源量。",
                        "",
                        f"- **9.2 原文**：{clauses['9.2'].quote}",
                        f"- **9.3 原文**：{clauses['9.3'].quote}",
                        f"- **9.4 原文**：{clauses['9.4'].quote}",
                    ]
                )

        if effective_plan.intent == "exploration_type_factors":
            tables = [source for source in sources if re.search(r"表\s*E\.[1-5]", source.chapter or "", flags=re.IGNORECASE)]
            if tables:
                tables.sort(
                    key=lambda source: int(re.search(r"表\s*E\.([1-5])", source.chapter or "", flags=re.IGNORECASE).group(1))
                )
                return "\n\n".join(
                    [
                        "根据 **DZ/T 0205-2020《矿产地质勘查规范 岩金》附录 E.1**，岩金矿床勘查类型按矿体规模、形态变化程度、厚度稳定程度、构造与脉岩影响程度、主要有用组分分布均匀程度五项因素划分。",
                        *(source.quote or source.chapter or "" for source in tables),
                    ]
                )

        if effective_plan.intent == "basic_analysis_items":
            source = next((item for item in sources if self._is_basic_analysis_source(item)), None)
            if source:
                if "铁矿" in effective_plan.normalized_query:
                    conclusion = (
                        "- **磁性铁矿石**以及使用磁性铁含量圈定矿体的其他矿石：分析 **TFe、mFe**。\n"
                        "- **赤铁矿石、褐铁矿石、菱铁矿石**：分析 **TFe**。"
                    )
                else:
                    conclusion = source.quote or ""
                return "\n".join(
                    [
                        f"根据 **{source.standard_no or '相关标准'}《{source.title}》**，基本分析项目如下：",
                        "",
                        conclusion,
                        "",
                        f"- **依据条款**：{source.chapter or '相关条款'}",
                        f"- **直接依据**：{source.quote}",
                    ]
                )

        if effective_plan.intent == "standard_selection" and sources:
            source = sources[0]
            return (
                f"根据当前知识库和官方标准目录，建议使用 **{source.standard_no or '未知标准号'}"
                f"《{source.title}》**。\n\n"
                f"- **依据**：标准目录命中 `{source.standard_no or '未知标准号'}`，标准名称为《{source.title}》。\n"
                f"- **官方平台**：{source.source_platform or '官方标准平台'}。\n"
                "- **注意**：如果问题涉及具体技术条款，还应继续查询该标准正文中的对应章节或表格。"
            )

        if (
            self._is_projection_distance_question(question, effective_plan)
            and (self._is_comparison_question(question) or effective_plan.search_mode in {"comparison", "exhaustive"})
            and sources
        ):
            groups: dict[str, dict[str, list[tuple[Source, str]]]] = {}
            for source in sources:
                quote = self._evidence_quote_for_prompt(question, source.quote, effective_plan) or ""
                projection_type = self._projection_type(quote)
                bucket = self._projection_distance_bucket(quote)
                groups.setdefault(projection_type, {}).setdefault(bucket, []).append((source, quote))

            lines = [
                "存在不一致。不同标准对矿体外推时采用的“距离基准”并不完全相同，主要差异如下：",
                "",
            ]
            if "无限外推" in question and "有限外推" in groups:
                lines.extend(
                    [
                        "以下先列无限外推的直接规定，再单列有限外推对照。有限外推条款用于说明距离基准差异，不作为无限外推条款引用。",
                        "",
                    ]
                )
            index = 1
            type_order = ("无限外推", "有限外推", "有限与无限外推综合条款", "外推规则")
            for projection_type in type_order:
                type_groups = groups.get(projection_type) or {}
                if not type_groups:
                    continue
                type_label = projection_type
                if "无限外推" in question and projection_type == "有限外推":
                    type_label = "有限外推对照"
                lines.extend([f"**{type_label}**", ""])
                for bucket, items in type_groups.items():
                    lines.append(f"{index}. **{bucket}**")
                    for source, quote in items:
                        lines.append(
                            f"   - **{source.standard_no or '未知标准号'}《{source.title}》**"
                            f"（{source.chapter or '相关章节'}）：{quote}"
                        )
                    lines.append("")
                    index += 1
            lines.append(
                "结论：回答这类问题时，必须同时说明外推类型，以及比例所作用的“实际工程间距”"
                "“基本工程间距”“推断资源量工程间距”或“经验工程间距”，不能只给出1/2、1/4等比例。"
            )
            return "\n".join(lines).strip()

        if (
            effective_plan.intent == "projection_comparison"
            and (self._is_comparison_question(question) or effective_plan.search_mode in {"comparison", "exhaustive"})
            and sources
        ):
            lines = [
                "现有知识库证据显示，不同标准对“矿体外推”的规定存在不一致，主要体现在外推基准、外推比例、特殊情形和规定详略上。",
                "",
                "主要差异如下：",
            ]
            for index, source in enumerate(sources, start=1):
                quote = self._evidence_quote_for_prompt(question, source.quote, effective_plan) or ""
                lines.append(
                    f"{index}. **{source.standard_no or '未知标准号'}《{source.title}》**"
                    f"（{source.chapter or '相关章节'}）：{quote}"
                )
            lines.extend(
                [
                    "",
                    "初步判断：这些差异不一定是法律意义上的冲突，更可能是不同矿种、矿体形态和勘查控制程度下的专门化规定。实际使用时应优先采用对应矿种的现行勘查规范；跨矿种类比时，需要明确说明采用的外推基准和比例。",
                ]
            )
            return "\n".join(lines)

        return None

    @staticmethod
    def _definition_term_from_text(text: str, plan: QueryPlan) -> str | None:
        clean = re.sub(r"\s+", " ", text or "").strip()
        for term in sorted(plan.definition_slots, key=len, reverse=True):
            if re.search(
                rf"(?:^|\s)\d+(?:\.\d+)+\s+{re.escape(term)}(?=\s|[:：]|$)",
                clean,
            ):
                return term
        return None

    def _definition_term_from_source(self, source: Source, plan: QueryPlan) -> str | None:
        return self._definition_term_from_text(source.quote or "", plan)

    def _definition_answer(self, plan: QueryPlan, sources: list[Source]) -> str | None:
        if plan.intent != "definition_explanation" or not plan.definition_slots:
            return None
        by_term: dict[str, Source] = {}
        for source in sources:
            term = self._definition_term_from_source(source, plan)
            if term and term not in by_term:
                by_term[term] = source
        if not set(plan.definition_slots).issubset(by_term):
            return None

        lines: list[str] = []
        if plan.definition_mode == "compound" and plan.target_terms:
            target = "、".join(plan.target_terms)
            lines.extend(
                [
                    f"**“{target}”在本次采用的现行分类标准中没有作为同名、独立术语给出定义。**",
                    "",
                    "应分别核验其组成概念：",
                    "",
                ]
            )
        elif len(plan.definition_slots) == 1:
            lines.extend([f"**{plan.definition_slots[0]}的标准定义如下：**", ""])
        else:
            lines.extend(["**相关术语的标准定义如下：**", ""])

        for index, term in enumerate(plan.definition_slots, start=1):
            source = by_term[term]
            prefix = f"{index}. " if len(plan.definition_slots) > 1 else ""
            lines.extend(
                [
                    f"{prefix}**{term}**",
                    f"   - **依据**：{source.standard_no or '未知标准号'}《{source.title}》"
                    f"（{source.chapter or '术语和定义'}）",
                    f"   - **原文**：{source.quote}",
                    "",
                ]
            )

        if plan.definition_mode == "compound" and set(plan.definition_slots) == {"资源量", "储量"}:
            lines.extend(
                [
                    "**关系说明**：日常业务文件中的“资源储量”常作为“资源量”和“储量”的总括表达；"
                    "在专业引用中应继续区分这两个标准术语。",
                ]
            )
        return "\n".join(lines).strip()

    def _definition_max_tokens(self, sources: list[Source], plan: QueryPlan) -> int:
        quote_chars = sum(
            len(source.quote or "")
            for source in sources
            if self._definition_term_from_source(source, plan)
        )
        requested = 650 + 1.5 * quote_chars + 100 * max(1, len(plan.definition_slots))
        bounded = min(float(self.settings.definition_answer_max_tokens), max(1000.0, requested))
        return int(math.ceil(bounded / 100.0) * 100)

    @staticmethod
    def _should_recommend_deep(plan: QueryPlan, question: str) -> bool:
        if plan.search_mode in {"comparison", "exhaustive"} or plan.exhaustive_search:
            return True
        if plan.intent in {"projection_comparison", "clause_comparison", "cross_document_audit"}:
            return True
        return any(
            term in question
            for term in (
                "逐一检查",
                "逐项对比",
                "全量检查",
                "所有标准",
                "各类标准",
                "各类规范",
                "分矿种规范",
                "哪些规范与",
                "哪些标准与",
                "是否存在不一致",
                "冲突检查",
            )
        )

    def _engineering_distance_answer(self, plan: QueryPlan, sources: list[Source]) -> str | None:
        if plan.intent != "engineering_distance_lookup" or not plan.target_exploration_type:
            return None
        labels = {
            "坑探-穿脉": "穿脉",
            "坑探-沿脉": "沿脉",
            "钻探-走向": "走向",
            "钻探-倾斜": "倾斜",
        }
        distance_pattern = r"(\d+(?:\.\d+)?\s*[~～-]\s*\d+(?:\.\d+)?)\s*m?"
        for source in sources:
            quote = source.quote or ""
            if f"{plan.target_exploration_type}类型" not in quote:
                continue
            values: dict[str, str] = {}
            for evidence_label, display_label in labels.items():
                match = re.search(rf"{re.escape(evidence_label)}\s*{distance_pattern}", quote)
                if match:
                    values[display_label] = re.sub(r"\s*[~～-]\s*", "～", match.group(1))
            if len(values) != len(labels):
                continue

            chapter = source.chapter or "附录F表F.1"
            return "\n".join(
                [
                    f"根据 **{source.standard_no or '未知标准号'}《{source.title}》**（{chapter}），"
                    f"金矿（岩金）勘查 **{plan.target_exploration_type}类型**的参考基本勘查工程间距为：",
                    "",
                    f"- **坑探**：穿脉 {values['穿脉']} m；沿脉 {values['沿脉']} m",
                    f"- **钻探**：走向 {values['走向']} m；倾斜 {values['倾斜']} m",
                    "",
                    "该表给出的是控制资源量勘查工程间距的参考值。",
                ]
            )
        return None

    @staticmethod
    def _projection_type(quote: str) -> str:
        has_infinite = "无限外推" in quote or "见矿工程向外再没有工程控制" in quote
        has_finite = "有限外推" in quote or "相邻的两个工程一个见矿" in quote
        if has_infinite and not has_finite:
            return "无限外推"
        if has_finite and not has_infinite:
            return "有限外推"
        if has_infinite and has_finite:
            return "有限与无限外推综合条款"
        return "外推规则"

    @staticmethod
    def _projection_distance_bucket(quote: str) -> str:
        if "经验工程间距" in quote:
            return "以拟推资源量类型的经验工程间距为外推依据"
        if "理论工程" in quote or "理论工程间距" in quote:
            if "实际间距" in quote or "实际工程间距" in quote:
                return "按理论工程间距与实际间距分情形"
            return "以理论工程间距为外推依据"
        if "推断资源量" in quote and "实际" in quote and "工程间距" in quote:
            return "按推断资源量工程间距与实际工程间距分情形"
        if "推断资源量" in quote and "工程间距" in quote:
            return "以推断资源量工程间距为外推依据"
        if "基本" in quote and "工程间距" in quote:
            return "以基本工程间距为外推依据"
        if "实际工程间距" in quote or "实际 工程间距" in quote:
            return "以实际工程间距为外推依据"
        if "同类型资源量" in quote or "相应工程间距" in quote or "相应 工程间距" in quote:
            return "以同类型资源量/相应工程间距为外推依据"
        return "其他或需要结合上下文判断"

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
            source_platform=hit.get("source_platform"),
            source_role=hit.get("source_role"),
        )

    def _append_source_links(self, answer: str, sources: list[Source]) -> str:
        lines = []
        seen: set[tuple[str | None, str | None]] = set()
        for source in sources:
            if not source.url:
                continue
            key = (source.standard_no, source.url)
            if key in seen:
                continue
            seen.add(key)
            label = (
                f"{source.standard_no}《{source.title}》"
                if source.standard_no
                else f"《{source.title}》"
            )
            lines.append(f"- [{label}]({source.url})")
        if not lines:
            return answer
        return answer.rstrip() + "\n\n来源：\n" + "\n".join(lines)

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

    def _evidence_summary_answer(self, sources: list[Source]) -> str:
        lines = ["根据当前已审查通过的知识库证据：", ""]
        for index, source in enumerate(sources, start=1):
            lines.append(
                f"{index}. **{source.standard_no or '未知文号'}《{source.title}》**"
                f"（{source.chapter or '相关条款'}）：{source.quote or '当前仅有目录级证据。'}"
            )
        lines.extend(["", "以上仅概括已检索到的直接证据，具体适用时还应结合矿种、阶段和条款条件判断。"])
        return "\n".join(lines)
