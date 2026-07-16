from __future__ import annotations

import re


TECHNICAL_REQUIREMENT_STANDARD_NO = "DZ/T 0340-2020"
TECHNICAL_REQUIREMENT_STANDARD_TITLE = "矿产勘查矿石加工选冶技术性能试验研究程度要求"
STAGE_SECTION_BY_LABEL = {
    "普查": "6.3",
    "详查": "6.4",
    "勘探": "6.5",
}


def stage_label_from_text(text: str) -> str | None:
    compact = re.sub(r"\s+", "", str(text or ""))
    for label in STAGE_SECTION_BY_LABEL:
        if f"{label}阶段" in compact or label in compact:
            return label
    return None


def stage_section_from_text(text: str) -> str | None:
    label = stage_label_from_text(text)
    return STAGE_SECTION_BY_LABEL.get(label) if label else None


def stage_requirement_clauses(text: str) -> tuple[str, ...]:
    section = stage_section_from_text(text)
    if not section:
        return ()
    return tuple(f"{section}.{index}" for index in range(1, 5))


def stage_requirement_label(text: str) -> str:
    return f"{stage_label_from_text(text) or '对应'}阶段"
