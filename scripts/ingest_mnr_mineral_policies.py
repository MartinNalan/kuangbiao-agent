from __future__ import annotations

import argparse
import csv
import html
import json
import mimetypes
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.kb_build_utils import document_type_from_policy_level, split_clause_like_text, stable_id  # noqa: E402
from mining_qa.knowledge_store import DEFAULT_DB_PATH, connect, utc_now  # noqa: E402
from mining_qa.mnr_policy_allowlist import (  # noqa: E402
    DEFAULT_ALLOWLIST_ARTIFACT,
    load_allowlist_artifact,
    policy_is_allowed,
)


CATEGORY_URL = "https://f.mnr.gov.cn/579/585/index_3553.html"
KB_ROOT = PROJECT_ROOT / "data" / "knowledge_base"
RAW_DIR = KB_ROOT / "raw" / "mnr_policy"
ATTACH_DIR = RAW_DIR / "attachments"
MANIFEST_DIR = KB_ROOT / "manifests"
LOG_DIR = KB_ROOT / "logs"


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            attrs_dict = {k: v or "" for k, v in attrs}
            self._href = attrs_dict.get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            text = " ".join("".join(self._text).split())
            self.links.append({"href": self._href, "text": text})
            self._href = None
            self._text = []


def fetch(url: str, retries: int = 3, delay: float = 0.4) -> tuple[bytes, dict[str, str]]:
    last_error: Exception | None = None
    for _ in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://f.mnr.gov.cn/"})
            with urlopen(req, timeout=30) as resp:
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.read(), headers
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(delay)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def decode_html(data: bytes, headers: dict[str, str]) -> str:
    ctype = headers.get("content-type", "")
    m = re.search(r"charset=([A-Za-z0-9_-]+)", ctype)
    encodings = [m.group(1)] if m else []
    encodings += ["utf-8", "gb18030"]
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except Exception:  # noqa: BLE001
            continue
    return data.decode("utf-8", errors="replace")


def page_url(page_index: int) -> str:
    if page_index == 0:
        return CATEGORY_URL
    return CATEGORY_URL.replace("index_3553.html", f"index_3553_{page_index}.html")


def count_pages(text: str) -> int:
    m = re.search(r"countPage\s*=\s*(\d+)", text)
    return int(m.group(1)) if m else 1


def extract_li_blocks(text: str) -> list[str]:
    return re.findall(r"<li\s+class=\"p123\"[^>]*>(.*?)</li>", text, flags=re.S | re.I)


def strip_tags(value: str) -> str:
    value = re.sub(r"<script.*?</script>|<style.*?</style>", "", value, flags=re.S | re.I)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</(?:p|div|tr|li|h\d)>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(re.sub(r"[ \t]+", " ", value)).strip()


def parse_list_block(block: str, base_url: str) -> dict[str, Any] | None:
    parser = LinkParser()
    parser.feed(block)
    detail = None
    for link in parser.links:
        href = link["href"]
        if href and href != "javascript:;" and re.search(r"/t\d+_\d+\.html|\.\/\d{6}/t\d+_\d+\.html", href):
            detail = {"title": link["text"], "url": urljoin(base_url, href)}
            break
    if not detail:
        return None
    metadata: dict[str, str] = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", block, flags=re.S | re.I):
        cells = [strip_tags(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S | re.I)]
        for i in range(0, len(cells) - 1, 2):
            key = re.sub(r"\s+", "", cells[i])
            value = " ".join(cells[i + 1].split())
            if key:
                metadata[key] = value
    return {
        "title": metadata.get("名称") or detail["title"],
        "url": detail["url"],
        "business_type": metadata.get("业务类型", ""),
        "metadata": metadata,
    }


def extract_links(text: str, base_url: str) -> list[dict[str, str]]:
    parser = LinkParser()
    parser.feed(text)
    links = []
    for link in parser.links:
        href = link["href"]
        if not href or href == "javascript:;":
            continue
        links.append({"label": link["text"], "url": urljoin(base_url, href)})
    return links


def extract_detail_text(text: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>", "", text, flags=re.S | re.I)
    nodes = re.findall(r"<[^>]+class=[\"'][^\"']*Custom_UnionStyle[^\"']*[\"'][^>]*>(.*?)</[^>]+>", text, flags=re.S | re.I)
    if nodes and any(strip_tags(node) for node in nodes):
        body = "\n".join(strip_tags(node) for node in nodes)
    else:
        startprint = re.search(r"<!--\s*startprint\s*-->(.*?)<!--\s*endprint\s*-->", text, flags=re.S | re.I)
        if startprint:
            body = strip_tags(startprint.group(1))
        else:
            m = re.search(r"<div[^>]+id=[\"']content[\"'][^>]*>(.*?)</body>", text, flags=re.S | re.I)
            if not m:
                m = re.search(r"<div[^>]+class=[\"']content[\"'][^>]*>(.*?)</body>", text, flags=re.S | re.I)
            body = strip_tags(m.group(1) if m else text)
    if "body" not in locals():
        body = ""
    if len(body.strip()) < 20:
        m = re.search(r"<div[^>]+id=[\"']content[\"'][^>]*>(.*?)</body>", text, flags=re.S | re.I)
        body = strip_tags(m.group(1) if m else text)
    markers = ["各省、自治区", "中华人民共和国国务院令", "第一章", "第一条", "为统筹", "根据《"]
    starts = [body.find(marker) for marker in markers if body.find(marker) >= 0]
    if starts:
        body = body[min(starts) :]
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def safe_name(value: str, max_len: int = 80) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value).strip("_")
    return value[:max_len] or "file"


