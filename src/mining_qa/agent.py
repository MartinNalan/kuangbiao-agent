import json
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
from .query_understanding import QueryPlan, normalize_user_query, understand_query
from .retrieval_planner import RetrievalPlanner
from .retrieval_trace import RetrievalTraceLogger
from .schemas import AskRequest, AskResponse, Limitations, RetrievalStats, Source
from .web_supplement import WebSupplement


ANSWER_CACHE_ENABLED = False
ANSWER_CACHE: dict[str, AskResponse] = {}
CACHEABLE_COMPARISON_TERMS = ("不一致", "差异", "不同", "比较", "列举", "哪些标准", "哪些规范")
PROJECTION_DISTANCE_TERMS = ("外推所依据的距离", "外推依据", "外推距离", "依据的距离")
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
        base_plan = understand_query(question)
        planner_result = await self.planner.plan(question, base_plan)
        plan = planner_result.plan
        rounds = max(1, min(2, int(self.settings.max_retrieval_rounds)))
        merged_hits: list[dict] = []
        kb_results = []
        rerank_result: RerankResult | None = None
        reranker_ms = 0.0
        knowledge_ms = 0.0

        for retrieval_round in range(1, rounds + 1):
            kb_started = perf_counter()
            kb_result = await self.knowledge.search(
                question,
                filters,
                plan,
                retrieval_round=retrieval_round,
            )
            knowledge_ms += (perf_counter() - kb_started) * 1000
            kb_results.append(kb_result)
            merged_hits = self._merge_hits(merged_hits, kb_result.results)

            if not self.reranker.needs_model(plan):
                break

            rerank_result = await self.reranker.judge(question, plan, merged_hits)
            reranker_ms += rerank_result.elapsed_ms
            if rerank_result.sufficient or retrieval_round >= rounds:
                break
            refined_plan = self._refined_plan(plan, rerank_result)
            if refined_plan.retrieval_query == plan.retrieval_query:
                break
            plan = refined_plan

        kb_result = kb_results[-1]
        if self.reranker.needs_model(plan):
            if rerank_result is None:
                rerank_result = await self.reranker.judge(question, plan, merged_hits)
                reranker_ms += rerank_result.elapsed_ms
            evidence_hits = list(rerank_result.hits)
        else:
            evidence_hits = self._select_evidence_hits(merged_hits, question, plan)
        sources = [self._source_from_hit(hit) for hit in evidence_hits]
        sources = self._trim_source_quotes(question, sources, plan)
        if rerank_result is not None and self.reranker.needs_model(plan):
            has_usable_evidence = rerank_result.sufficient
            has_clause_evidence = rerank_result.sufficient and bool(sources)
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
        if len(kb_results) > 1:
            notes.append("首轮证据不足，已按证据缺口执行第二轮受控检索。")
        if planner_result.error:
            notes.append("查询规划器不可用，本次已使用确定性理解方案降级检索。")
        if rerank_result and rerank_result.error:
            notes.append("证据审查器不可用，本次已使用证据关系组进行确定性审查。")

        retrieval = RetrievalStats(
            full_text_hits=sum(int(result.retrieval.get("full_text_hits", 0)) for result in kb_results),
            vector_hits=sum(int(result.retrieval.get("vector_hits", 0)) for result in kb_results),
            graph_hits=sum(int(result.retrieval.get("graph_hits", 0)) for result in kb_results),
            web_hits=sum(int(result.retrieval.get("web_hits", 0)) for result in kb_results),
            direct_evidence_hits=(rerank_result.direct_evidence_count if rerank_result else len(evidence_hits)),
            retrieval_rounds=len(kb_results),
            planner_used=planner_result.used,
            reranker_used=bool(rerank_result and rerank_result.used),
            ann_used=any(bool(result.retrieval.get("ann_used")) for result in kb_results),
            planner_ms=round(planner_result.elapsed_ms, 3),
            knowledge_ms=round(knowledge_ms, 3),
            reranker_ms=round(reranker_ms, 3),
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
            )
            response.retrieval.total_ms = round((perf_counter() - started) * 1000, 3)
            self._write_trace(
                trace_id,
                question,
                plan,
                response,
                self._trace_details(planner_result, kb_results, rerank_result),
            )
            return response

        synthesis_started = perf_counter()
        answer = self._fast_answer(question, sources, plan) if plan.intent in DETERMINISTIC_FAST_INTENTS else None
        if answer is None and rerank_result and rerank_result.sufficient and rerank_result.grounded_answer:
            answer = rerank_result.grounded_answer
        if answer is None and (not self.llm.enabled or bool(rerank_result and rerank_result.error)):
            answer = self._fast_answer(question, sources, plan)
        if answer is None:
            try:
                answer = await self.llm.complete(
                    self._messages(
                        question,
                        sources,
                        limitations,
                        plan,
                        rerank_result.facts if rerank_result else (),
                    ),
                    max_tokens=self.settings.answer_max_tokens,
                )
            except Exception:
                answer = self._fast_answer(question, sources, plan) or self._evidence_summary_answer(sources)
                limitations.notes.append("回答模型调用超时或不可用，已按审查通过的证据生成确定性降级答案。")
        retrieval.synthesis_ms = round((perf_counter() - synthesis_started) * 1000, 3)
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
        )
        response.retrieval.total_ms = round((perf_counter() - started) * 1000, 3)
        if ANSWER_CACHE_ENABLED and self._is_cacheable_question(question):
            ANSWER_CACHE[cache_key] = response.model_copy(deep=True)
        self._write_trace(
            trace_id,
            question,
            plan,
            response,
            self._trace_details(planner_result, kb_results, rerank_result),
        )
        return response

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

    def _trace_details(self, planner_result, kb_results: list, rerank_result: RerankResult | None) -> dict:
        return {
            "planner": {
                "used": planner_result.used,
                "elapsed_ms": round(planner_result.elapsed_ms, 3),
                "error": planner_result.error,
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
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    def _select_evidence_hits(
        self,
        hits: list[dict],
        question: str,
        plan: QueryPlan | None = None,
    ) -> list[dict]:
        if not hits:
            return []
        effective_plan = plan or understand_query(question)
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
            selected = [hit for hit in (policy, report_limit) if hit]
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
            selected = []
            seen_documents: set[str | None] = set()
            for hit in hits:
                quote = hit.get("quote") or hit.get("text") or ""
                if not hit.get("standard_no"):
                    continue
                if hit.get("document_id") in seen_documents:
                    continue
                if "具体要求按" in quote and "1/2" not in quote and "二分之一" not in quote:
                    continue
                has_ratio = any(term in quote for term in ("1/2", "二分之一", "１／２", "1/4", "四分之一", "１／４"))
                has_distance_basis = "工程间距" in quote or "基本间距" in quote or "实际间距" in quote
                if has_ratio and has_distance_basis:
                    selected.append(hit)
                    seen_documents.add(hit.get("document_id"))
                if len(selected) >= 7:
                    break
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
            return title == "采矿权申请资料清单及要求" and "材料" in (source.chapter or "")
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
        match = re.search(
            r"(自然资源部负责的矿业权.*?按照本通知附件2探矿权申请资料清单及要求、附件4采矿权申请资料清单及要求执行。)",
            clean,
        )
        if match:
            return match.group(1)
        return clean[:260] + ("..." if len(clean) > 260 else "")

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
        ]
        for pattern in patterns:
            match = re.search(pattern, clean)
            if match:
                return match.group(1)
        return clean[:260] + ("..." if len(clean) > 260 else "")

    def _direct_projection_quote(self, text: str) -> str:
        normalized = re.sub(r"\s+", " ", text).strip()
        patterns = [
            r"(5\.4\.2\s*相邻的两个工程一个见矿.*?推断资源量工程间距.*?1/2尖推。)",
            r"(8\.3\.4\.5\.2\s*有限外推：\s*a\).*?实际间距1/2尖推、1/4平推。)",
            r"(8\.2\.3\.1\s*有限外推原则：.*?实际工程间距2/3尖推或1/3平推。)",
            r"(G\.1\.3\s*应根据矿体.*?基本勘查工程间距的四分之一平推。)",
            r"(a\)当见矿工程与相邻工程.*?按推断资源量.*?1/2尖推或1/4平推推断\s*资源量。.*?b\)当见矿工程与相邻工程.*?实际.*?1/2尖推或1/4平推推断资源量)",
        ]
        for pattern in patterns:
            match = re.search(pattern, normalized)
            if match:
                return match.group(1).strip()

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
            return "".join(candidates)
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
        engineering_answer = self._engineering_distance_answer(effective_plan, sources)
        if engineering_answer:
            return engineering_answer

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
            attachment_sources = [
                item
                for item in sources
                if item.source_role == "policy_attachment"
                and item.title == "采矿权申请资料清单及要求"
            ]
            if attachment_sources:
                def material_sequence(item: Source) -> int:
                    match = re.search(r"材料\s*(\d+)", item.chapter or "")
                    return int(match.group(1)) if match else 999

                attachment_sources.sort(key=material_sequence)
                application_label = "延续"
                if "注销" in effective_plan.normalized_query:
                    application_label = "注销"
                elif any(term in effective_plan.normalized_query for term in ("首次", "新立")):
                    application_label = "新立"
                elif "变更" in effective_plan.normalized_query or any(
                    term in effective_plan.normalized_query for term in ("转让", "转移")
                ):
                    application_label = "变更"
                lines = [
                    f"采矿权{application_label}申请应按 **自然资规〔2023〕4号附件4《采矿权申请资料清单及要求》** 提交以下材料：",
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
                        "- **当前限制**：现有证据只明确了应适用的附件，附件4逐项材料尚未结构化入库，因此暂不凭推测列出材料清单。",
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
                issuing_authority = "自然资源部" if any(term in question for term in ("自然资源部", "部颁发", "部发")) else None
                conclusion = (
                    "应由 **自然资源部** 负责矿产资源储量评审备案。"
                    if issuing_authority
                    else "需要按许可证颁发层级判断：自然资源部本级已颁发许可证的，由 **自然资源部** 负责；其他由 **省级自然资源主管部门** 负责。"
                )
                lines = [
                    conclusion,
                    "",
                    f"- **依据文件**：{authority_source.standard_no or '未知文号'}《{authority_source.title}》",
                    f"- **依据条款**：{authority_source.chapter or '相关条款'}",
                    f"- **直接依据**：{authority_source.quote}",
                ]
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
            groups: dict[str, list[tuple[Source, str]]] = {
                "按推断资源量工程间距与实际工程间距分情形": [],
                "按理论工程间距与实际间距分情形": [],
                "以拟推资源量类型的经验工程间距为外推依据": [],
                "以推断资源量工程间距为外推依据": [],
                "以理论工程间距为外推依据": [],
                "以基本工程间距为外推依据": [],
                "以实际工程间距为外推依据": [],
                "以同类型资源量/相应工程间距为外推依据": [],
                "其他或需要结合上下文判断": [],
            }
            for source in sources:
                quote = self._evidence_quote_for_prompt(question, source.quote, effective_plan) or ""
                bucket = self._projection_distance_bucket(quote)
                groups[bucket].append((source, quote))

            lines = [
                "存在不一致。不同标准对矿体外推时采用的“距离基准”并不完全相同，主要差异如下：",
                "",
            ]
            index = 1
            for bucket, items in groups.items():
                if not items:
                    continue
                lines.append(f"{index}. **{bucket}**")
                for source, quote in items:
                    lines.append(
                        f"   - **{source.standard_no or '未知标准号'}《{source.title}》**"
                        f"（{source.chapter or '相关章节'}）：{quote}"
                    )
                lines.append("")
                index += 1
            lines.append(
                "结论：回答这类问题时不能只说“按1/2尖推或1/4平推”，还必须说明这个比例是作用在"
                "“实际工程间距”“基本工程间距”“推断资源量工程间距”还是“同类型资源量工程间距”上。"
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

    def _projection_distance_bucket(self, quote: str) -> str:
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
            label = f"{source.standard_no or '未知标准'}《{source.title}》"
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
