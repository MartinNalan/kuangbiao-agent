from __future__ import annotations

"""Small, source-agnostic helpers for selecting evidence text.

The helpers deliberately describe structure rather than embedding any source
sentence.  They return text from the caller-provided evidence unchanged.
"""

import re
from collections.abc import Sequence


AnchorGroup = Sequence[str]
AnchorSequence = Sequence[AnchorGroup]


def contains_evidence_anchor_group(
    text: str,
    alternatives: Sequence[AnchorGroup],
) -> bool:
    """Return whether one short, source-agnostic anchor group matches text."""

    compact = re.sub(r"\s+", "", text or "").casefold()
    return any(
        bool(group)
        and all(
            re.sub(r"\s+", "", anchor).casefold() in compact
            for anchor in group
            if anchor
        )
        for group in alternatives
    )


def split_evidence_sentences(text: str) -> list[str]:
    clean = re.sub(r"\s+", " ", text or "").strip()
    if not clean:
        return []
    parts = re.split(r"(?<=[。！？；;.!?])\s*", clean)
    sentences = [part.strip() for part in parts if part.strip()]
    if len(sentences) <= 1 and len(clean) > 120:
        clauses = re.split(r"(?<=[，,、])\s*", clean)
        sentences = [part.strip() for part in clauses if part.strip()]
    return sentences or [clean]


def extract_evidence_by_anchor_sequences(
    text: str,
    alternatives: Sequence[AnchorSequence],
    *,
    limit: int = 700,
) -> str | None:
    """Return the first ordered sentence span satisfying short anchor groups.

    Each alternative is an ordered sequence of sentence-level anchor groups.
    All anchors in one group must occur in the corresponding sentence.  This
    supports both a single evidence sentence and a bounded adjacent evidence
    chain without storing the source wording in application code.
    """

    sentences = split_evidence_sentences(text)
    compact_sentences = [re.sub(r"\s+", "", item).casefold() for item in sentences]
    for alternative in alternatives:
        groups = tuple(tuple(anchor for anchor in group if anchor) for group in alternative)
        if not groups or any(not group for group in groups):
            continue
        span = len(groups)
        for start in range(0, len(sentences) - span + 1):
            if all(
                all(
                    re.sub(r"\s+", "", anchor).casefold()
                    in compact_sentences[start + offset]
                    for anchor in group
                )
                for offset, group in enumerate(groups)
            ):
                selected = "".join(sentences[start : start + span]).strip()
                return selected[:limit].rstrip() + (
                    "..." if len(selected) > limit else ""
                )
    return None


__all__ = (
    "AnchorGroup",
    "AnchorSequence",
    "contains_evidence_anchor_group",
    "extract_evidence_by_anchor_sequences",
    "split_evidence_sentences",
)
