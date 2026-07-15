from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TechnicalTestLevel:
    key: str
    label: str
    rank: int
    source_clause: str
    source_standard_no: str = "DZ/T 0340-2020"
    aliases: tuple[str, ...] = ()


# This schema applies only to the mineral-processing test track in
# DZ/T 0340-2020. It deliberately excludes mineralogy and physical-property
# studies, which are separate evidence axes rather than lower test levels.
MINERAL_PROCESSING_TEST_LEVELS = (
    TechnicalTestLevel(
        key="selectability",
        label="可选性试验",
        rank=1,
        source_clause="5.2.1",
    ),
    TechnicalTestLevel(
        key="laboratory_flow",
        label="实验室流程试验",
        rank=2,
        source_clause="5.2.2",
        aliases=("实验室流程实验",),
    ),
    TechnicalTestLevel(
        key="laboratory_expanded_continuous",
        label="实验室扩大连续试验",
        rank=3,
        source_clause="5.2.3",
        aliases=("扩大连续试验", "扩大试验"),
    ),
    TechnicalTestLevel(
        key="semi_industrial",
        label="半工业试验",
        rank=4,
        source_clause="5.2.4",
    ),
    TechnicalTestLevel(
        key="industrial",
        label="工业试验",
        rank=5,
        source_clause="5.2.5",
    ),
)


def levels_in_text(text: str) -> tuple[TechnicalTestLevel, ...]:
    return tuple(level for level, _, _ in level_mentions(text))


def level_mentions(text: str) -> tuple[tuple[TechnicalTestLevel, int, int], ...]:
    """Return non-duplicated level mentions in their order of appearance."""
    compact = "".join(str(text or "").split())
    matches: list[tuple[TechnicalTestLevel, int, int]] = []
    for level in MINERAL_PROCESSING_TEST_LEVELS:
        for name in (level.label, *level.aliases):
            start = compact.find(name)
            if start >= 0:
                matches.append((level, start, start + len(name)))
                break
    non_overlapping = [
        item
        for item in matches
        if not any(
            other is not item
            and other[1] <= item[1]
            and other[2] >= item[2]
            and (other[2] - other[1]) > (item[2] - item[1])
            for other in matches
        )
    ]
    return tuple(sorted(non_overlapping, key=lambda item: item[1]))


def highest_level_in_text(text: str) -> TechnicalTestLevel | None:
    levels = levels_in_text(text)
    return max(levels, key=lambda item: item.rank, default=None)


def actual_level_from_sufficiency_question(text: str) -> TechnicalTestLevel | None:
    """Extract the test asserted by the user before a satisfaction question.

    This only identifies the stated test level. It deliberately does not inspect
    its sample mass, duration, equipment, or other conformance details.
    """
    compact = "".join(str(text or "").split())
    mentions = level_mentions(compact)
    if not mentions:
        return None
    pivot = re.search(r"能否|是否|可否|可以|能不能|还需要|还需|必须", compact)
    if pivot:
        preceding = [item for item in mentions if item[2] <= pivot.start()]
        if preceding:
            return preceding[-1][0]
    return mentions[0][0]


def required_level_from_sufficiency_question(text: str) -> TechnicalTestLevel | None:
    """Extract an explicitly named target level after a relation verb."""
    compact = "".join(str(text or "").split())
    mentions = level_mentions(compact)
    if len(mentions) < 2:
        return None
    relation = re.search(r"满足|覆盖|替代|还需要|还需|必须达到|要求达到", compact)
    if not relation:
        return None
    following = [item for item in mentions if item[1] >= relation.end()]
    return following[0][0] if following else None


def level_covers(
    actual: TechnicalTestLevel | None,
    required: TechnicalTestLevel | None,
) -> bool:
    return bool(actual and required and actual.rank >= required.rank)


def hierarchy_clauses_through(level: TechnicalTestLevel | None) -> tuple[str, ...]:
    if level is None:
        return ()
    return tuple(
        item.source_clause
        for item in MINERAL_PROCESSING_TEST_LEVELS
        if item.rank <= level.rank
    )
