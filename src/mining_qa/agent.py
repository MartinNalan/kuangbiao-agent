import re
from uuid import uuid4

from .config import Settings
from .domain_gate import DomainGate
from .gap_tasks import KnowledgeGapTaskStore
from .knowledge_client import KnowledgeClient
from .llm_client import LLMClient
from .schemas import AskRequest, AskResponse, Limitations, RetrievalStats, Source
from .web_supplement import WebSupplement


ANSWER_CACHE_ENABLED = False
ANSWER_CACHE: dict[str, AskResponse] = {}
CACHEABLE_COMPARISON_TERMS = ("不一致", "差异", "不同", "比较", "列举", "哪些标准", "哪些规范")
PROJECTION_DISTANCE_TERMS = ("外推所依据的距离", "外推依据", "外推距离", "依据的距离")
POLICY_AUTHORITY_INTENT_TERMS = ("哪个机构", "去哪个机构", "谁负责", "哪一级部门", "哪个部门", "权限", "负责")
POLICY_AUTHORITY_TOPIC_TERMS = (
    "储量评审",
    "储量报告评审",
    "储量报告",
    "评审备案",
    "采矿证",
    "采矿许可证",
    "勘查许可证",
    "矿产资源储量",
)


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
        cache_key = self._cache_key(request.question)
        if ANSWER_CACHE_ENABLED and cache_key in ANSWER_CACHE:
            cached = ANSWER_CACHE[cache_key].model_copy(deep=True)
            cached.session_id = session_id
            return cached

        domain_decision = self.domain_gate.check(request.question)
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
        kb_result = await self.knowledge.search(request.question, filters)

        evidence_hits = self._select_evidence_hits(kb_result.results, request.question)
        sources = [self._source_from_hit(hit) for hit in evidence_hits]
        sources = self._trim_source_quotes(request.question, sources)
        retrieval = RetrievalStats(
            full_text_hits=len(evidence_hits),
            vector_hits=kb_result.retrieval.get("vector_hits", 0),
            graph_hits=kb_result.retrieval.get("graph_hits", 0),
            web_hits=kb_result.retrieval.get("web_hits", 0),
        )
        has_clause_evidence = bool(kb_result.coverage.get("has_clause_level_evidence", False))
        has_catalog_evidence = self._is_standard_selection_question(request.question) and bool(sources)
        has_usable_evidence = has_clause_evidence or has_catalog_evidence
        notes = list(kb_result.coverage.get("notes", []))
        if self._is_policy_authority_question(request.question):
            has_authority_evidence = any(self._is_policy_authority_source(source) for source in sources)
            has_usable_evidence = has_authority_evidence
            has_clause_evidence = has_authority_evidence
            if not has_authority_evidence:
                notes.append("已识别为职责/权限归属问题，但当前证据未包含明确负责主体，不能给出确定结论。")
        if kb_result.coverage.get("needs_web_supplement") and self.settings.enable_sync_web_supplement:
            notes.append("本地知识库证据不足，建议补充官方元数据、全文入口或 OCR 任务。")
            web_result = await self.web.search(request.question)
            sources.extend(web_result.sources)
            retrieval.web_hits = len(web_result.sources)
            notes.extend(web_result.notes)
            staged_count = await self.knowledge.create_candidates(
                request.question,
                [source.model_dump(exclude_none=True) for source in web_result.sources],
            )
            if staged_count:
                notes.append(f"已将 {staged_count} 条联网候选来源写入候选暂存区，等待管理员审核后入库。")
        elif kb_result.coverage.get("needs_web_supplement"):
            notes.append("本地知识库证据不足，已进入异步补库流程；本次请求不等待联网搜索或 OCR。")

        limitations = Limitations(has_clause_level_evidence=has_usable_evidence, notes=notes)

        if not has_usable_evidence:
            gap_task = self.gap_tasks.create(request.question, domain_decision, len(sources))
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

        answer = self._fast_answer(request.question, sources)
        if answer is None:
            answer = await self.llm.complete(self._messages(request.question, sources, limitations))
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
        if ANSWER_CACHE_ENABLED and self._is_cacheable_question(request.question):
            ANSWER_CACHE[cache_key] = response.model_copy(deep=True)
        return response

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
        if self._is_standard_selection_question(question):
            catalog_hits = [hit for hit in hits if "catalog" in (hit.get("hit_type") or [])]
            if catalog_hits:
                return catalog_hits[:1]
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

        top = hits[0]
        top_score = float(top.get("score") or 0)
        top_document_id = top.get("document_id")
        is_comparison_question = self._is_comparison_question(question)
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
        return any(term in question for term in POLICY_AUTHORITY_INTENT_TERMS) and any(
            term in question for term in POLICY_AUTHORITY_TOPIC_TERMS
        )

    def _is_policy_authority_source(self, source: Source) -> bool:
        quote = source.quote or ""
        return (
            "自然资源部负责本级已颁发勘查许可证或采矿许可证" in quote
            or "其他由省级自然资源主管部门负责" in quote
            or ("自然资源主管部门" in quote and "委托矿产资源储量评审机构" in quote)
        )

    def _trim_source_quotes(self, question: str, sources: list[Source]) -> list[Source]:
        if not self._is_projection_distance_question(question) and not self._is_policy_authority_question(question):
            return sources
        trimmed = []
        for source in sources:
            item = source.model_copy()
            if self._is_policy_authority_question(question):
                item.quote = self._direct_policy_authority_quote(source.quote or "")
            else:
                item.quote = self._evidence_quote_for_prompt(question, source.quote)
            trimmed.append(item)
        return trimmed

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
