from __future__ import annotations

import argparse
import re
import sys
import time
from html import unescape
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.knowledge_store import DEFAULT_DB_PATH, connect, official_source  # noqa: E402


NRSIS_BASE = "http://www.nrsis.org.cn"


def fetch(url: str, timeout: int = 20) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def compact(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def is_english_version(value: str | None) -> bool:
    text = compact(value)
    return "英文版" in text or "（EN）" in text or "(EN)" in text


def nrsis_detail_url(standard_no: str, title: str) -> str | None:
    url = f"{NRSIS_BASE}/portal/xxcx/std?key={quote(standard_no)}"
    try:
        html = fetch(url)
    except URLError:
        return None

    rows = re.findall(r"<tr>(.*?)</tr>", html, flags=re.S | re.I)
    target_code = compact(standard_no)
    target_title = compact(title)
    candidates: list[tuple[int, str]] = []
    for row in rows:
        code_match = re.search(r"<td>\s*([^<]*?(?:DZ/T|DZ|TD/T|TD|HY/T|HY|CH/T|CH)[^<]*?)\s*</td>", row, flags=re.S | re.I)
        link_match = re.search(r'<a\s+href="(/portal/stdDetail/\d+)"[^>]*>(.*?)</a>', row, flags=re.S | re.I)
        if not code_match or not link_match:
            continue
        raw_code = unescape(code_match.group(1))
        raw_title = re.sub(r"<.*?>", "", unescape(link_match.group(2)))
        code = compact(raw_code)
        linked_title = compact(raw_title)
        score = 0
        if code == target_code:
            score += 100
        elif code.startswith(target_code):
            score += 40
        if target_title and linked_title == target_title:
            score += 50
        elif target_title and target_title in linked_title:
            score += 20
        if is_english_version(raw_code) or is_english_version(raw_title):
            score -= 100
        if score > 0:
            candidates.append((score, NRSIS_BASE + link_match.group(1)))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def hydrate(db_path: Path, sleep_seconds: float, limit: int | None) -> tuple[int, int]:
    updated = 0
    checked = 0
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            select document_id, standard_no, title, official_url
            from documents
            where standard_no is not null
            order by standard_no
            """
        ).fetchall()
        for row in rows:
            if limit is not None and checked >= limit:
                break
            standard_no = row["standard_no"] or ""
            platform, fallback_url = official_source(standard_no)
            detail_url = None
            if compact(standard_no).startswith(("DZ/T", "DZ", "TD/T", "TD", "HY/T", "HY", "CH/T", "CH")):
                checked += 1
                detail_url = nrsis_detail_url(standard_no, row["title"] or "")
                if sleep_seconds:
                    time.sleep(sleep_seconds)
            url = detail_url or fallback_url
            if url and url != row["official_url"]:
                conn.execute(
                    "update documents set official_url = ?, source_platform = ? where document_id = ?",
                    (url, platform, row["document_id"]),
                )
                updated += 1
        conn.commit()
    return checked, updated


def main() -> int:
    parser = argparse.ArgumentParser(description="Hydrate cached official standard detail links.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path")
    parser.add_argument("--sleep", type=float, default=0.05, help="Delay between official site requests")
    parser.add_argument("--limit", type=int, default=None, help="Optional max standards to check")
    args = parser.parse_args()

    checked, updated = hydrate(Path(args.db), args.sleep, args.limit)
    print(f"checked: {checked}")
    print(f"updated: {updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
