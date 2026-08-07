"""Shadow query decision compiler for governed, condition-aware retrieval.

This module deliberately has no imports from the production query pipeline and
is not imported by it.  It is a deterministic shadow boundary used to evaluate
the proposed ``query_decision.v1`` contract before any production migration.

The first implemented profile is mineral-processing test requirements by
exploration stage.  The compiler preserves the original question, records the
provenance of every slot, selects only compatible rows from the published
DZ/T 0340-2020 truth table, and emits a clause-level evidence contract.  It
does not execute retrieval and it does not generate an answer.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


SCHEMA_VERSION = "query_decision.v1"
RULES_VERSION = "technical-stage-matrix.dzt0340-2020.v1"
TECHNICAL_PROFILE_ID = "mineral_processing_test.v1"

GENERAL_STANDARD_NO = "DZ/T 0340-2020"
GENERAL_STANDARD_TITLE = "矿产勘查矿石加工选冶技术性能试验研究程度要求"
GENERAL_APPLICABILITY_CLAUSE = "6.1.1"

ORIGINAL_GENERAL_STANDARD_NO = "DZ/T 0079-2015"
ORIGINAL_GENERAL_STANDARD_TITLE = "固体矿产勘查地质资料综合整理综合研究技术要求"
ORIGINAL_GENERAL_STAGE_CLAUSE = "5.5.6.2"

ROCK_GOLD_STANDARD_NO = "DZ/T 0205-2020"
ROCK_GOLD_STANDARD_TITLE = "矿产地质勘查规范 岩金"
ROCK_GOLD_STAGE_CLAUSES = {
    "prospecting": "4.3.2",
    "detailed_investigation": "4.3.3",
    "exploration": "4.3.4",
}

PLACER_GOLD_STANDARD_NO = "DZ/T 0208-2020"
PLACER_GOLD_STANDARD_TITLE = "矿产地质勘查规范 金属砂矿类"
PLACER_GOLD_STAGE_CLAUSES = {
    "prospecting": "4.2.4",
    "detailed_investigation": "4.3.4",
    "exploration": "4.4.4",
}

STAGES = ("prospecting", "detailed_investigation", "exploration")
RESOURCE_SCALES = ("small", "medium", "large")
ORE_SELECTABILITY_VALUES = ("easy", "relatively_easy", "difficult", "new_type")
MINERAL_FORMS = ("rock_gold", "placer_gold")
BOOLEAN_VALUES = ("false", "true")

ALLOWED_EVIDENCE_CLAUSES: dict[str, frozenset[str]] = {
    GENERAL_STANDARD_NO: frozenset(
        {
            "6.1.1",
            "5.2.2",
            "6.3.1",
            "6.3.2",
            "6.3.3",
            "6.4.1",
            "6.4.2",
            "6.4.3",
            "6.4.4",
            "6.5.1",
            "6.5.2",
            "6.5.3",
            "6.5.4",
            "6.5.6",
        }
    ),
    ROCK_GOLD_STANDARD_NO: frozenset(ROCK_GOLD_STAGE_CLAUSES.values()),
    PLACER_GOLD_STANDARD_NO: frozenset(PLACER_GOLD_STAGE_CLAUSES.values()),
    ORIGINAL_GENERAL_STANDARD_NO: frozenset({ORIGINAL_GENERAL_STAGE_CLAUSE}),
}


class SlotState(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"
    NOT_APPLICABLE = "not_applicable"


class SlotOrigin(StrEnum):
    USER_EXPLICIT = "user_explicit"
    PROGRAM_NORMALIZED = "program_normalized"
    PROGRAM_FALLBACK = "program_fallback"
    MODEL_CANDIDATE = "model_candidate"
    UNKNOWN = "unknown"


class ValueRole(StrEnum):
    """How multiple mentions in one slot relate to each other."""

    UNSPECIFIED = "unspecified"
    SINGLE = "single"
    UNION = "union"
    COMPARISON = "comparison"
    CORRECTION = "correction"
    EXCLUSION = "exclusion"
    CONFLICT = "conflict"
    TRANSITION_SOURCE = "transition_source"
    TRANSITION_TARGET = "transition_target"


class AnchorState(StrEnum):
    LOCKED = "locked"
    UNRESOLVED = "unresolved"


class ResolutionMode(StrEnum):
    EXACT_BRANCH = "exact_branch"
    CONDITIONAL_MATRIX = "conditional_matrix"
    CLARIFICATION_REQUIRED = "clarification_required"
    NO_DIRECT_MATRIX_BRANCH = "no_direct_matrix_branch"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class SlotDecision:
    """A typed value together with its visible derivation.

    ``values`` is intentionally plural.  It supports explicit scopes such as
    ``大中型`` without turning a legitimate multi-value condition into a false
    conflict.  A conflicting extraction is represented by ``state=conflict``.
    """

    path: str
    values: tuple[str, ...]
    state: SlotState
    origin: SlotOrigin
    excluded_values: tuple[str, ...] = ()
    value_role: ValueRole = ValueRole.UNSPECIFIED
    source_text: str = ""
    rule_id: str = ""
    confidence: float = 0.0
    anchor_state: AnchorState = AnchorState.UNRESOLVED

    @property
    def value(self) -> str | None:
        return self.values[0] if len(self.values) == 1 else None

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QueryCore:
    primary_intent: str
    answer_focus: str
    requested_fields: tuple[str, ...]


@dataclass(frozen=True)
class TechnicalFacet:
    profile_id: str
    stage: SlotDecision
    resource_scale: SlotDecision
    ore_selectability: SlotDecision
    mineral: SlotDecision
    mineral_form: SlotDecision
    sample_collection_difficulty: SlotDecision
    expanded_continuous_test_required: SlotDecision
    current_resource_scale: SlotDecision
    target_resource_scale: SlotDecision
    route_id: str = "dzt0340_stage_matrix"

    @property
    def facts(self) -> tuple[SlotDecision, ...]:
        return (
            self.stage,
            self.resource_scale,
            self.ore_selectability,
            self.mineral,
            self.mineral_form,
            self.sample_collection_difficulty,
            self.expanded_continuous_test_required,
            self.current_resource_scale,
            self.target_resource_scale,
        )


@dataclass(frozen=True)
class ServiceFacet:
    """Minimal non-technical slots used only to prove profile isolation."""

    application_type: SlotDecision
    authority_relation: SlotDecision
    license_issuer_level: SlotDecision


@dataclass(frozen=True)
class QueryFacets:
    """Extensible facet envelope shared by future technical and service use."""

    technical: TechnicalFacet | None = None
    service: ServiceFacet | None = None


@dataclass(frozen=True)
class QuerySemantic:
    original_question: str
    normalized_question: str
    core: QueryCore
    facets: QueryFacets


@dataclass(frozen=True)
class TechnicalBranch:
    branch_id: str
    stage: str
    resource_scale: str
    ore_selectability: str
    standard_no: str
    clause_no: str


@dataclass(frozen=True)
class UnmappedCombination:
    stage: str
    resource_scale: str
    ore_selectability: str
    reason: str = "no_direct_general_matrix_branch"


@dataclass(frozen=True)
class EvidenceRef:
    standard_no: str
    title: str
    clause_no: str
    role: str
    scope: str = ""
    unit_id: str = ""


@dataclass(frozen=True)
class EvidenceGroup:
    group_id: str
    purpose: str
    operator: str
    required: bool
    refs: tuple[EvidenceRef, ...]
    activation_conditions: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceContract:
    contract_version: str
    resolution_mode: ResolutionMode
    missing_slots: tuple[str, ...]
    groups: tuple[EvidenceGroup, ...]
    stop_conditions: tuple[str, ...]
    specialist_scope_unresolved: bool = False

    @property
    def required_refs(self) -> tuple[EvidenceRef, ...]:
        return tuple(
            ref
            for group in self.groups
            if group.required and not group.activation_conditions
            for ref in group.refs
        )


@dataclass(frozen=True)
class QueryCompiled:
    resolution_mode: ResolutionMode
    compatible_branches: tuple[TechnicalBranch, ...]
    unmapped_combinations: tuple[UnmappedCombination, ...]
    evidence_contract: EvidenceContract


@dataclass(frozen=True)
class QueryAudit:
    schema_version: str
    rules_version: str
    original_question_sha256: str
    warnings: tuple[str, ...] = ()
    production_pipeline_affected: bool = False


@dataclass(frozen=True)
class QueryDecision:
    semantic: QuerySemantic
    compiled: QueryCompiled
    audit: QueryAudit

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def _truth_table() -> dict[tuple[str, str, str], str | None]:
    """Return the complete, declarative DZ/T 0340 stage matrix.

    ``None`` is deliberate: DZ/T 0340-2020 does not place new-type ore in a
    详查/勘探 matrix row.  It must not be silently treated as difficult ore.
    """

    table: dict[tuple[str, str, str], str | None] = {}

    # 6.3 普查阶段
    for scale in RESOURCE_SCALES:
        table[("prospecting", scale, "easy")] = "6.3.1"
        table[("prospecting", scale, "difficult")] = "6.3.2"
        table[("prospecting", scale, "new_type")] = "6.3.3"
    table[("prospecting", "small", "relatively_easy")] = "6.3.1"
    table[("prospecting", "medium", "relatively_easy")] = "6.3.1"
    table[("prospecting", "large", "relatively_easy")] = "6.3.2"

    # 6.4 详查阶段
    table.update(
        {
            ("detailed_investigation", "small", "easy"): "6.4.1",
            ("detailed_investigation", "medium", "easy"): "6.4.2",
            ("detailed_investigation", "large", "easy"): "6.4.2",
            ("detailed_investigation", "small", "relatively_easy"): "6.4.2",
            ("detailed_investigation", "medium", "relatively_easy"): "6.4.2",
            ("detailed_investigation", "large", "relatively_easy"): "6.4.3",
            ("detailed_investigation", "small", "difficult"): "6.4.3",
            ("detailed_investigation", "medium", "difficult"): "6.4.3",
            ("detailed_investigation", "large", "difficult"): "6.4.4",
        }
    )

    # 6.5 勘探阶段
    table.update(
        {
            ("exploration", "small", "easy"): "6.5.1",
            ("exploration", "medium", "easy"): "6.5.2",
            ("exploration", "large", "easy"): "6.5.2",
            ("exploration", "small", "relatively_easy"): "6.5.2",
            ("exploration", "medium", "relatively_easy"): "6.5.2",
            ("exploration", "large", "relatively_easy"): "6.5.3",
            ("exploration", "small", "difficult"): "6.5.2",
            ("exploration", "medium", "difficult"): "6.5.4",
            ("exploration", "large", "difficult"): "6.5.4",
        }
    )

    # Explicit negative rows prevent accidental new-type -> difficult fallback.
    for stage in ("detailed_investigation", "exploration"):
        for scale in RESOURCE_SCALES:
            table[(stage, scale, "new_type")] = None

    expected = {
        (stage, scale, ore)
        for stage in STAGES
        for scale in RESOURCE_SCALES
        for ore in ORE_SELECTABILITY_VALUES
    }
    if set(table) != expected:  # pragma: no cover - import-time invariant
        raise RuntimeError("technical stage truth table is incomplete")
    return table


TECHNICAL_STAGE_TRUTH_TABLE = _truth_table()


def _validate_evidence_registry() -> None:
    for clause in TECHNICAL_STAGE_TRUTH_TABLE.values():
        if clause is not None and clause not in ALLOWED_EVIDENCE_CLAUSES[GENERAL_STANDARD_NO]:
            raise RuntimeError(f"unapproved general matrix clause: {clause}")
    for standard_no, mapping in (
        (ROCK_GOLD_STANDARD_NO, ROCK_GOLD_STAGE_CLAUSES),
        (PLACER_GOLD_STANDARD_NO, PLACER_GOLD_STAGE_CLAUSES),
    ):
        if not set(mapping.values()) <= ALLOWED_EVIDENCE_CLAUSES[standard_no]:
            raise RuntimeError(f"unapproved specialist clause mapping: {standard_no}")


_validate_evidence_registry()


_STAGE_PATTERNS = {
    "prospecting": re.compile(r"普查(?:阶段)?"),
    "detailed_investigation": re.compile(r"详查(?:阶段)?"),
    "exploration": re.compile(r"勘探(?:阶段)?"),
}

_SCALE_LABEL_VALUES = {
    "小型": ("small",),
    "中型": ("medium",),
    "大型": ("large",),
    "中小型": ("small", "medium"),
    "大中型": ("medium", "large"),
    "小中大型": ("small", "medium", "large"),
}
_SCALE_LABEL_PATTERN = r"小中大型|中小型|大中型|小型|中型|大型"

_TECHNICAL_TERMS = (
    "选矿试验",
    "选冶试验",
    "加工选冶试验",
    "选冶技术性能",
    "矿石加工选冶",
    "试验研究程度",
    "可选性试验",
    "实验室流程试验",
    "扩大连续试验",
)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _ordered_values(values: set[str], order: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value for value in order if value in values)


@dataclass(frozen=True)
class _Mention:
    values: tuple[str, ...]
    raw_text: str
    start: int
    end: int
    negated: bool = False


_NEGATING_PREFIX_RE = re.compile(
    r"(?:不是|并非|不属于|不作为|不按|不要按|不回答|不要回答|"
    r"不讨论|不要讨论|无需考虑|不考虑|排除|而非|非|不)$"
)
_UNION_CONNECTOR_RE = re.compile(r"(?:、|和|及|或|以及|至|到|/|／)")
_COMPARISON_MARKER_RE = re.compile(r"(?:比较|对比|差异|分别|各自|与.+(?:相比|比较))")


def _mention_is_negated(text: str, start: int) -> bool:
    prefix = text[max(0, start - 12) : start]
    return bool(_NEGATING_PREFIX_RE.search(prefix))


def _slot_from_mentions(
    path: str,
    mentions: list[_Mention],
    *,
    order: tuple[str, ...],
    origin: SlotOrigin,
    rule_id: str,
    whole_text: str,
) -> SlotDecision:
    positives = [item for item in mentions if not item.negated]
    negatives = [item for item in mentions if item.negated]
    positive_values = _ordered_values(
        {value for item in positives for value in item.values}, order
    )
    excluded_values = _ordered_values(
        {value for item in negatives for value in item.values}, order
    )
    sources = tuple(dict.fromkeys(item.raw_text for item in mentions))

    if not positives:
        return SlotDecision(
            path=path,
            values=(),
            state=SlotState.UNKNOWN,
            origin=origin if negatives else SlotOrigin.UNKNOWN,
            excluded_values=excluded_values,
            value_role=(ValueRole.EXCLUSION if negatives else ValueRole.UNSPECIFIED),
            source_text="；".join(sources),
            rule_id=rule_id if negatives else "",
            confidence=1.0 if negatives else 0.0,
            anchor_state=AnchorState.LOCKED if negatives else AnchorState.UNRESOLVED,
        )

    if negatives:
        role = ValueRole.CORRECTION
        state = SlotState.KNOWN
    elif len(positive_values) == 1:
        role = ValueRole.SINGLE
        state = SlotState.KNOWN
    elif len(positives) == 1:
        # One compound expression such as 大中型 is an intentional union.
        role = ValueRole.UNION
        state = SlotState.KNOWN
    else:
        connectors = "".join(
            whole_text[left.end : right.start]
            for left, right in zip(positives, positives[1:])
        )
        if _COMPARISON_MARKER_RE.search(whole_text):
            role = ValueRole.COMPARISON
            state = SlotState.KNOWN
        elif _UNION_CONNECTOR_RE.search(connectors):
            role = ValueRole.UNION
            state = SlotState.KNOWN
        else:
            role = ValueRole.CONFLICT
            state = SlotState.CONFLICT

    return SlotDecision(
        path=path,
        values=positive_values,
        state=state,
        origin=origin,
        excluded_values=excluded_values,
        value_role=role,
        source_text="；".join(sources),
        rule_id=rule_id,
        confidence=1.0,
        anchor_state=(AnchorState.LOCKED if state == SlotState.KNOWN else AnchorState.UNRESOLVED),
    )


def _unknown_slot(
    path: str,
    *,
    source_text: str = "",
    excluded_values: tuple[str, ...] = (),
    rule_id: str = "",
) -> SlotDecision:
    return SlotDecision(
        path=path,
        values=(),
        state=SlotState.UNKNOWN,
        origin=(SlotOrigin.USER_EXPLICIT if source_text else SlotOrigin.UNKNOWN),
        excluded_values=excluded_values,
        value_role=(ValueRole.EXCLUSION if excluded_values else ValueRole.UNSPECIFIED),
        source_text=source_text,
        rule_id=rule_id,
        confidence=1.0 if source_text else 0.0,
        anchor_state=AnchorState.LOCKED if source_text else AnchorState.UNRESOLVED,
    )


def _known_slot(
    path: str,
    values: tuple[str, ...],
    *,
    origin: SlotOrigin,
    source_text: str,
    rule_id: str,
    confidence: float = 1.0,
    excluded_values: tuple[str, ...] = (),
    value_role: ValueRole | None = None,
) -> SlotDecision:
    return SlotDecision(
        path=path,
        values=values,
        state=SlotState.KNOWN,
        origin=origin,
        excluded_values=excluded_values,
        value_role=value_role or (ValueRole.SINGLE if len(values) == 1 else ValueRole.UNION),
        source_text=source_text,
        rule_id=rule_id,
        confidence=confidence,
        anchor_state=AnchorState.LOCKED,
    )


def extract_stage(text: str) -> SlotDecision:
    compact = _compact(text)
    mentions: list[_Mention] = []
    for value, pattern in _STAGE_PATTERNS.items():
        for match in pattern.finditer(compact):
            mentions.append(
                _Mention(
                    values=(value,),
                    raw_text=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    negated=_mention_is_negated(compact, match.start()),
                )
            )
    mentions.sort(key=lambda item: item.start)
    return _slot_from_mentions(
        "facets.technical.stage",
        mentions,
        order=STAGES,
        origin=SlotOrigin.USER_EXPLICIT,
        rule_id="technical-stage-explicit.v2",
        whole_text=compact,
    )


def _scale_values(label: str) -> tuple[str, ...]:
    return _SCALE_LABEL_VALUES[label]


def extract_resource_scale_transition(
    text: str,
) -> tuple[SlotDecision, SlotDecision] | None:
    """Return explicit current/target scale slots for a state transition."""

    compact = _compact(text)
    pattern = re.compile(
        rf"(?:资源量|资源储量)规模从(?P<source>{_SCALE_LABEL_PATTERN})"
        rf"(?:扩大|扩展|增加|调整|变化|变更|转变)?为(?P<target>{_SCALE_LABEL_PATTERN})"
    )
    match = pattern.search(compact)
    if not match:
        return None
    source = _known_slot(
        "facets.technical.current_resource_scale",
        _scale_values(match.group("source")),
        origin=SlotOrigin.USER_EXPLICIT,
        source_text=match.group(0),
        rule_id="resource-scale-transition.v1",
        value_role=ValueRole.TRANSITION_SOURCE,
    )
    target = _known_slot(
        "facets.technical.target_resource_scale",
        _scale_values(match.group("target")),
        origin=SlotOrigin.USER_EXPLICIT,
        source_text=match.group(0),
        rule_id="resource-scale-transition.v1",
        value_role=ValueRole.TRANSITION_TARGET,
    )
    return source, target


def extract_resource_scale(
    text: str,
    *,
    allow_bare_gold_fallback: bool = True,
    allow_small_gold_fallback: bool | None = None,
) -> SlotDecision:
    """Extract the resource-scale slot with approved business normalizations.

    Explicit resource-scale expressions and approved ``小/中/大型规模``
    aliases are parsed together so corrections remain visible.  Bare
    ``小/中/大型金矿`` is a separately governed fallback and is only enabled
    for a stage-specific mineral-processing question.
    """

    if allow_small_gold_fallback is not None:
        # Compatibility for the first shadow prototype.  The governed rule now
        # applies symmetrically to bare small/medium/large gold wording.
        allow_bare_gold_fallback = allow_small_gold_fallback

    transition = extract_resource_scale_transition(text)
    if transition is not None:
        _, target = transition
        return _known_slot(
            "facets.technical.resource_scale",
            target.values,
            origin=target.origin,
            source_text=target.source_text,
            rule_id=target.rule_id,
            value_role=ValueRole.TRANSITION_TARGET,
        )

    compact = _compact(text)
    label_pattern = _SCALE_LABEL_PATTERN
    explicit_pattern = re.compile(
        rf"(?:(?P<prefix>{label_pattern})(?:资源量|资源储量)规模|"
        rf"(?:资源量|资源储量)规模(?:为|是|属于)?(?P<suffix>{label_pattern}))"
    )
    alias_pattern = re.compile(rf"(?P<alias>{label_pattern})规模")
    mentions: list[_Mention] = []
    explicit_spans: list[tuple[int, int]] = []
    for match in explicit_pattern.finditer(compact):
        label = next((value for value in match.groupdict().values() if value), "")
        explicit_spans.append((match.start(), match.end()))
        mentions.append(
            _Mention(
                values=_scale_values(label),
                raw_text=match.group(0),
                start=match.start(),
                end=match.end(),
                negated=_mention_is_negated(compact, match.start()),
            )
        )
    for match in alias_pattern.finditer(compact):
        if any(start <= match.start() and match.end() <= end for start, end in explicit_spans):
            continue
        mentions.append(
            _Mention(
                values=_scale_values(match.group("alias")),
                raw_text=match.group(0),
                start=match.start(),
                end=match.end(),
                negated=_mention_is_negated(compact, match.start()),
            )
        )
    if mentions:
        mentions.sort(key=lambda item: item.start)
        return _slot_from_mentions(
            "facets.technical.resource_scale",
            mentions,
            order=RESOURCE_SCALES,
            origin=(SlotOrigin.USER_EXPLICIT if explicit_spans else SlotOrigin.PROGRAM_NORMALIZED),
            rule_id=(
                "resource-scale-explicit-or-approved-alias.v2"
                if explicit_spans
                else "resource-scale-short-size-approved.v2"
            ),
            whole_text=compact,
        )

    fallback_pattern = re.compile(
        rf"(?P<label>{label_pattern})(?:的)?"
        r"(?:岩金矿?|砂金矿?|沙金矿?|金矿)(?:床|山|项目)?"
    )
    has_stage = any(pattern.search(compact) for pattern in _STAGE_PATTERNS.values())
    has_technical_task = any(term in compact for term in _TECHNICAL_TERMS)
    if allow_bare_gold_fallback and has_stage and has_technical_task:
        fallback_mentions = [
            _Mention(
                values=_scale_values(match.group("label")),
                raw_text=match.group(0),
                start=match.start(),
                end=match.end(),
                negated=_mention_is_negated(compact, match.start()),
            )
            for match in fallback_pattern.finditer(compact)
        ]
        if fallback_mentions:
            return _slot_from_mentions(
                "facets.technical.resource_scale",
                fallback_mentions,
                order=RESOURCE_SCALES,
                origin=SlotOrigin.PROGRAM_FALLBACK,
                rule_id="bare-gold-resource-scale-fallback.v2",
                whole_text=compact,
            )
    return _unknown_slot("facets.technical.resource_scale")


def extract_ore_selectability(text: str) -> SlotDecision:
    compact = _compact(text)
    patterns = (
        ("new_type", re.compile(r"新类型(?:矿石|矿砂)?")),
        ("relatively_easy", re.compile(r"较易选(?:矿石|矿砂)?")),
        ("difficult", re.compile(r"难选(?:矿石|矿砂)?")),
        ("easy", re.compile(r"(?<!较)易选(?:矿石|矿砂)?")),
    )
    mentions: list[_Mention] = []
    for value, pattern in patterns:
        for match in pattern.finditer(compact):
            mentions.append(
                _Mention(
                    values=(value,),
                    raw_text=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    negated=_mention_is_negated(compact, match.start()),
                )
            )
    mentions.sort(key=lambda item: item.start)
    return _slot_from_mentions(
        "facets.technical.ore_selectability",
        mentions,
        order=ORE_SELECTABILITY_VALUES,
        origin=SlotOrigin.USER_EXPLICIT,
        rule_id="ore-selectability-explicit.v2",
        whole_text=compact,
    )


def extract_gold_slots(text: str) -> tuple[SlotDecision, SlotDecision]:
    compact = _compact(text)
    mineral_path = "facets.technical.mineral"
    form_path = "facets.technical.mineral_form"
    form_mentions: list[_Mention] = []
    for match in re.finditer(r"砂金|沙金|岩金", compact):
        raw = match.group(0)
        form_mentions.append(
            _Mention(
                values=(("rock_gold",) if raw == "岩金" else ("placer_gold",)),
                raw_text=raw,
                start=match.start(),
                end=match.end(),
                negated=_mention_is_negated(compact, match.start()),
            )
        )
    form_mentions.sort(key=lambda item: item.start)
    positive_forms = [item for item in form_mentions if not item.negated]

    generic_mentions = []
    for match in re.finditer(r"(?<!岩)(?<!砂)(?<!沙)金矿(?:床|山|项目)?", compact):
        generic_mentions.append(
            _Mention(
                values=("gold",),
                raw_text=match.group(0),
                start=match.start(),
                end=match.end(),
                negated=_mention_is_negated(compact, match.start()),
            )
        )
    positive_generic = [item for item in generic_mentions if not item.negated]

    if positive_forms or positive_generic:
        source_text = "；".join(
            dict.fromkeys(
                item.raw_text
                for item in (*positive_forms, *positive_generic)
            )
        )
        mineral = _known_slot(
            mineral_path,
            ("gold",),
            origin=SlotOrigin.USER_EXPLICIT,
            source_text=source_text,
            rule_id="gold-mineral-explicit.v2",
        )
        if positive_forms:
            form_origin = (
                SlotOrigin.PROGRAM_NORMALIZED
                if all(item.raw_text == "沙金" for item in positive_forms)
                else SlotOrigin.USER_EXPLICIT
            )
            form = _slot_from_mentions(
                form_path,
                form_mentions,
                order=MINERAL_FORMS,
                origin=form_origin,
                rule_id="gold-form-explicit-and-correction.v2",
                whole_text=compact,
            )
        else:
            excluded = _ordered_values(
                {
                    value
                    for item in form_mentions
                    if item.negated
                    for value in item.values
                },
                MINERAL_FORMS,
            )
            form = _unknown_slot(
                form_path,
                source_text="；".join(
                    dict.fromkeys(
                        item.raw_text for item in (*positive_generic, *form_mentions)
                    )
                ),
                excluded_values=excluded,
                rule_id="generic-gold-form-unresolved.v2",
            )
        return mineral, form
    return _unknown_slot(mineral_path), _unknown_slot(form_path)


def extract_sample_collection_difficulty(text: str) -> SlotDecision:
    compact = _compact(text)
    path = "facets.technical.sample_collection_difficulty"
    patterns = (
        ("false", re.compile(r"样品(?:采集|采取)(?:没有|不存在|无)困难|样品(?:容易|便于)采集")),
        ("true", re.compile(r"样品(?:采集|采取)(?:确有)?困难|样品难以(?:采集|采取)")),
    )
    mentions: list[_Mention] = []
    for value, pattern in patterns:
        for match in pattern.finditer(compact):
            mentions.append(
                _Mention(
                    values=(value,),
                    raw_text=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    negated=_mention_is_negated(compact, match.start()),
                )
            )
    mentions.sort(key=lambda item: item.start)
    return _slot_from_mentions(
        path,
        mentions,
        order=BOOLEAN_VALUES,
        origin=SlotOrigin.USER_EXPLICIT,
        rule_id="sample-collection-difficulty-explicit.v1",
        whole_text=compact,
    )


def extract_expanded_continuous_test_required(text: str) -> SlotDecision:
    compact = _compact(text)
    path = "facets.technical.expanded_continuous_test_required"
    patterns = (
        (
            "false",
            re.compile(r"(?:无需|不需要|不必)(?:进行|开展|做)?(?:实验室)?扩大连续试验"),
        ),
        (
            "true",
            re.compile(r"(?:需要|需|应|本应|必须)(?:进行|开展|做)?(?:实验室)?扩大连续试验"),
        ),
    )
    mentions: list[_Mention] = []
    for value, pattern in patterns:
        for match in pattern.finditer(compact):
            mentions.append(
                _Mention(
                    values=(value,),
                    raw_text=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    negated=False,
                )
            )
    mentions.sort(key=lambda item: item.start)
    return _slot_from_mentions(
        path,
        mentions,
        order=BOOLEAN_VALUES,
        origin=SlotOrigin.USER_EXPLICIT,
        rule_id="expanded-continuous-test-required.v1",
        whole_text=compact,
    )


_TECHNICAL_REQUEST_RE = re.compile(
    r"(?:要求|规定|做到什么程度|达到什么程度|达到何种|开展到|进行到|"
    r"至少应|至少要|应该做到|应当做到|需要做到|应进行|应开展|"
    r"哪一级试验|何种试验|什么类型或规模|是否满足|能否满足|"
    r"应如何处理|如何处理|怎么处理)"
)
_NON_TECHNICAL_REQUEST_RES = (
    re.compile(r"(?:申请|办理|延续|新立|变更|注销).{0,18}(?:材料|资料|手续|流程|受理|提交什么)"),
    re.compile(r"(?:哪个|哪份|该).{0,12}(?:标准|文件).{0,12}(?:废止|现行|有效|含有|包含|提到)"),
    re.compile(r"(?:翻译|改写|校对).{0,12}(?:勘探|详查|普查|选矿|选冶)"),
)
_ALLOWED_TECHNICAL_INTENTS = frozenset(
    {
        "technical_stage_requirement",
        "technical_method",
        "technical_requirement_sufficiency",
    }
)


def classify_shadow_primary_intent(text: str) -> str:
    """Conservative shadow-only gate used to demonstrate profile isolation."""

    compact = _compact(text)
    if (
        any(term in compact for term in ("提交储量", "估算矿产资源储量", "资源量转为储量"))
        and any(term in compact for term in ("依据", "可研", "技术经济", "开发利用方案", "材料"))
    ):
        return "reserve_estimation_basis"
    if any(term in compact for term in ("评审备案", "储量报告评审", "储量评审")) and any(
        term in compact for term in ("哪个机构", "哪一级", "向哪里", "向哪一级", "谁负责")
    ):
        return "authority_jurisdiction"
    if any(term in compact for term in ("材料", "资料", "报件")) and any(
        term in compact for term in ("申请", "提交", "新立", "延续", "变更", "注销", "评审备案")
    ):
        return "service_materials"
    if any(term in compact for term in ("废止", "现行", "有效吗", "是否有效", "替代")) and any(
        term in compact for term in ("标准", "文件", "规范")
    ):
        return "status_verification"
    if is_technical_stage_question(compact):
        return "technical_stage_requirement"
    return "unclassified"


def _service_slot(
    path: str,
    value: str | None,
    *,
    source_text: str = "",
    rule_id: str,
) -> SlotDecision:
    if value is None:
        return _unknown_slot(path)
    return _known_slot(
        path,
        (value,),
        origin=SlotOrigin.USER_EXPLICIT,
        source_text=source_text,
        rule_id=rule_id,
    )


def extract_service_facet(text: str, primary_intent: str) -> ServiceFacet | None:
    if primary_intent not in {"service_materials", "service_workflow", "authority_jurisdiction"}:
        return None
    compact = _compact(text)
    application_type: str | None = None
    if any(term in compact for term in ("新立", "首次申请", "首次登记")):
        application_type = "new"
    elif any(term in compact for term in ("延续", "续期")):
        application_type = "renewal"
    elif any(term in compact for term in ("变更", "转让", "扩大范围", "缩小范围")):
        application_type = "change"
    elif "注销" in compact:
        application_type = "cancellation"

    authority_relation = (
        "reserve_filing"
        if any(term in compact for term in ("评审备案", "储量报告评审", "储量评审"))
        else None
    )
    issuer: str | None = None
    if re.search(r"(?:省|自治区|直辖市).{0,12}(?:颁发|发放).{0,8}采矿许可证", compact):
        issuer = "province"
    elif re.search(r"(?:自然资源部|部里|部).{0,12}(?:颁发|发放).{0,8}采矿许可证", compact):
        issuer = "ministry"
    return ServiceFacet(
        application_type=_service_slot(
            "facets.service.application_type",
            application_type,
            source_text=application_type or "",
            rule_id="service-application-type.v1",
        ),
        authority_relation=_service_slot(
            "facets.service.authority_relation",
            authority_relation,
            source_text=authority_relation or "",
            rule_id="service-authority-relation.v1",
        ),
        license_issuer_level=_service_slot(
            "facets.service.license_issuer_level",
            issuer,
            source_text=issuer or "",
            rule_id="service-license-issuer.v1",
        ),
    )


def _active_request_text(text: str) -> str:
    """Drop explicitly rejected request clauses before conservative intent gating."""

    kept: list[str] = []
    for segment in re.split(r"[。；;，,]", _compact(text)):
        if not segment:
            continue
        if re.match(r"^(?:不要|无需|不用|不必)(?:回答|讨论|查询|查证|考虑)?", segment):
            positive = re.search(r"只(?:回答|讨论|查询|查证|考虑|查)?(.+)$", segment)
            if positive and positive.group(1):
                kept.append(positive.group(1))
            continue
        kept.append(segment)
    return "，".join(kept)


def is_technical_stage_question(
    text: str,
    *,
    primary_intent: str | None = None,
) -> bool:
    if primary_intent is not None and primary_intent not in _ALLOWED_TECHNICAL_INTENTS:
        return False
    active = _active_request_text(text)
    if any(pattern.search(active) for pattern in _NON_TECHNICAL_REQUEST_RES):
        return False
    if not any(term in active for term in _TECHNICAL_TERMS):
        return False
    stage = extract_stage(active)
    if not stage.values or stage.state == SlotState.CONFLICT:
        return False
    # A governed upstream intent is sufficient.  Standalone shadow compilation
    # remains conservative and also requires an explicit requirement relation.
    return primary_intent in _ALLOWED_TECHNICAL_INTENTS or bool(
        _TECHNICAL_REQUEST_RE.search(active)
    )


def normalize_question(text: str, facet: TechnicalFacet | None = None) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().replace("沙金", "砂金")
    replacements = (
        ("小中大型规模", "小型、中型和大型资源量规模"),
        ("中小型规模", "中小型资源量规模"),
        ("大中型规模", "大中型资源量规模"),
        ("小型规模", "小型资源量规模"),
        ("中型规模", "中型资源量规模"),
        ("大型规模", "大型资源量规模"),
    )
    for source, target in replacements:
        normalized = normalized.replace(source, target)
    if (
        facet
        and facet.resource_scale.origin == SlotOrigin.PROGRAM_FALLBACK
        and facet.resource_scale.value in RESOURCE_SCALES
    ):
        label = {
            "small": "小型",
            "medium": "中型",
            "large": "大型",
        }[facet.resource_scale.value]
        normalized = re.sub(
            rf"{label}(?:的)?(?=(?:岩金矿?|砂金矿?|金矿))",
            f"{label}资源量规模的",
            normalized,
            count=1,
        )
    return normalized


def extract_technical_facet(
    text: str,
    *,
    primary_intent: str | None = None,
) -> TechnicalFacet | None:
    if not is_technical_stage_question(text, primary_intent=primary_intent):
        return None
    mineral, mineral_form = extract_gold_slots(text)
    transition = extract_resource_scale_transition(text)
    if transition is None:
        current_scale = _unknown_slot("facets.technical.current_resource_scale")
        target_scale = _unknown_slot("facets.technical.target_resource_scale")
    else:
        current_scale, target_scale = transition
    compact = _compact(text)
    if primary_intent == "technical_requirement_sufficiency":
        route_id = "technical_requirement_sufficiency"
    elif (
        "固体矿产勘查" in compact
        and "一般矿石" in compact
        and "加工选冶试验" in compact
    ):
        route_id = "dzt0079_general_ore_stage"
    else:
        route_id = "dzt0340_stage_matrix"
    return TechnicalFacet(
        profile_id=TECHNICAL_PROFILE_ID,
        stage=extract_stage(text),
        resource_scale=extract_resource_scale(text),
        ore_selectability=extract_ore_selectability(text),
        mineral=mineral,
        mineral_form=mineral_form,
        sample_collection_difficulty=extract_sample_collection_difficulty(text),
        expanded_continuous_test_required=extract_expanded_continuous_test_required(text),
        current_resource_scale=current_scale,
        target_resource_scale=target_scale,
        route_id=route_id,
    )


def _slot_candidates(slot: SlotDecision, domain: tuple[str, ...]) -> tuple[str, ...]:
    if slot.state == SlotState.CONFLICT:
        return ()
    source = slot.values if slot.values else domain
    excluded = set(slot.excluded_values)
    return tuple(value for value in domain if value in source and value not in excluded)


def partition_technical_branches(
    facet: TechnicalFacet,
) -> tuple[tuple[TechnicalBranch, ...], tuple[UnmappedCombination, ...]]:
    if any(
        slot.state == SlotState.CONFLICT
        for slot in (facet.stage, facet.resource_scale, facet.ore_selectability)
    ):
        return (), ()
    stages = _slot_candidates(facet.stage, STAGES)
    scales = _slot_candidates(facet.resource_scale, RESOURCE_SCALES)
    ore_values = _slot_candidates(facet.ore_selectability, ORE_SELECTABILITY_VALUES)
    branches: list[TechnicalBranch] = []
    unmapped: list[UnmappedCombination] = []
    seen: set[tuple[str, str, str, str]] = set()
    for stage in stages:
        for scale in scales:
            for ore in ore_values:
                clause = TECHNICAL_STAGE_TRUTH_TABLE[(stage, scale, ore)]
                if clause is None:
                    unmapped.append(
                        UnmappedCombination(
                            stage=stage,
                            resource_scale=scale,
                            ore_selectability=ore,
                        )
                    )
                    continue
                key = (stage, scale, ore, clause)
                if key in seen:
                    continue
                seen.add(key)
                branches.append(
                    TechnicalBranch(
                        branch_id=f"{stage}.{scale}.{ore}",
                        stage=stage,
                        resource_scale=scale,
                        ore_selectability=ore,
                        standard_no=GENERAL_STANDARD_NO,
                        clause_no=clause,
                    )
                )
    return tuple(branches), tuple(unmapped)


def compatible_technical_branches(facet: TechnicalFacet) -> tuple[TechnicalBranch, ...]:
    """Compatibility wrapper retained for the first shadow A/B runner."""

    return partition_technical_branches(facet)[0]


def _resolution_mode(
    facet: TechnicalFacet,
    branches: tuple[TechnicalBranch, ...],
    unmapped: tuple[UnmappedCombination, ...],
) -> ResolutionMode:
    decision_slots = (facet.stage, facet.resource_scale, facet.ore_selectability)
    if any(slot.state == SlotState.CONFLICT for slot in decision_slots):
        return ResolutionMode.CLARIFICATION_REQUIRED
    if all(slot.state == SlotState.KNOWN and len(slot.values) == 1 for slot in decision_slots):
        return (
            ResolutionMode.EXACT_BRANCH
            if len(branches) == 1 and not unmapped
            else ResolutionMode.NO_DIRECT_MATRIX_BRANCH
        )
    return ResolutionMode.CONDITIONAL_MATRIX if branches else ResolutionMode.NO_DIRECT_MATRIX_BRANCH


def _evidence_ref(
    *,
    standard_no: str,
    title: str,
    clause_no: str,
    role: str,
    scope: str = "",
    unit_id: str = "",
) -> EvidenceRef:
    if clause_no not in ALLOWED_EVIDENCE_CLAUSES.get(standard_no, frozenset()):
        raise ValueError(f"evidence clause is outside the governed registry: {standard_no}#{clause_no}")
    return EvidenceRef(
        standard_no=standard_no,
        title=title,
        clause_no=clause_no,
        role=role,
        scope=scope,
        unit_id=unit_id,
    )


def _specialist_group(facet: TechnicalFacet) -> EvidenceGroup | None:
    stages = _slot_candidates(facet.stage, STAGES)
    if not stages or facet.mineral.value != "gold":
        return None
    form_candidates = _slot_candidates(facet.mineral_form, MINERAL_FORMS)
    refs: list[EvidenceRef] = []
    for stage in stages:
        if "rock_gold" in form_candidates:
            refs.append(
                _evidence_ref(
                    standard_no=ROCK_GOLD_STANDARD_NO,
                    title=ROCK_GOLD_STANDARD_TITLE,
                    clause_no=ROCK_GOLD_STAGE_CLAUSES[stage],
                    role="mineral_specific_requirement",
                    scope="rock_gold",
                )
            )
        if "placer_gold" in form_candidates:
            refs.append(
                _evidence_ref(
                    standard_no=PLACER_GOLD_STANDARD_NO,
                    title=PLACER_GOLD_STANDARD_TITLE,
                    clause_no=PLACER_GOLD_STAGE_CLAUSES[stage],
                    role="mineral_specific_requirement",
                    scope="placer_gold",
                )
            )
    if not refs:
        return None
    scope_unresolved = facet.mineral_form.state == SlotState.UNKNOWN
    return EvidenceGroup(
        group_id="mineral_specific_cross_check",
        purpose="核对适用矿种专项规范；泛称金矿不得预先等同岩金或砂金",
        operator="route_by_scope" if scope_unresolved else "all_of",
        required=not scope_unresolved,
        refs=tuple(refs),
        activation_conditions=(
            ("facets.technical.mineral_form must be resolved",)
            if scope_unresolved
            else ()
        ),
    )


def _specialist_can_require_expanded_continuous_test(facet: TechnicalFacet) -> bool:
    if facet.mineral.value != "gold" or "exploration" not in _slot_candidates(facet.stage, STAGES):
        return False
    forms = _slot_candidates(facet.mineral_form, MINERAL_FORMS)
    ores = _slot_candidates(facet.ore_selectability, ORE_SELECTABILITY_VALUES)
    # DZ/T 0205-2020 4.3.4: rock-gold easy/relatively-easy may need an
    # expanded continuous test; difficult/new-type ores require one.
    if "rock_gold" in forms and ores:
        return True
    # DZ/T 0208-2020 4.4.4: placer difficult/new-type ore explicitly uses it.
    return "placer_gold" in forms and bool({"difficult", "new_type"} & set(ores))


def build_evidence_contract(
    facet: TechnicalFacet,
    branches: tuple[TechnicalBranch, ...],
    unmapped: tuple[UnmappedCombination, ...] = (),
) -> EvidenceContract:
    mode = _resolution_mode(facet, branches, unmapped)
    missing_slots = tuple(
        slot.path
        for slot in (facet.stage, facet.resource_scale, facet.ore_selectability)
        if slot.state == SlotState.UNKNOWN
    )
    groups: list[EvidenceGroup] = []
    protected_original_route = facet.route_id in {
        "dzt0079_general_ore_stage",
        "technical_requirement_sufficiency",
    }
    if protected_original_route:
        if facet.route_id == "dzt0079_general_ore_stage":
            groups.append(
                EvidenceGroup(
                    group_id="original_general_ore_stage_route",
                    purpose="保留原问题已命中的固体矿产一般矿石阶段要求",
                    operator="all_of",
                    required=True,
                    refs=(
                        _evidence_ref(
                            standard_no=ORIGINAL_GENERAL_STANDARD_NO,
                            title=ORIGINAL_GENERAL_STANDARD_TITLE,
                            clause_no=ORIGINAL_GENERAL_STAGE_CLAUSE,
                            role="original_route",
                        ),
                    ),
                )
            )
        else:
            groups.append(
                EvidenceGroup(
                    group_id="technical_level_relationship",
                    purpose="保留高等级试验是否覆盖低等级要求所需的直接层级关系证据",
                    operator="all_of",
                    required=True,
                    refs=(
                        _evidence_ref(
                            standard_no=GENERAL_STANDARD_NO,
                            title=GENERAL_STANDARD_TITLE,
                            clause_no="5.2.2",
                            role="technical_level_relationship",
                        ),
                    ),
                )
            )
        supplemental_clauses = (
            GENERAL_APPLICABILITY_CLAUSE,
            *tuple(sorted({branch.clause_no for branch in branches})),
        )
        groups.append(
            EvidenceGroup(
                group_id="supplemental_general_stage_matrix",
                purpose="新增阶段矩阵仅作补充，不替代原有直接证据路线",
                operator="supplemental_only",
                required=False,
                refs=tuple(
                    _evidence_ref(
                        standard_no=GENERAL_STANDARD_NO,
                        title=GENERAL_STANDARD_TITLE,
                        clause_no=clause,
                        role="supplemental_matrix",
                    )
                    for clause in dict.fromkeys(supplemental_clauses)
                ),
                activation_conditions=(
                    "general_stage_matrix_is_useful_without_replacing_original_route",
                ),
            )
        )
    else:
        if facet.stage.state == SlotState.KNOWN:
            groups.append(
                EvidenceGroup(
                    group_id="general_applicability",
                    purpose="证明试验程度由勘查阶段、选冶难易程度和资源量规模共同决定",
                    operator="all_of",
                    required=True,
                    refs=(
                        _evidence_ref(
                            standard_no=GENERAL_STANDARD_NO,
                            title=GENERAL_STANDARD_TITLE,
                            clause_no=GENERAL_APPLICABILITY_CLAUSE,
                            role="applicability",
                        ),
                    ),
                )
            )
        clause_numbers = tuple(sorted({branch.clause_no for branch in branches}))
        if clause_numbers:
            groups.append(
                EvidenceGroup(
                    group_id="compatible_stage_matrix",
                    purpose="覆盖所有且仅覆盖与已知条件兼容的阶段矩阵分支",
                    operator="all_of",
                    required=True,
                    refs=tuple(
                        _evidence_ref(
                            standard_no=GENERAL_STANDARD_NO,
                            title=GENERAL_STANDARD_TITLE,
                            clause_no=clause,
                            role="matrix_branch",
                        )
                        for clause in clause_numbers
                    ),
                )
            )
    specialist = _specialist_group(facet)
    if specialist:
        groups.append(specialist)

    # 6.5.6 is never a matrix row.  It becomes unconditionally required only
    # when sample difficulty is explicit and every surviving matrix branch is
    # one that can require an expanded continuous test.  Otherwise it remains
    # a visibly conditional candidate and cannot leak into required_refs.
    relevant_sampling_branches = tuple(
        branch for branch in branches if branch.clause_no in {"6.5.3", "6.5.4"}
    )
    exploration_in_scope = "exploration" in _slot_candidates(facet.stage, STAGES)
    sampling_value = facet.sample_collection_difficulty.value
    expanded_value = facet.expanded_continuous_test_required.value
    expanded_may_be_required = bool(
        relevant_sampling_branches
        or _specialist_can_require_expanded_continuous_test(facet)
    )
    sampling_required = bool(
        exploration_in_scope
        and sampling_value == "true"
        and expanded_value == "true"
    )
    sampling_conditional = bool(
        sampling_value != "false"
        and expanded_value != "false"
        and exploration_in_scope
        and not sampling_required
        and (expanded_may_be_required or expanded_value == "true")
    )
    if sampling_required or sampling_conditional:
        groups.append(
            EvidenceGroup(
                group_id="sampling_difficulty_exception",
                purpose="仅在需要扩大连续试验且样品采集确有困难时核验例外",
                operator="all_of" if sampling_required else "required_when",
                required=sampling_required,
                refs=(
                    _evidence_ref(
                        standard_no=GENERAL_STANDARD_NO,
                        title=GENERAL_STANDARD_TITLE,
                        clause_no="6.5.6",
                        role="exception",
                    ),
                ),
                activation_conditions=(
                    ()
                    if sampling_required
                    else (
                        "expanded_continuous_test_required_and_sample_collection_difficult",
                    )
                ),
            )
        )

    stop_conditions: list[str] = []
    if mode == ResolutionMode.NO_DIRECT_MATRIX_BRANCH:
        stop_conditions.append("no_direct_general_matrix_branch_do_not_map_new_type_to_difficult")
    if mode == ResolutionMode.CLARIFICATION_REQUIRED:
        stop_conditions.append("conflicting_decision_slots_require_resolution")
    if unmapped and branches:
        stop_conditions.append("partial_matrix_gap_must_remain_visible")
    if facet.mineral.value == "gold" and facet.mineral_form.state == SlotState.UNKNOWN:
        stop_conditions.append("specialist_scope_difference_must_be_disclosed_or_confirmed")
    return EvidenceContract(
        contract_version="evidence_contract.technical_stage.v1",
        resolution_mode=mode,
        missing_slots=missing_slots,
        groups=tuple(groups),
        stop_conditions=tuple(stop_conditions),
        specialist_scope_unresolved=(
            facet.mineral.value == "gold"
            and facet.mineral_form.state == SlotState.UNKNOWN
        ),
    )


def compile_query_decision(
    question: str,
    *,
    upstream_intent: str | None = None,
    primary_intent: str | None = None,
    answer_focus: str | None = None,
) -> QueryDecision:
    """Compile one question into the shadow ``query_decision.v1`` contract."""

    original = str(question or "").strip()
    resolved_primary_intent = (
        str(upstream_intent or primary_intent or "").strip()
        or classify_shadow_primary_intent(original)
    )
    facet = extract_technical_facet(
        original,
        primary_intent=resolved_primary_intent,
    )
    service_facet = extract_service_facet(original, resolved_primary_intent)
    if facet is None:
        semantic = QuerySemantic(
            original_question=original,
            normalized_question=re.sub(r"\s+", " ", original).strip().replace("沙金", "砂金"),
            core=QueryCore(
                primary_intent=resolved_primary_intent,
                answer_focus=answer_focus or "unknown",
                requested_fields=(),
            ),
            facets=QueryFacets(service=service_facet),
        )
        mode = ResolutionMode.NOT_APPLICABLE
        contract = EvidenceContract(
            contract_version="evidence_contract.none.v1",
            resolution_mode=mode,
            missing_slots=(),
            groups=(),
            stop_conditions=(),
        )
        warnings = ("shadow compiler currently implements only mineral_processing_test.v1",)
        branches: tuple[TechnicalBranch, ...] = ()
        unmapped: tuple[UnmappedCombination, ...] = ()
    else:
        semantic = QuerySemantic(
            original_question=original,
            normalized_question=normalize_question(original, facet),
            core=QueryCore(
                primary_intent=resolved_primary_intent,
                answer_focus=(
                    answer_focus
                    or (
                        "technical_requirement_sufficiency"
                        if resolved_primary_intent == "technical_requirement_sufficiency"
                        else "technical_requirement"
                    )
                ),
                requested_fields=("required_test_research_level",),
            ),
            facets=QueryFacets(technical=facet),
        )
        branches, unmapped = partition_technical_branches(facet)
        contract = build_evidence_contract(facet, branches, unmapped)
        mode = contract.resolution_mode
        warnings_list: list[str] = []
        if facet.resource_scale.origin == SlotOrigin.PROGRAM_FALLBACK:
            warnings_list.append(
                "bare gold size wording was routed to resource scale by approved fallback"
            )
        if facet.mineral.value == "gold" and facet.mineral_form.state == SlotState.UNKNOWN:
            warnings_list.append("generic gold scope preserved; rock-gold and placer-gold routes remain distinct")
        if branches and unmapped:
            warnings_list.append("partial_matrix_gap")
        if mode == ResolutionMode.NO_DIRECT_MATRIX_BRANCH:
            warnings_list.append("no direct DZ/T 0340 matrix row matches all known conditions")
        warnings = tuple(warnings_list)

    return QueryDecision(
        semantic=semantic,
        compiled=QueryCompiled(
            resolution_mode=mode,
            compatible_branches=branches,
            unmapped_combinations=unmapped,
            evidence_contract=contract,
        ),
        audit=QueryAudit(
            schema_version=SCHEMA_VERSION,
            rules_version=RULES_VERSION,
            original_question_sha256=hashlib.sha256(original.encode("utf-8")).hexdigest(),
            warnings=warnings,
        ),
    )


# The longer name makes shadow-only call sites self-documenting.  Keeping a
# short compiler name above also gives the future production adapter a stable
# API if the experiment is accepted.
compile_shadow_query_decision = compile_query_decision


__all__ = [
    "ALLOWED_EVIDENCE_CLAUSES",
    "AnchorState",
    "EvidenceContract",
    "EvidenceGroup",
    "EvidenceRef",
    "ORE_SELECTABILITY_VALUES",
    "QueryDecision",
    "ResolutionMode",
    "RESOURCE_SCALES",
    "RULES_VERSION",
    "SCHEMA_VERSION",
    "STAGES",
    "SlotDecision",
    "SlotOrigin",
    "SlotState",
    "ServiceFacet",
    "TECHNICAL_STAGE_TRUTH_TABLE",
    "TechnicalBranch",
    "TechnicalFacet",
    "UnmappedCombination",
    "ValueRole",
    "build_evidence_contract",
    "compatible_technical_branches",
    "classify_shadow_primary_intent",
    "compile_query_decision",
    "compile_shadow_query_decision",
    "extract_gold_slots",
    "extract_expanded_continuous_test_required",
    "extract_ore_selectability",
    "extract_resource_scale",
    "extract_resource_scale_transition",
    "extract_sample_collection_difficulty",
    "extract_stage",
    "extract_technical_facet",
    "normalize_question",
    "partition_technical_branches",
]