def download_attachment(url: str, label: str, doc_id: str) -> dict[str, Any]:
    data, headers = fetch(url)
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix
    if not suffix:
        suffix = mimetypes.guess_extension(headers.get("content-type", "").split(";")[0].strip()) or ".bin"
    out_dir = ATTACH_DIR / doc_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{safe_name(label or Path(parsed.path).name)}{suffix}"
    out_path.write_bytes(data)
    return {
        "label": label,
        "url": url,
        "path": str(out_path.relative_to(PROJECT_ROOT)),
        "content_type": headers.get("content-type", ""),
        "bytes": len(data),
    }


def insert_policy(conn, entry: dict[str, Any], detail_text: str, attachments: list[dict[str, Any]], html_path: Path, now: str) -> tuple[str, int]:
    meta = entry["metadata"]
    url = entry["url"]
    title = entry["title"]
    doc_id = stable_id("mnr_policy", url, prefix="policy")
    doc_type = document_type_from_policy_level(meta.get("效力级别", ""))
    status = meta.get("时效状态") or "unknown"
    clause_chunks = split_clause_like_text(detail_text, max_len=1800)
    source_trace = {
        "source_url": url,
        "source_site": "自然资源部政策法规库",
        "category": "矿产资源管理",
        "raw_html": str(html_path.relative_to(PROJECT_ROOT)),
        "attachments": attachments,
    }
    conn.execute("delete from documents where document_id = ?", (doc_id,))
    conn.execute("delete from chunks_fts where document_id = ?", (doc_id,))
    conn.execute("delete from chunks where document_id = ?", (doc_id,))
    conn.execute(
        """
        insert into documents (
          document_id,title,standard_no,document_type,status,source_type,text_access,
          validation_status,visibility,review_status,publish_date,implementation_date,
          ingestion_time,updated_at,source_priority,source_trace_json,bibliographic_json,
          quality_json,page_count,chunk_count,table_count,can_answer,official_url,source_platform
        ) values (?, ?, ?, ?, ?, 'official_fulltext', 'html_text', 'parsed',
          'internal', 'approved_for_service', ?, ?, ?, ?, 120, ?, ?, ?, 0, ?, ?, ?, ?, ?)
        """,
        (
            doc_id,
            title,
            meta.get("文号") or None,
            doc_type,
            status,
            meta.get("成文时间") or None,
            meta.get("发布日期") or None,
            now,
            now,
            json.dumps(source_trace, ensure_ascii=False),
            json.dumps(meta, ensure_ascii=False),
            json.dumps({"policy_category": "矿产资源管理"}, ensure_ascii=False),
            len(clause_chunks),
            len(attachments),
            1 if clause_chunks else 0,
            url,
            "自然资源部政策法规库",
        ),
    )
    rows = []
    for idx, chunk in enumerate(clause_chunks, 1):
        cid = stable_id(doc_id, idx, chunk["text"], prefix="chunk")
        rows.append(
            (
                cid,
                doc_id,
                "policy_clause",
                title,
                meta.get("文号") or None,
                chunk.get("section_path"),
                chunk.get("clause_no"),
                None,
                None,
                None,
                None,
                chunk["text"],
                None,
                "official_fulltext",
                "html_text",
                "html_policy_clause",
                None,
                "parsed",
                "internal",
                url,
                now,
            )
        )
    conn.executemany(
        """
        insert into chunks (
          chunk_id,document_id,chunk_type,title,standard_no,section_path,clause_no,
          page_start,page_end,char_start,char_end,text,table_json,source_type,text_access,
          parse_method,confidence,validation_status,visibility,source_ref,created_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.executemany(
        "insert into chunks_fts(chunk_id,document_id,title,standard_no,section_path,text) values (?, ?, ?, ?, ?, ?)",
        [(row[0], row[1], row[3], row[4], row[5], row[11]) for row in rows],
    )
    return doc_id, len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download and ingest MNR mineral-resource management policies/laws.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--limit", type=int, default=0, help="Limit documents for testing; 0 means all.")
    parser.add_argument("--no-attachments", action="store_true", help="Do not download attachments.")
    parser.add_argument(
        "--allowlist-artifact",
        type=Path,
        default=DEFAULT_ALLOWLIST_ARTIFACT,
        help="Workbook-derived pre-2026 MNR valid-document allowlist artifact.",
    )
    args = parser.parse_args()
    allowlist_artifact = load_allowlist_artifact(args.allowlist_artifact)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    ATTACH_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    first_data, first_headers = fetch(CATEGORY_URL)
    first_text = decode_html(first_data, first_headers)
    page_total = count_pages(first_text)
    entries: list[dict[str, Any]] = []
    for page_index in range(page_total):
        url = page_url(page_index)
        data, headers = (first_data, first_headers) if page_index == 0 else fetch(url)
        text = first_text if page_index == 0 else decode_html(data, headers)
        page_file = RAW_DIR / f"list_{page_index + 1:03d}.html"
        page_file.write_text(text, encoding="utf-8")
        for block in extract_li_blocks(text):
            entry = parse_list_block(block, url)
            if entry:
                entries.append(entry)
        time.sleep(0.1)

    seen = set()
    deduped = []
    for entry in entries:
        if entry["url"] in seen:
            continue
        seen.add(entry["url"])
        deduped.append(entry)
    entries = deduped[: args.limit] if args.limit else deduped

    manifest_rows = []
    skipped_rows = []
    now = utc_now()
    with connect(Path(args.db)) as conn:
        for idx, entry in enumerate(entries, 1):
            meta = entry["metadata"]
            allowed, governance_reason = policy_is_allowed(
                meta.get("文号"),
                meta.get("成文时间") or meta.get("发布日期"),
                allowlist_artifact,
            )
            if not allowed:
                skipped_rows.append(
                    {
                        "index": idx,
                        "title": entry["title"],
                        "url": entry["url"],
                        "file_no": meta.get("文号", ""),
                        "published_or_formed": meta.get("成文时间") or meta.get("发布日期") or "",
                        "governance_reason": governance_reason,
                    }
                )
                print(f"[{idx}/{len(entries)}] SKIP {entry['title']} reason={governance_reason}")
                continue
            doc_id = stable_id("mnr_policy", entry["url"], prefix="policy")
            detail_data, detail_headers = fetch(entry["url"])
            detail_text_raw = decode_html(detail_data, detail_headers)
            html_path = RAW_DIR / f"{doc_id}.html"
            html_path.write_text(detail_text_raw, encoding="utf-8")
            links = extract_links(detail_text_raw, entry["url"])
            attachment_links = [
                link
                for link in links
                if re.search(r"\.(pdf|doc|docx|xls|xlsx|zip|rar)(?:$|\?)", link["url"], re.I) or "附件" in link["label"]
            ]
            attachments = []
            if not args.no_attachments:
                for link in attachment_links:
                    try:
                        attachments.append(download_attachment(link["url"], link["label"], doc_id))
                    except Exception as exc:  # noqa: BLE001
                        attachments.append({"label": link["label"], "url": link["url"], "error": str(exc)})
                    time.sleep(0.1)
            body = extract_detail_text(detail_text_raw)
            inserted_doc_id, chunk_count = insert_policy(conn, entry, body, attachments, html_path, now)
            manifest_rows.append(
                {
                    "index": idx,
                    "document_id": inserted_doc_id,
                    "title": entry["title"],
                    "url": entry["url"],
                    "file_no": entry["metadata"].get("文号", ""),
                    "status": entry["metadata"].get("时效状态", ""),
                    "level": entry["metadata"].get("效力级别", ""),
                    "published_or_formed": entry["metadata"].get("成文时间", ""),
                    "chunk_count": chunk_count,
                    "attachment_count": len(attachments),
                }
            )
            print(f"[{idx}/{len(entries)}] {entry['title']} chunks={chunk_count} attachments={len(attachments)}")

    manifest_csv = MANIFEST_DIR / "mnr_mineral_policy_manifest.csv"
    with manifest_csv.open("w", encoding="utf-8-sig", newline="") as f:
        fields = list(manifest_rows[0].keys()) if manifest_rows else []
        writer = csv.DictWriter(f, fieldnames=fields)
        if manifest_rows:
            writer.writeheader()
            writer.writerows(manifest_rows)
    manifest_json = MANIFEST_DIR / "mnr_mineral_policy_manifest.json"
    manifest_json.write_text(json.dumps(manifest_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    skipped_json = MANIFEST_DIR / "mnr_mineral_policy_skipped_by_allowlist.json"
    skipped_json.write_text(json.dumps(skipped_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "category_url": CATEGORY_URL,
        "page_total": page_total,
        "document_count": len(manifest_rows),
        "manifest_csv": str(manifest_csv),
        "manifest_json": str(manifest_json),
        "skipped_manifest_json": str(skipped_json),
        "skipped_by_allowlist_count": len(skipped_rows),
        "allowlist_artifact": str(args.allowlist_artifact),
        "raw_dir": str(RAW_DIR),
    }
    (LOG_DIR / "mnr_policy_ingest_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
