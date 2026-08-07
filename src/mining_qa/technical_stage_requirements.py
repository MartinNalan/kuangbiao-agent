from __future__ import annotations

import re


TECHNICAL_REQUIREMENT_STANDARD_NO = "DZ/T 0340-2020"
TECHNICAL_REQUIREMENT_STANDARD_TITLE = "矿产勘查矿石加工选冶技术性能试验研究程度要求"
TECHNICAL_REQUIREMENT_APPLICABILITY_CLAUSE = "6.1.1"
ROCK_GOLD_STANDARD_NO = "DZ/T 0205-2020"
ROCK_GOLD_STANDARD_TITLE = "矿产地质勘查规范 岩金"
ROCK_GOLD_EXPLORATION_CLAUSE = "4.3.4"
STAGE_SECTION_BY_LABEL = {
    "普查": "6.3",
    "详查": "6.4",
    "勘探": "6.5",
}


def explicit_resource_scale_from_text(text: str) -> str | None:
    """Return a scale only when the question explicitly ties it to resources.

    ``大型金矿`` may describe a mine or project and must not silently become
    ``大型资源量规模``.  The fixed admissibility item supplies the latter
    relation explicitly, so its large-scale matrix can be narrowed safely.
    """

    compact = re.sub(r"\s+", "", str(text or ""))
    for label, value in (("大型", "large"), ("中型", "medium"), ("小型", "small")):
        if re.search(
            rf"(?:"
            rf"(?:资源量|资源储量)规模(?:为|是|属于)?{label}"
            rf"|{label}(?:资源量|资源储量)规模"
            rf")",
            compact,
        ):
            return value
    return None


def is_rock_gold_question(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    return "岩金" in compact or "金矿" in compact


def stage_label_from_text(text: str) -> str | None:
    compact = re.sub(r"\s+", "", str(text or ""))
    for label in STAGE_SECTION_BY_LABEL:
        if f"{label}阶段" in compact or label in compact:
            return label
    return None


def stage_section_from_text(text: str) -> str | None:
    label = stage_label_from_text(text)
    return STAGE_SECTION_BY_LABEL.get(label) if label else None


def stage_requirement_matrix_clauses(text: str) -> tuple[str, ...]:
    section = stage_section_from_text(text)
    if not section:
        return ()
    if section == "6.5" and explicit_resource_scale_from_text(text) == "large":
        # 6.5.2—6.5.4 respectively contain the large-scale easy,
        # relatively-easy and difficult-ore branches.  6.5.1 is only the
        # small-scale easy-ore branch.
        return ("6.5.2", "6.5.3", "6.5.4")
    return tuple(f"{section}.{index}" for index in range(1, 5))


def stage_requirement_exception_clauses(text: str) -> tuple[str, ...]:
    # 6.5.6 is not another matrix row.  It is the explicit sampling-difficulty
    # exception and must be preserved separately in an exploration-stage
    # admissibility conclusion.  6.5.5 concerns crushing/grinding indices and
    # belongs to a different review item.
    return ("6.5.6",) if stage_section_from_text(text) == "6.5" else ()


def stage_requirement_clauses(text: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *stage_requirement_matrix_clauses(text),
                *stage_requirement_exception_clauses(text),
            )
        )
    )


def stage_requirement_evidence_refs(text: str) -> tuple[tuple[str, str], ...]:
    """Compile the deterministic evidence contract for a stage question."""

    refs: list[tuple[str, str]] = []
    if is_rock_gold_question(text) and stage_section_from_text(text) == "6.5":
        refs.append(
            (
                TECHNICAL_REQUIREMENT_STANDARD_NO,
                TECHNICAL_REQUIREMENT_APPLICABILITY_CLAUSE,
            )
        )
    refs.extend(
        (TECHNICAL_REQUIREMENT_STANDARD_NO, clause)
        for clause in stage_requirement_clauses(text)
    )
    if is_rock_gold_question(text) and stage_section_from_text(text) == "6.5":
        refs.append((ROCK_GOLD_STANDARD_NO, ROCK_GOLD_EXPLORATION_CLAUSE))
    return tuple(dict.fromkeys(refs))


def stage_requirement_label(text: str) -> str:
    return f"{stage_label_from_text(text) or '对应'}阶段"
