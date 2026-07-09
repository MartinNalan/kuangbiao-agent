from __future__ import annotations

import hashlib
import re
from typing import Any


CN_NUM = "一二三四五六七八九十百千万零〇两"


def stable_id(*parts: Any, prefix: str = "id") -> str:
    raw = "\n".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def normalize_ws(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def infer_clause_no(line: str) -> str | None:
    line = normalize_ws(line).strip()
    patterns = [
        r"^((?:[0-9]+|[A-Z])(?:[.．][0-9A-Z]+){0,6})(?=\s|[\u4e00-\u9fff（(])",
        rf"^(第[{CN_NUM}]+条)",
        rf"^(第[{CN_NUM}]+章)",
        rf"^([{CN_NUM}]+、)",
        r"^([（(][一二三四五六七八九十百千万0-9]+[）)])",
        r"^(附录\s*[A-ZＡ-Ｚ])",
    ]
    for pattern in patterns:
        m = re.match(pattern, line)
        if m:
            return re.sub(r"\s+", "", m.group(1)).replace("．", ".")
    prefix = line[:100]
    m = re.search(r"(?:^|[\s。；;，,])((?:[1-9]\d?|[A-Z])(?:[.．][0-9A-Z]+){1,6})(?=\s*[\u4e00-\u9fff（(])", prefix)
    if m:
        return m.group(1).replace("．", ".")
    return None


def is_clause_heading(line: str) -> bool:
    no = infer_clause_no(line)
    if not no:
        return False
    return bool(
        re.match(rf"^第[{CN_NUM}]+条", line.strip())
        or re.match(r"^(?:[0-9]+|[A-Z])(?:[.．][0-9A-Z]+){0,6}(?=\s|[\u4e00-\u9fff（(])", line.strip())
        or re.search(r"(?:^|[\s。；;，,])(?:[1-9]\d?|[A-Z])(?:[.．][0-9A-Z]+){1,6}(?=\s*[\u4e00-\u9fff（(])", line[:100])
    )


def is_section_heading(line: str) -> bool:
    stripped = line.strip()
    return bool(
        re.match(rf"^第[{CN_NUM}]+章", stripped)
        or re.match(r"^附录\s*[A-ZＡ-Ｚ]", stripped)
        or re.match(r"^[0-9]+\s+[\u4e00-\u9fff]", stripped)
    )


def split_clause_like_text(text: str, max_len: int = 1800) -> list[dict[str, Any]]:
    """Split legal/standard text into clause-like chunks while keeping fallback chunks.

    The splitter is intentionally conservative. It starts a new chunk at obvious
    Chinese law clauses, standard numeric clauses, chapter headings, and list
    headings. Long chunks are further split by paragraphs.
    """
    text = normalize_ws(text)
    if not text:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    chunks: list[dict[str, Any]] = []
    current: list[str] = []
    current_no: str | None = None
    current_section: str | None = None
    active_section: str | None = None

    def flush() -> None:
        nonlocal current, current_no, current_section
        if not current:
            return
        body = "\n".join(current).strip()
        if len(body) <= max_len:
            chunks.append({"clause_no": current_no, "section_path": current_section, "text": body})
        else:
            paras = [p.strip() for p in re.split(r"\n\s*\n|(?<=。)\n", body) if p.strip()]
            part: list[str] = []
            part_idx = 1
            for para in paras:
                candidate = "\n".join(part + [para]).strip()
                if part and len(candidate) > max_len:
                    suffix = f"{current_no or 'part'}#{part_idx}"
                    chunks.append({"clause_no": suffix, "section_path": current_section, "text": "\n".join(part).strip()})
                    part = [para]
                    part_idx += 1
                else:
                    part.append(para)
            if part:
                suffix = current_no if part_idx == 1 else f"{current_no or 'part'}#{part_idx}"
                chunks.append({"clause_no": suffix, "section_path": current_section, "text": "\n".join(part).strip()})
        current = []
        current_no = None
        current_section = None

    for line in lines:
        no = infer_clause_no(line)
        is_heading = is_section_heading(line)
        starts_clause = is_clause_heading(line)
        starts_list_heading = bool(re.match(rf"^[{CN_NUM}]+、", line) and len(line) <= 80)
        if current and (is_heading or starts_clause or starts_list_heading):
            flush()
        if not current:
            current_no = no
            if is_heading or starts_list_heading:
                current_section = line[:120]
                active_section = current_section
            else:
                current_section = active_section
        current.append(line)
    flush()
    return chunks


def document_type_from_policy_level(level: str) -> str:
    if "法律" in level:
        return "law"
    if "行政法规" in level or "国务院" in level:
        return "regulation"
    if "规章" in level:
        return "department_rule"
    return "policy_document"
