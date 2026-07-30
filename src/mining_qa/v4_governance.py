"""Evidence-backed governance helpers for the isolated v4 corpus."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urljoin


NRSIS_BASE = "http://www.nrsis.org.cn"
SAMR_BASE = "https://std.samr.gov.cn"


def compact_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def normalize_standard_no(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").upper()
    text = text.replace("－", "-").replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", "", text)


def normalize_title(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower()
    return "".join(char for char in text if char.isalnum())


def title_similarity(left: str | None, right: str | None) -> float:
    normalized_left = normalize_title(left)
    normalized_right = normalize_title(right)
    if not normalized_left or not normalized_right:
        return 0.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def effective_status_from_official(value: str | None) -> str:
    status = compact_text(value)
    if status in {"现行", "现行有效", "有效", "已发布"}:
        return "current"
    if status in {"废止", "失效", "被代替", "已废止"}:
        return "repealed"
    return "governance_conflict"


@dataclass(frozen=True)
class OfficialRecord:
    platform: str
    query_url: str
    detail_url: str
    standard_no: str
    title: str
    official_status: str
    publish_date: str | None = None
    implementation_date: str | None = None
    record_type: str | None = None
    record_id: str | None = None

    @property
    def effective_status(self) -> str:
        return effective_status_from_official(self.official_status)


class NrsisSearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self._row: list[dict[str, str | None]] | None = None
        self._cell_text: list[str] | None = None
        self._cell_link: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell_text = []
            self._cell_link = None
        elif tag == "a" and self._cell_text is not None:
            self._cell_link = values.get("href")

    def handle_data(self, data: str) -> None:
        if self._cell_text is not None:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._row is not None and self._cell_text is not None:
            self._row.append({"text": compact_text("".join(self._cell_text)), "link": self._cell_link})
            self._cell_text = None
            self._cell_link = None
        elif tag == "tr" and self._row is not None:
            link_index = next((index for index, cell in enumerate(self._row) if cell.get("link")), None)
            if link_index is not None and link_index >= 1 and len(self._row) >= link_index + 4:
                self.rows.append(
                    {
                        "standard_no": self._row[link_index - 1]["text"],
                        "title": self._row[link_index]["text"],
                        "detail_path": self._row[link_index]["link"],
                        "publish_date": self._row[link_index + 1]["text"],
                        "implementation_date": self._row[link_index + 2]["text"],
                        "status": self._row[link_index + 3]["text"],
                    }
                )
            self._row = None


_SAMR_CODE_PATTERN = re.compile(
    r"(?P<prefix>GB\s*/\s*T|GB|DZ\s*/\s*T|EJ\s*/\s*T|MT\s*/\s*T)\s*"
    r"(?P<number>\d+(?:\.\d+)*-\d{4})",
    flags=re.IGNORECASE,
)


def _canonical_prefix(value: str) -> str:
    compact = re.sub(r"\s+", "", value.upper())
    return {"GB/T": "GB/T", "GB": "GB", "DZ/T": "DZ/T", "EJ/T": "EJ/T", "MT/T": "MT/T"}[compact]


class SamrSearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.records: list[dict[str, Any]] = []
        self._anchor: dict[str, Any] | None = None
        self._pending: dict[str, Any] | None = None
        self._status_text: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("tid") and values.get("pid"):
            self._anchor = {"tid": values["tid"], "pid": values["pid"], "text": []}
        elif tag == "span" and self._pending is not None:
            classes = (values.get("class") or "").split()
            if "s-status" in classes and self._status_text is None:
                self._status_text = []

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor["text"].append(data)
        elif self._status_text is not None:
            self._status_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None:
            display = compact_text(" ".join(self._anchor.pop("text")))
            match = _SAMR_CODE_PATTERN.search(display)
            if match:
                self._pending = {
                    **self._anchor,
                    "standard_no": f"{_canonical_prefix(match.group('prefix'))} {match.group('number')}",
                    "title": compact_text(display[match.end() :]),
                }
            self._anchor = None
        elif tag == "span" and self._pending is not None and self._status_text is not None:
            status = compact_text("".join(self._status_text))
            self._status_text = None
            if status:
                self.records.append({**self._pending, "status": status})
                self._pending = None


def parse_nrsis_search(html_text: str, query_url: str) -> list[OfficialRecord]:
    parser = NrsisSearchParser()
    parser.feed(html_text)
    return [
        OfficialRecord(
            platform="自然资源标准化信息服务平台",
            query_url=query_url,
            detail_url=urljoin(NRSIS_BASE, str(row["detail_path"])),
            standard_no=str(row["standard_no"]),
            title=str(row["title"]),
            official_status=str(row["status"]),
            publish_date=str(row["publish_date"] or "") or None,
            implementation_date=str(row["implementation_date"] or "") or None,
            record_type="natural_resources_standard",
        )
        for row in parser.rows
    ]


def samr_detail_url(record_type: str, record_id: str) -> str:
    if record_type == "BV_HB":
        return f"{SAMR_BASE}/hb/search/stdHBDetailed?id={quote(record_id)}"
    if record_type == "BV_DB":
        return f"{SAMR_BASE}/db/search/stdDBDetailed?id={quote(record_id)}"
    if record_type == "BV_GB_PLAN":
        return f"{SAMR_BASE}/search/stdPage?q={quote(record_id)}&tid=BV_GB_PLAN"
    return f"{SAMR_BASE}/gb/search/gbDetailed?id={quote(record_id)}"


def parse_samr_search(html_text: str, query_url: str) -> list[OfficialRecord]:
    parser = SamrSearchParser()
    parser.feed(html_text)
    return [
        OfficialRecord(
            platform="全国标准信息公共服务平台",
            query_url=query_url,
            detail_url=samr_detail_url(str(row["tid"]), str(row["pid"])),
            standard_no=str(row["standard_no"]),
            title=str(row["title"]),
            official_status=str(row["status"]),
            record_type=str(row["tid"]),
            record_id=str(row["pid"]),
        )
        for row in parser.records
    ]


def official_query_url(standard_no: str) -> tuple[str, str]:
    if normalize_standard_no(standard_no).startswith("DZ/T"):
        query = urlencode(
            {
                "pageNo": 1,
                "key": standard_no,
                "pageSize": 20,
                "pageOrderBy": "",
                "pageOrderType": "",
            }
        )
        return "nrsis", f"{NRSIS_BASE}/portal/xxcx/std?{query}"
    return "samr", f"{SAMR_BASE}/search/stdPage?q={quote(standard_no)}&tid="


def parse_official_search(platform_key: str, html_text: str, query_url: str) -> list[OfficialRecord]:
    if platform_key == "nrsis":
        return parse_nrsis_search(html_text, query_url)
    return parse_samr_search(html_text, query_url)


def choose_exact_record(records: list[OfficialRecord], standard_no: str, title: str) -> OfficialRecord | None:
    target_code = normalize_standard_no(standard_no)
    exact = [record for record in records if normalize_standard_no(record.standard_no) == target_code]
    ordinary = [record for record in exact if record.record_type != "BV_GB_PLAN" and "修改单" not in record.title]
    candidates = ordinary or exact
    if not candidates:
        return None
    return max(candidates, key=lambda record: title_similarity(title, record.title))


def amendment_parent_title(title: str) -> str | None:
    compact = compact_text(title).strip("《》")
    for suffix in ("国家标准第1号修改单", "《第1号修改单》", "第1号修改单", "修改单"):
        if compact.endswith(suffix):
            return compact[: -len(suffix)].strip("《》 ")
    return None


def priority_for_quality(finding_type: str, effective_status: str) -> tuple[str, int]:
    if finding_type in {"attachment_requires_parser_benchmark", "empty_source_page", "identity_status_governance_conflict"}:
        return "P0", 0
    if finding_type == "missing_standard_number":
        return ("P0", 0) if effective_status == "governance_conflict" else ("P2", 2)
    if finding_type == "low_ocr_confidence":
        return ("P1", 1) if effective_status == "current" else ("P2", 2)
    if finding_type == "oversized_structural_unit":
        return ("P1", 1) if effective_status == "current" else ("P2", 2)
    return "P2", 2


REMEDIATION_METHODS = {
    "attachment_requires_parser_benchmark": "从官方原始附件运行受控结构化解析器，与现有治理表逐行比对；基准通过前保留现有结构且不得静默替换。",
    "empty_source_page": "检查原始 PDF/图片判断是否为真实空白页；若含内容则受控重跑 OCR，并保留原文本、页码映射、前后哈希和人工验收决定。",
    "identity_status_governance_conflict": "由人工在官方发布或标准平台核验文献身份与效力；没有正式证据时保持不可回答。",
    "missing_standard_number": "核验标准号；修改单可保留无编号，但必须维持明确的母标准关系和母标准官方状态证据。",
    "low_ocr_confidence": "以原始 PDF 对应页受控重跑 OCR；对数值、单位、表格和条款号做差异校核，验收前不覆盖原文本。",
    "oversized_structural_unit": "仅按原文显式标题、条款号或页边界确定性重切分；正文不改写，并保留原单元哈希与新旧映射。",
}


def evidence_file_name(document_id: str, platform_key: str) -> str:
    return f"{document_id}.{platform_key}.search.html"


def load_json_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
