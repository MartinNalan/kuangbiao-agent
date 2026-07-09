from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from urllib.parse import quote, urljoin

import httpx

from .config import Settings
from .llm_client import LLMClient
from .schemas import Source


STANDARD_NO_RE = re.compile(r"\b(?:GB|GB/T|DZ/T|DZ|NB/T|HJ|YS/T)\s*\d{3,6}(?:\.\d+)?[-－]\d{4}\b", re.I)


@dataclass(frozen=True)
class StandardCandidate:
    standard_no: str | None
    title: str | None
    reason: str


@dataclass
class WebSupplementResult:
    sources: list[Source]
    notes: list[str]


class WebSupplement:
    def __init__(self, settings: Settings, llm: LLMClient):
        self.settings = settings
        self.llm = llm

    async def search(self, question: str) -> WebSupplementResult:
        candidates = await self._candidates(question)
        sources: list[Source] = []
        notes: list[str] = []

        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
            trust_env=False,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            for candidate in candidates[:5]:
                found = await self._search_candidate(client, candidate)
                sources.extend(found)

        unique_sources = self._dedupe_sources(sources)
        if unique_sources:
            notes.append("本地知识库未提供条款级证据，已尝试从官方标准平台补充元数据和公开阅读入口。")
            notes.append("联网补充结果仅用于确认来源和可访问性；未取得可检索正文时，不能据此生成条款级结论。")
        else:
            notes.append("本地知识库未命中，且官方标准平台未检索到可用候选来源。")

        return WebSupplementResult(sources=unique_sources, notes=notes)

    async def _candidates(self, question: str) -> list[StandardCandidate]:
        candidates: list[StandardCandidate] = []
        for standard_no in STANDARD_NO_RE.findall(question):
            candidates.append(StandardCandidate(standard_no=self._normalize_standard_no(standard_no), title=None, reason="用户问题中包含标准号"))

        candidates.extend(self._keyword_candidates(question))
        if len(candidates) < 2:
            candidates.extend(await self._llm_candidates(question))

        return self._dedupe_candidates(candidates)

    def _keyword_candidates(self, question: str) -> list[StandardCandidate]:
        candidates: list[StandardCandidate] = []
        if "固体矿产资源储量分类" in question or ("资源储量" in question and "分类" in question):
            candidates.append(
                StandardCandidate(
                    standard_no="GB/T 17766-2020",
                    title="固体矿产资源储量分类",
                    reason="问题提到固体矿产资源储量分类",
                )
            )
        if "方解石" in question and ("规范" in question or "勘查" in question):
            candidates.append(
                StandardCandidate(
                    standard_no="DZ/T 0321-2018",
                    title="方解石矿地质勘查规范",
                    reason="问题提到方解石矿地质勘查规范",
                )
            )
        return candidates

    async def _llm_candidates(self, question: str) -> list[StandardCandidate]:
        if not self.llm.enabled:
            return []

        messages = [
            {
                "role": "system",
                "content": (
                    "你只负责为矿产资源标准问答提取可能相关的标准检索线索。"
                    "不要回答用户问题，不要编造条款。输出 JSON："
                    "{\"candidates\":[{\"standard_no\":null或标准号,\"title\":null或标准名称,\"reason\":\"简短理由\"}]}。"
                    "最多 3 个候选。"
                ),
            },
            {"role": "user", "content": question},
        ]
        try:
            raw = await self.llm.complete_json(messages)
            data = json.loads(raw)
        except Exception:
            return []

        candidates = []
        for item in data.get("candidates", []):
            if not isinstance(item, dict):
                continue
            standard_no = item.get("standard_no")
            title = item.get("title")
            reason = item.get("reason") or "大模型根据问题语义推测的检索线索"
            if standard_no or title:
                candidates.append(
                    StandardCandidate(
                        standard_no=self._normalize_standard_no(str(standard_no)) if standard_no else None,
                        title=str(title).strip() if title else None,
                        reason=str(reason).strip(),
                    )
                )
        return candidates

    async def _search_candidate(self, client: httpx.AsyncClient, candidate: StandardCandidate) -> list[Source]:
        query = candidate.standard_no or candidate.title
        if not query:
            return []

        sources: list[Source] = []
        if candidate.standard_no and self._is_national_standard(candidate):
            sources.extend(await self._search_samr(client, candidate))
            return sources

        if candidate.standard_no and self._is_natural_resources_standard(candidate):
            sources.extend(await self._search_nrsis(client, candidate))
            return sources

        if self._is_national_standard(candidate):
            sources.extend(await self._search_samr(client, candidate))
        if self._is_natural_resources_standard(candidate):
            sources.extend(await self._search_nrsis(client, candidate))

        if not sources:
            sources.extend(await self._search_samr(client, candidate))
            sources.extend(await self._search_nrsis(client, candidate))
        return sources

    async def _search_samr(self, client: httpx.AsyncClient, candidate: StandardCandidate) -> list[Source]:
        direct_url = self._known_samr_url(candidate)
        if direct_url:
            source = await self._parse_samr_detail(client, direct_url)
            return [source] if source else []

        query = quote(candidate.standard_no or candidate.title or "")
        try:
            response = await client.get(f"https://std.samr.gov.cn/search/stdPage?q={query}&tid=")
        except httpx.HTTPError:
            return []

        links = re.findall(r'href=["\']([^"\']*newGbInfo\?hcno=[^"\']+)["\']', response.text)
        sources = []
        for link in links[:3]:
            source = await self._parse_samr_detail(client, urljoin("https://std.samr.gov.cn", html.unescape(link)))
            if source:
                sources.append(source)
        return sources

    async def _parse_samr_detail(self, client: httpx.AsyncClient, url: str) -> Source | None:
        try:
            response = await client.get(url)
        except httpx.HTTPError:
            return None

        text = response.text
        standard_no = self._first_match(r"标准号：\s*([^<\s][^<]+?)\s*(?:</|$)", text)
        title = self._first_match(r"中文标准名称：\s*<b>(.*?)</b>", text) or self._first_match(r"<title>国家标准\|([^<]+)</title>", text)
        status = self._first_match(r">\s*(现行|废止|即将实施)\s*<", text)
        publish_date = self._near_label_value(text, "发布日期")
        implementation_date = self._near_label_value(text, "实施日期")
        has_preview = "在线预览" in text or "ck_btn" in text

        if not standard_no and not title:
            return None

        quote_parts = ["国家标准公开系统元数据"]
        if status:
            quote_parts.append(f"状态：{status}")
        if publish_date:
            quote_parts.append(f"发布日期：{publish_date}")
        if implementation_date:
            quote_parts.append(f"实施日期：{implementation_date}")
        if has_preview:
            quote_parts.append("提供在线预览入口，正文通常需按官方阅读器查看或 OCR 后入库")

        return Source(
            title=self._clean(title) or "国家标准公开系统候选标准",
            standard_no=self._normalize_standard_no(self._clean(standard_no)) if standard_no else None,
            quote="；".join(quote_parts),
            source_type="official_visual" if has_preview else "official_metadata",
            text_access="image_ocr_required" if has_preview else "metadata_only",
            url=url,
            validation_status="official_source_found",
        )

    async def _search_nrsis(self, client: httpx.AsyncClient, candidate: StandardCandidate) -> list[Source]:
        query = quote(candidate.standard_no or candidate.title or "")
        url = f"http://www.nrsis.org.cn/portal/xxcx/std?pageNo=1&key={query}&pageSize=20&pageOrderBy=&pageOrderType="
        try:
            response = await client.get(url)
        except httpx.HTTPError:
            return []

        detail_links = re.findall(r'href=["\'](/portal/stdDetail/\d+)["\']', response.text)
        sources = []
        for link in detail_links[:3]:
            source = await self._parse_nrsis_detail(client, urljoin("http://www.nrsis.org.cn", link))
            if source:
                sources.append(source)
        return sources

    async def _parse_nrsis_detail(self, client: httpx.AsyncClient, url: str) -> Source | None:
        try:
            response = await client.get(url)
        except httpx.HTTPError:
            return None

        text = response.text
        title = self._first_match(r"<h4[^>]*>\s*([^<\n]+)", text)
        standard_no = self._first_match(r"<b>\s*((?:DZ/T|DZ)\s*\d{3,6}[-－]\d{4})\s*</b>", text)
        status = self._first_match(r'class=["\']s-status[^"\']*["\'][^>]*>\s*([^<]+)', text)
        reader_url = self._first_match(r"readPdf\('([^']+)'\)", text)
        publish_date = self._near_label_value(text, "发布日期")
        implementation_date = self._near_label_value(text, "实施日期")

        if not standard_no and not title:
            return None

        quote_parts = ["自然资源标准化信息服务平台元数据"]
        if status:
            quote_parts.append(f"状态：{self._clean(status)}")
        if publish_date:
            quote_parts.append(f"发布日期：{publish_date}")
        if implementation_date:
            quote_parts.append(f"实施日期：{implementation_date}")
        if reader_url:
            quote_parts.append("提供官方全文阅读入口；如为图片型页面，需要 OCR 后才能条款级回答")

        return Source(
            title=self._clean(title) or "自然资源标准候选标准",
            standard_no=self._normalize_standard_no(self._clean(standard_no)) if standard_no else None,
            quote="；".join(quote_parts),
            source_type="official_visual" if reader_url else "official_metadata",
            text_access="image_ocr_required" if reader_url else "metadata_only",
            url=reader_url or url,
            validation_status="official_source_found",
        )

    def _is_national_standard(self, candidate: StandardCandidate) -> bool:
        value = candidate.standard_no or ""
        return value.upper().startswith(("GB", "GB/T"))

    def _is_natural_resources_standard(self, candidate: StandardCandidate) -> bool:
        value = candidate.standard_no or ""
        return value.upper().startswith(("DZ", "DZ/T")) or "地质" in (candidate.title or "") or "矿" in (candidate.title or "")

    def _known_samr_url(self, candidate: StandardCandidate) -> str | None:
        standard_no = (candidate.standard_no or "").upper().replace(" ", "")
        if standard_no == "GB/T17766-2020":
            return "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=3F98C03DE9AB232432B3732A491983E7"
        return None

    def _dedupe_candidates(self, candidates: list[StandardCandidate]) -> list[StandardCandidate]:
        seen: set[tuple[str | None, str | None]] = set()
        unique = []
        for candidate in candidates:
            key = (candidate.standard_no, candidate.title)
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return unique

    def _dedupe_sources(self, sources: list[Source]) -> list[Source]:
        seen: set[tuple[str | None, str | None]] = set()
        unique = []
        for source in sources:
            key = (source.standard_no, source.url)
            if key in seen:
                continue
            seen.add(key)
            unique.append(source)
        return unique

    def _near_label_value(self, text: str, label: str) -> str | None:
        pattern = rf"{label}</[^>]+>\s*<[^>]+>\s*([^<]+)"
        return self._clean(self._first_match(pattern, text))

    def _first_match(self, pattern: str, text: str) -> str | None:
        match = re.search(pattern, text, re.I | re.S)
        return match.group(1) if match else None

    def _clean(self, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = re.sub(r"<[^>]+>", " ", html.unescape(value))
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ：:;；\n\t")
        return cleaned or None

    def _normalize_standard_no(self, value: str) -> str:
        return re.sub(r"\s+", " ", value.replace("－", "-").strip()).upper()
