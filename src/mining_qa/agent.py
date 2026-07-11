import json
import re
from uuid import uuid4

from .config import Settings
from .domain_gate import DomainGate
from .gap_tasks import KnowledgeGapTaskStore
from .knowledge_client import KnowledgeClient
from .llm_client import LLMClient
from .query_understanding import QueryPlan, understand_query
from .schemas import AskRequest, AskResponse, Limitations, RetrievalStats, Source
from .web_supplement import WebSupplement


ANSWER_CACHE_ENABLED = False
ANSWER_CACHE: dict[str, AskResponse] = {}
CACHEABLE_COMPARISON_TERMS = ("不一致", "差异", "不同", "比较", "列举", "哪些标准", "哪些规范")
PROJECTION_DISTANCE_TERMS = ("外推所依据的距离", "外推依据", "外推距离", "依据的距离")
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
"""


class MiningQAAgent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.knowledge = KnowledgeClient(settings)
        self.llm = LLMClient(settings)
        self.web = WebSupplement(settings, self.llm)
        self.domain_gate = DomainGate()
        self.gap_tasks = KnowledgeGapTaskStore()

    async def ask(self, request: AskRequest) -> AskResponse:
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
            return AskResponse(
                answer="本服务仅回答矿产资源、地质勘查、矿山设计、自然资源管理、标准规范及相关政策技术问题，无法处理该问题。",
                session_id=session_id,
                status="out_of_scope",
                limitations=limitations,
                confidence="low",
            )

        filters = request.filters.model_dump(exclude_none=True)
        kb_result = await self.knowledge.search(question, filters)

        evidence_hits = self._select_evidence_hits(kb_result.results, question)
        sources = [self._source_from_hit(hit) for hit in evidence_hits]
        sources = self._trim_source_quotes(question, sources)
        has_usable_evidence, has_clause_evidence = self._evaluate_evidence(question, kb_result.coverage, sources)
        notes = list(kb_result.coverage.get("notes", []))

        if not has_usable_evidence:
            rewritten_query = await self._fallback_retrieval_query(question)
            if rewritten_query and self._cache_key(rewritten_query) != self._cache_key(question):
                rewritten_result = await self.knowledge.search(rewritten_query, filters)
                rewritten_hits = self._select_evidence_hits(rewritten_result.results, question)
                rewritten_sources = [self._source_from_hit(hit) for hit in rewritten_hits]
                rewritten_sources = self._trim_source_quotes(question, rewritten_sources)
                rewritten_usable, rewritten_clause = self._evaluate_evidence(
                    question,
                    rewritten_result.coverage,
                    rewritten_sources,
                )
                if rewritten_usable:
                    kb_result = rewritten_result
                    evidence_hits = rewritten_hits
                    sources = rewritten_sources
                    has_usable_evidence = rewritten_usable
                    has_clause_evidence = rewritten_clause
                    notes = list(rewritten_result.coverage.get("notes", []))
                    notes.append("首次检索证据不足，已使用一次低置信度查询改写后重新检索。")

        retrieval = RetrievalStats(
            full_text_hits=len(evidence_hits),
            vector_hits=kb_result.retrieval.get("vector_hits", 0),
            graph_hits=kb_result.retrieval.get("graph_hits", 0),
            web_hits=kb_result.retrieval.get("web_hits", 0),
        )
        if kb_result.coverage.get("needs_web_supplement") and self.settings.enable_sync_web_supplement:
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
        elif kb_result.coverage.get("needs_web_supplement"):
            notes.append("本地知识库证据不足，已进入异步补库流程；本次请求不等待联网搜索或 OCR。")

        limitations = Limitations(has_clause_level_evidence=has_usable_evidence, notes=notes)

        if not has_usable_evidence:
            gap_task = self.gap_tasks.create(question, domain_decision, len(sources))
            return AskResponse(
                answer=self._insufficient_answer(request.question, notes),
                session_id=session_id,
                status="queued_for_enrichment",
                sources=sources,
                retrieval=retrieval,
                limitations=limitations,
                knowledge_gap_task=gap_task,
                confidence="low",
            )

        answer = self._fast_answer(question, sources)
        if answer is None:
            answer = await self.llm.complete(self._messages(question, sources, limitations))
        answer = self._append_source_links(answer, sources)
        response = AskResponse(
            answer=answer,
            session_id=session_id,
            status="answered",
            sources=sources,
            retrieval=retrieval,
            limitations=limitations,
            confidence="medium" if sources else "low",
        )
        if ANSWER_CACHE_ENABLED and self._is_cacheable_question(question):
            ANSWER_CACHE[cache_key] = response.model_copy(deep=True)
        return response

    def _evaluate_evidence(
        self,
        question: str,
        coverage: dict,
        sources: list[Source],
    ) -> tuple[bool, bool]:
        plan = understand_query(question)
        if plan.intent == "standard_selection":
            found = bool(sources)
            return found, found

        validators = {
            "engineering_distance_lookup": self._is_engineering_distance_source,
            "authority_responsibility": self._is_policy_authority_source,
            "service_materials": self._is_service_material_source,
            "service_procedure_basis": self._is_service_procedure_source,
            "service_time_limit": self._is_service_time_limit_source,
            "projection_numeric_rule": self._is_projection_numeric_source,
            "legal_responsibility": self._is_legal_responsibility_source,
        }
        validator = validators.get(plan.intent)
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

    def _messages(self, question: str, sources: list[Source], limitations: Limitations) -> list[dict[str, str]]:
        evidence_lines = []
        for index, source in enumerate(sources, start=1):
            quote = self._evidence_quote_for_prompt(question, source.quote)
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

    def _select_evidence_hits(self, hits: list[dict], question: str) -> list[dict]:
        if not hits:
            return []
        plan = understand_query(question)
        if plan.intent == "engineering_distance_lookup" and plan.target_exploration_type:
            required_labels = ("坑探-穿脉", "坑探-沿脉", "钻探-走向", "钻探-倾斜")
            for hit in hits:
                quote = hit.get("quote") or ""
                title = hit.get("title") or ""
                title_matches = not plan.candidate_title_terms or any(
                    term in title for term in plan.candidate_title_terms
                )
                if (
                    title_matches
                    and f"{plan.target_exploration_type}类型" in quote
                    and all(label in quote for label in required_labels)
                ):
                    return [hit]
        if self._is_standard_selection_question(question):
            catalog_hits = [hit for hit in hits if "catalog" in (hit.get("hit_type") or [])]
            if catalog_hits:
                return catalog_hits[:1]

        strict_validators = {
            "service_materials": self._is_service_material_source,
            "service_procedure_basis": self._is_service_procedure_source,
            "service_time_limit": self._is_service_time_limit_source,
            "projection_numeric_rule": self._is_projection_numeric_source,
            "legal_responsibility": self._is_legal_responsibility_source,
        }
        strict_validator = strict_validators.get(plan.intent)
        if strict_validator:
            matched = [hit for hit in hits if strict_validator(self._source_from_hit(hit))]
            if plan.intent == "service_materials":
                attachment_hits = [
                    hit
                    for hit in matched
                    if hit.get("source_role") == "policy_attachment"
                ]
                if attachment_hits:
                    return attachment_hits[:20]
            if plan.intent in {"projection_numeric_rule", "legal_responsibility"}:
                return matched[:1]
            return matched[:3]
        if self._is_projection_distance_question(question):
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
        if self._is_policy_authority_question(question):
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

        if plan.intent == "related_documents" and plan.focus_terms:
            anchor = max(plan.focus_terms, key=len)
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
        is_comparison_question = self._is_comparison_question(question) or plan.exhaustive_search
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

    def _is_comparison_question(self, question: str) -> bool:
        return any(term in question for term in CACHEABLE_COMPARISON_TERMS)

    def _is_cacheable_question(self, question: str) -> bool:
        return False

    def _cache_key(self, question: str) -> str:
        return " ".join(question.split())

    def _evidence_quote_for_prompt(self, question: str, quote: str | None) -> str | None:
        if not quote or not self._is_comparison_question(question):
            return quote
        clean = " ".join(quote.split())
        if self._is_projection_distance_question(question):
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

    def _is_projection_distance_question(self, question: str) -> bool:
        return "矿体外推" in question and any(term in question for term in PROJECTION_DISTANCE_TERMS)

    def _is_policy_authority_question(self, question: str) -> bool:
        return understand_query(question).intent == "authority_responsibility"

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

    def _is_policy_authority_source(self, source: Source) -> bool:
        quote = source.quote or ""
        return (
            "自然资源部负责本级已颁发勘查许可证或采矿许可证" in quote
            or "其他由省级自然资源主管部门负责" in quote
            or ("自然资源主管部门" in quote and "委托矿产资源储量评审机构" in quote)
        )

    def _trim_source_quotes(self, question: str, sources: list[Source]) -> list[Source]:
        plan = understand_query(question)
        if (
            not self._is_projection_distance_question(question)
            and not self._is_policy_authority_question(question)
            and plan.intent not in {"projection_numeric_rule", "legal_responsibility", "service_materials"}
        ):
            return sources
        trimmed = []
        for source in sources:
            item = source.model_copy()
            if self._is_policy_authority_question(question):
                item.quote = self._direct_policy_authority_quote(source.quote or "")
            elif plan.intent == "projection_numeric_rule":
                item.quote = self._direct_infinite_projection_quote(source.quote or "")
            elif plan.intent == "legal_responsibility":
                item.quote = self._direct_legal_responsibility_quote(source.quote or "")
            elif plan.intent == "service_materials":
                item.quote = self._direct_service_material_quote(source.quote or "")
            else:
                item.quote = self._evidence_quote_for_prompt(question, source.quote)
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

    def _fast_answer(self, question: str, sources: list[Source]) -> str | None:
        plan = understand_query(question)
        engineering_answer = self._engineering_distance_answer(plan, sources)
        if engineering_answer:
            return engineering_answer

        if plan.intent == "projection_numeric_rule":
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

        if plan.intent == "legal_responsibility":
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

        if plan.intent == "service_materials":
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
                if "注销" in plan.normalized_query:
                    application_label = "注销"
                elif any(term in plan.normalized_query for term in ("首次", "新立")):
                    application_label = "新立"
                elif "变更" in plan.normalized_query or any(
                    term in plan.normalized_query for term in ("转让", "转移")
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
            guide_source = next(
                (
                    item
                    for item in sources
                    if "服务指南" in item.title
                    and "申请材料" in f"{item.chapter or ''} {item.quote or ''}"
                ),
                None,
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

        if plan.intent == "service_procedure_basis":
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

        if plan.intent == "service_time_limit":
            source = next((item for item in sources if self._is_service_time_limit_source(item)), None)
            if source:
                return "\n".join(
                    [
                        f"《{source.title}》的办结时限为：{source.quote}",
                        "",
                        f"- **官方来源**：{source.url or '未提供'}",
                    ]
                )

        if self._is_policy_authority_question(question) and sources:
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

        if self._is_standard_selection_question(question) and sources:
            source = sources[0]
            return (
                f"根据当前知识库和官方标准目录，建议使用 **{source.standard_no or '未知标准号'}"
                f"《{source.title}》**。\n\n"
                f"- **依据**：标准目录命中 `{source.standard_no or '未知标准号'}`，标准名称为《{source.title}》。\n"
                f"- **官方平台**：{source.source_platform or '官方标准平台'}。\n"
                "- **注意**：如果问题涉及具体技术条款，还应继续查询该标准正文中的对应章节或表格。"
            )

        if self._is_projection_distance_question(question) and self._is_comparison_question(question) and sources:
            groups: dict[str, list[tuple[Source, str]]] = {
                "按推断资源量工程间距与实际工程间距分情形": [],
                "按理论工程间距与实际间距分情形": [],
                "以推断资源量工程间距为外推依据": [],
                "以理论工程间距为外推依据": [],
                "以基本工程间距为外推依据": [],
                "以实际工程间距为外推依据": [],
                "以同类型资源量/相应工程间距为外推依据": [],
                "其他或需要结合上下文判断": [],
            }
            for source in sources:
                quote = self._evidence_quote_for_prompt(question, source.quote) or ""
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

        if "矿体外推" in question and self._is_comparison_question(question) and sources:
            lines = [
                "现有知识库证据显示，不同标准对“矿体外推”的规定存在不一致，主要体现在外推基准、外推比例、特殊情形和规定详略上。",
                "",
                "主要差异如下：",
            ]
            for index, source in enumerate(sources, start=1):
                quote = self._evidence_quote_for_prompt(question, source.quote) or ""
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
