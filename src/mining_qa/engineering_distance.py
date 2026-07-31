from __future__ import annotations

import re
from collections.abc import Sequence

from .query_understanding import canonical_exploration_type
from .schemas import Source


ENGINEERING_DISTANCE_COLUMNS = (
    ("坑探-穿脉", "坑探—穿脉"),
    ("坑探-沿脉", "坑探—沿脉"),
    ("钻探-走向", "钻探—走向"),
    ("钻探-倾斜", "钻探—倾斜"),
)

_TYPE_PATTERNS = {
    "Ⅰ": r"(?:Ⅰ|I|工)",
    "Ⅱ": r"(?:Ⅱ|II)",
    "Ⅲ": r"(?:Ⅲ|III)",
}
_DISTANCE_PATTERN = r"(\d+(?:\.\d+)?\s*[~～-]\s*\d+(?:\.\d+)?)\s*m?"


def _normalized_distance(value: str) -> str:
    return re.sub(r"\s*[~～-]\s*", "～", value).strip()


def _explicit_column_pattern(label: str) -> str:
    first, second = label.split("-", maxsplit=1)
    first_pattern = r"\s*".join(re.escape(character) for character in first)
    second_pattern = r"\s*".join(re.escape(character) for character in second)
    return rf"{first_pattern}\s*[-—－]?\s*{second_pattern}"


def parse_engineering_distance_matrix(text: str) -> dict[str, tuple[str, str, str, str]]:
    """Parse both raw OCR matrices and target-row table quotations.

    The v4 source leaf stores table F.1 as a newline matrix, while the table
    projection path stores one selected row with explicit column labels.  Both
    representations carry four independent cells and must not be reduced to a
    single scalar merely because their values happen to be equal.
    """

    compact = re.sub(r"\s+", " ", text or "").strip()
    if not compact:
        return {}

    matrix: dict[str, tuple[str, str, str, str]] = {}
    for type_label, type_pattern in _TYPE_PATTERNS.items():
        row = re.search(
            rf"(?<![A-Za-z0-9]){type_pattern}(?:\s*类型)?(?![A-Za-z0-9])"
            rf"\s*[：:；;,，。]?\s*{_DISTANCE_PATTERN}\s+{_DISTANCE_PATTERN}\s+"
            rf"{_DISTANCE_PATTERN}\s+{_DISTANCE_PATTERN}",
            compact,
        )
        if row:
            matrix[type_label] = tuple(
                _normalized_distance(row.group(index)) for index in range(1, 5)
            )  # type: ignore[assignment]

    # A selected table row is rendered as “Ⅰ类型；坑探-穿脉 …；…”.
    # Its measurements are labelled rather than adjacent, so parse it as a
    # second representation only when the target type is explicit.
    for type_label, type_pattern in _TYPE_PATTERNS.items():
        if type_label in matrix:
            continue
        if not re.search(
            rf"(?<![A-Za-z0-9]){type_pattern}\s*类型(?![A-Za-z0-9])",
            compact,
        ):
            continue
        values: list[str] = []
        for internal_label, _ in ENGINEERING_DISTANCE_COLUMNS:
            match = re.search(
                rf"{_explicit_column_pattern(internal_label)}\s*{_DISTANCE_PATTERN}",
                compact,
            )
            if not match:
                values = []
                break
            values.append(_normalized_distance(match.group(1)))
        if len(values) == 4:
            matrix[type_label] = tuple(values)  # type: ignore[assignment]

    return matrix


def engineering_distance_row(
    source: Source,
    target_type: str | None,
) -> tuple[str, tuple[str, str, str, str]] | None:
    canonical_type = canonical_exploration_type(target_type)
    matrix = parse_engineering_distance_matrix(source.quote or "")
    if canonical_type:
        values = matrix.get(canonical_type)
        return (canonical_type, values) if values else None
    if len(matrix) == 1:
        return next(iter(matrix.items()))
    return None


def _source_table_label(source: Source) -> str:
    context = f"{source.chapter or ''} {source.quote or ''}"
    match = re.search(r"表\s*F\.\s*1", context, flags=re.IGNORECASE)
    if match:
        return "表 F.1"
    return source.chapter or "相关工程间距表"


def render_engineering_distance_answer(
    sources: Sequence[Source],
    target_type: str | None,
    *,
    research_heading: bool = False,
) -> str | None:
    canonical_type = canonical_exploration_type(target_type)
    for source in sources:
        matrix = parse_engineering_distance_matrix(source.quote or "")
        if not matrix:
            continue
        table_label = _source_table_label(source)
        prefix = ["**研究结论**", ""] if research_heading else []

        if canonical_type:
            values = matrix.get(canonical_type)
            if not values:
                continue
            pit_crosscut, pit_drift, drill_strike, drill_dip = values
            lines = [
                *prefix,
                f"根据 **{source.standard_no or '未知标准号'}《{source.title}》**（{table_label}），"
                f"勘查 **{canonical_type}类型**控制资源量的参考基本勘查工程间距为：",
                "",
                f"- **沿矿体走向线：{drill_strike} m**",
                f"- **沿矿体倾斜线：{drill_dip} m**",
                "",
                f"即 **{drill_strike} m × {drill_dip} m（走向 × 倾斜）**。",
                "",
                "表中四个工程栏的原始读数为：",
                f"- **坑探**：穿脉 {pit_crosscut} m；沿脉 {pit_drift} m",
                f"- **钻探**：走向 {drill_strike} m；倾斜 {drill_dip} m",
                "",
                "该表给出的是控制资源量勘查工程间距的参考值。",
            ]
            quote = source.quote or ""
            if "实际工作" in quote and "适当调整" in quote:
                lines[-1] += "实际工作可按矿床实际适当调整。"
            return "\n".join(lines)

        if set(matrix) != set(_TYPE_PATTERNS):
            continue
        rows = [
            "| 勘查类型 | 坑探—穿脉（m） | 坑探—沿脉（m） | 钻探—走向（m） | 钻探—倾斜（m） |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        rows.extend(
            f"| {type_label} | {values[0]} | {values[1]} | {values[2]} | {values[3]} |"
            for type_label, values in matrix.items()
        )
        return "\n".join(
            [
                *prefix,
                f"根据 **{source.standard_no or '未知标准号'}《{source.title}》**（{table_label}），"
                "控制资源量的参考基本勘查工程间距如下：",
                "",
                *rows,
                "",
                "以上数值为控制资源量勘查工程间距的参考值。",
            ]
        )
    return None
