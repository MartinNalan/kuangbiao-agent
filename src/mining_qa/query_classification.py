from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from typing import Any


CLASSIFICATION_VERSION = "1.0"

PRIMARY_INTENTS = {
    "standard_catalog_lookup",
    "status_verification",
    "clause_lookup",
    "definition_lookup",
    "numeric_table_lookup",
    "service_materials",
    "service_workflow",
    "authority_jurisdiction",
    "eligibility_condition",
    "cross_document_comparison",
    "document_inventory",
    "technical_method",
    "out_of_scope",
}


@dataclass(frozen=True)
class RetrievalStrategy:
    strategy_id: str
    document_types: tuple[str, ...]
    evidence_slots: tuple[str, ...]
    output_shape: str
    search_mode: str = "default"
    deep_recommended: bool = False


STRATEGY_REGISTRY: dict[str, RetrievalStrategy] = {
    "standard_catalog_lookup": RetrievalStrategy(
        strategy_id="catalog_then_scope",
        document_types=("standard", "national_standard", "industry_standard", "policy_document", "guidance"),
        evidence_slots=("正式标题", "标准号或文号", "文件类型", "现行状态", "官方链接", "适用范围"),
        output_shape="document_list",
        search_mode="catalog",
    ),
    "status_verification": RetrievalStrategy(
        strategy_id="official_metadata_status",
        document_types=("standard", "national_standard", "industry_standard", "policy_document"),
        evidence_slots=("状态", "发布日期", "实施日期", "废止日期", "替代关系", "官方链接"),
        output_shape="status_summary",
        search_mode="catalog",
    ),
    "clause_lookup": RetrievalStrategy(
        strategy_id="hard_scope_clause",
        document_types=("standard", "national_standard", "industry_standard", "policy_document", "law", "regulation", "guidance"),
        evidence_slots=("目标文件", "条款号", "直接原文", "适用条件", "官方链接"),
        output_shape="clause_answer",
        search_mode="scoped",
    ),
    "definition_lookup": RetrievalStrategy(
        strategy_id="definition_first",
        document_types=("standard", "national_standard", "industry_standard"),
        evidence_slots=("术语", "直接定义", "条款号", "适用语境", "官方链接"),
        output_shape="definition",
        search_mode="scoped",
    ),
    "numeric_table_lookup": RetrievalStrategy(
        strategy_id="schema_table_cell",
        document_types=("standard", "national_standard", "industry_standard", "service_guide"),
        evidence_slots=("表名", "目标行", "目标列", "数值", "单位", "表注", "适用条件"),
        output_shape="structured_table",
        search_mode="scoped",
    ),
    "service_materials": RetrievalStrategy(
        strategy_id="service_material_inventory",
        document_types=("service_guide", "administrative_service_guide", "policy_attachment", "policy_document"),
        evidence_slots=("适用事项", "办理类型", "材料目录", "必交或按情形", "提交形式", "免提交说明"),
        output_shape="numbered_materials",
        search_mode="scoped",
    ),
    "service_workflow": RetrievalStrategy(
        strategy_id="stage_relation_workflow",
        document_types=("service_guide", "administrative_service_guide", "policy_attachment", "policy_document", "regulation"),
        evidence_slots=("适用范围", "当前阶段", "待办事项", "办理顺序", "办理结果", "条件"),
        output_shape="ordered_procedure",
        search_mode="scoped",
    ),
    "authority_jurisdiction": RetrievalStrategy(
        strategy_id="authority_relation",
        document_types=("policy_document", "law", "regulation", "department_rule", "service_guide"),
        evidence_slots=("责任主体", "权限关系", "具体事项", "决定条件", "例外", "核验方式"),
        output_shape="conditional_conclusion",
        search_mode="scoped",
    ),
    "eligibility_condition": RetrievalStrategy(
        strategy_id="condition_matrix",
        document_types=("policy_document", "law", "regulation", "department_rule", "guidance", "standard", "industry_standard"),
        evidence_slots=("目标动作", "一般条件", "特殊条件", "限制", "例外", "待确认条件"),
        output_shape="condition_matrix",
        search_mode="comparison",
    ),
    "cross_document_comparison": RetrievalStrategy(
        strategy_id="enumerate_then_compare",
        document_types=("standard", "national_standard", "industry_standard", "policy_document", "law", "regulation", "guidance"),
        evidence_slots=("比较主题", "候选范围", "统一维度", "直接条款", "具体差异", "适用条件", "覆盖率"),
        output_shape="comparison_matrix",
        search_mode="comparison",
        deep_recommended=True,
    ),
    "document_inventory": RetrievalStrategy(
        strategy_id="stage_profession_inventory",
        document_types=("standard", "national_standard", "industry_standard", "policy_document", "service_guide", "guidance"),
        evidence_slots=("项目阶段", "专业", "文件", "用途", "现行状态", "适用层级", "缺口"),
        output_shape="grouped_inventory",
        search_mode="exhaustive",
        deep_recommended=True,
    ),
    "technical_method": RetrievalStrategy(
        strategy_id="requirements_then_advice",
        document_types=("standard", "national_standard", "industry_standard", "guidance"),
        evidence_slots=("技术目标", "约束条件", "规范要求", "方法选择条件", "工作步骤", "不确定性"),
        output_shape="requirements_and_advice",
        search_mode="default",
    ),
    "out_of_scope": RetrievalStrategy(
        strategy_id="reject_without_retrieval",
        document_types=(),
        evidence_slots=(),
        output_shape="scope_rejection",
    ),
}


LEGACY_TO_PRIMARY = {
    "standard_selection": "standard_catalog_lookup",
    "related_documents": "standard_catalog_lookup",
    "regulation_lookup": "clause_lookup",
    "definition_explanation": "definition_lookup",
    "engineering_distance_lookup": "numeric_table_lookup",
    "projection_numeric_rule": "numeric_table_lookup",
    "exploration_type_factors": "numeric_table_lookup",
    "basic_analysis_items": "numeric_table_lookup",
    "companion_resource_type": "clause_lookup",
    "service_materials": "service_materials",
    "service_procedure_basis": "service_workflow",
    "service_time_limit": "service_workflow",
    "authority_responsibility": "authority_jurisdiction",
    "exploration_to_mining_eligibility": "eligibility_condition",
    "legal_responsibility": "clause_lookup",
    "projection_comparison": "cross_document_comparison",
    "clause_comparison": "cross_document_comparison",
    "cross_document_audit": "cross_document_comparison",
}

PRIMARY_TO_LEGACY = {
    "standard_catalog_lookup": "standard_selection",
    "status_verification": "standard_selection",
    "clause_lookup": "regulation_lookup",
    "definition_lookup": "definition_explanation",
    "numeric_table_lookup": "general",
    "service_materials": "service_materials",
    "service_workflow": "service_procedure_basis",
    "authority_jurisdiction": "authority_responsibility",
    "eligibility_condition": "general",
    "cross_document_comparison": "projection_comparison",
    "document_inventory": "related_documents",
    "technical_method": "general",
    "out_of_scope": "general",
}


APPLICATION_LABELS = {
    "new": "新立",
    "renewal": "延续",
    "change": "变更",
    "cancellation": "注销",
}

CHANGE_SUBTYPE_LABELS = {
    "expand_area": "扩大矿区范围",
    "shrink_area": "缩小矿区范围",
    "mineral_or_mining_method": "变更开采主矿种或开采方式",
    "holder_name": "变更采矿权人名称",
    "transfer": "采矿权转让",
}


@dataclass(frozen=True)
class QueryClassification:
    version: str
    primary_intent: str
    secondary_intents: tuple[str, ...] = ()
    target_entity: str | None = None
    mineral: str | None = None
    business_action: str | None = None
    completed_stage: str | None = None
    target_outcome: str | None = None
    document_types: tuple[str, ...] = ()
    evidence_slots: tuple[str, ...] = ()
    output_shape: str = "default"
    ambiguities: tuple[str, ...] = ()
    missing_slots: tuple[str, ...] = ()
    confidence: float = 0.0
    application_type: str | None = None
    change_subtype: str | None = None
    authority_relation: str | None = None
    license_issuer_level: str = "unknown"
    comparison_topic: str | None = None
    comparison_scope: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def resolved_slots(self) -> dict[str, str]:
        values = {
            "application_type": self.application_type,
            "change_subtype": self.change_subtype,
            "authority_relation": self.authority_relation,
            "license_issuer_level": (
                self.license_issuer_level
                if self.license_issuer_level in {"ministry", "province"}
                else None
            ),
            "completed_stage": self.completed_stage,
            "target_outcome": self.target_outcome,
            "comparison_topic": self.comparison_topic,
            "comparison_scope": self.comparison_scope,
        }
        return {key: value for key, value in values.items() if value}


def strategy_for(primary_intent: str) -> RetrievalStrategy:
    return STRATEGY_REGISTRY.get(primary_intent, STRATEGY_REGISTRY["technical_method"])


def legacy_primary_intent(intent: str) -> str:
    return LEGACY_TO_PRIMARY.get(intent, "technical_method")


def legacy_intent_for_primary(primary_intent: str, fallback: str = "general") -> str:
    value = PRIMARY_TO_LEGACY.get(primary_intent, fallback)
    if primary_intent == "numeric_table_lookup" and fallback != "general":
        return fallback
    if primary_intent == "eligibility_condition" and fallback != "general":
        return fallback
    if primary_intent == "cross_document_comparison" and fallback not in {"general", "projection_rule"}:
        return fallback
    return value


def _clean_optional(value: object, limit: int = 160) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()[:limit]
    return text or None


def _clean_values(value: object, *, limit: int = 12) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        text = _clean_optional(item, 120)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return tuple(result)


def extract_application_slots(question: str) -> tuple[str | None, str | None]:
    compact = re.sub(r"\s+", "", question or "")
    subtype: str | None = None
    if re.search(r"(?:扩大|扩展|扩界|增加).{0,6}(?:矿区|开采)?范围|扩大范围", compact):
        subtype = "expand_area"
    elif re.search(r"(?:缩小|缩减|减小).{0,6}(?:矿区|开采)?范围|缩小范围", compact):
        subtype = "shrink_area"
    elif any(term in compact for term in ("开采主矿种", "开采矿种", "开采方式", "矿种或开采方式")):
        subtype = "mineral_or_mining_method"
    elif any(term in compact for term in ("采矿权人名称", "矿业权人名称", "权人名称", "名称变更")):
        subtype = "holder_name"
    elif any(term in compact for term in ("采矿权转让", "矿业权转让", "转让申请", "采矿权转移")):
        subtype = "transfer"

    if subtype:
        return "change", subtype
    if any(term in compact for term in ("新立", "首次登记", "首次申请", "探矿权转采矿权", "探转采")):
        return "new", None
    if any(term in compact for term in ("延续", "续期")):
        return "renewal", None
    if "注销" in compact:
        return "cancellation", None
    if any(term in compact for term in ("变更", "转让", "转移")):
        return "change", None
    return None, None


def extract_authority_relation(question: str) -> str | None:
    compact = re.sub(r"\s+", "", question or "")
    if any(term in compact for term in ("资源储量评审备案", "储量评审备案", "储量报告评审", "储量备案")):
        return "reserve_filing"
    if any(term in compact for term in ("压覆审批", "压覆矿产资源审批")):
        return "overlap_approval"
    if any(term in compact for term in ("出让权限", "矿业权出让", "谁出让")):
        return "granting"
    if any(term in compact for term in ("登记机关", "登记权限", "采矿权登记")):
        return "registration"
    if any(term in compact for term in ("发证机关", "谁发证", "颁发采矿许可证")):
        return "license_issuance"
    return None


def _mineral_from_question(question: str) -> str | None:
    common = (
        "岩金", "砂金", "沙金", "金", "银", "铜", "铅", "锌", "钼", "钨", "锡", "铁", "锰", "铬",
        "铝土", "稀土", "煤", "煤层气", "石油", "天然气", "石灰岩", "白云岩", "菱镁", "方解石",
    )
    for mineral in common:
        if f"{mineral}矿" in question or mineral in {"煤", "煤层气", "石油", "天然气"} and mineral in question:
            return "砂金" if mineral == "沙金" else mineral
    return None


def _comparison_topic(question: str) -> str | None:
    for topic in ("有限外推", "无限外推", "矿体外推", "工程间距", "选冶试验", "转采", "勘查程度"):
        if topic in question:
            return topic
    return None


def _business_action(question: str, primary_intent: str, application_type: str | None, change_subtype: str | None) -> str | None:
    if primary_intent == "service_materials":
        if change_subtype:
            return CHANGE_SUBTYPE_LABELS[change_subtype]
        if application_type:
            return f"采矿权{APPLICATION_LABELS[application_type]}申请"
        return "采矿权申请"
    if primary_intent == "authority_jurisdiction":
        relation = extract_authority_relation(question)
        return {
            "reserve_filing": "矿产资源储量评审备案",
            "overlap_approval": "压覆矿产资源审批",
            "granting": "矿业权出让",
            "registration": "矿业权登记",
            "license_issuance": "许可证颁发",
        }.get(relation)
    if "转采" in question or "探矿权转采矿权" in question:
        return "探矿权转采矿权"
    return None


def build_classification(
    question: str,
    legacy_intent: str,
    *,
    document_types: tuple[str, ...] = (),
    license_issuer_level: str = "unknown",
    confidence: float = 0.72,
) -> QueryClassification:
    primary = legacy_primary_intent(legacy_intent)
    if legacy_intent == "standard_selection" and any(
        term in question for term in ("还有效", "是否有效", "现行", "废止", "替代", "最新版", "新版本")
    ):
        primary = "status_verification"
    if legacy_intent == "related_documents" and any(
        term in question
        for term in ("全套资料", "全套文件", "全套执行文件", "全套标准", "项目需要哪些标准", "各阶段分别用什么规范", "项目资料体系")
    ):
        primary = "document_inventory"
    workflow_transition = bool(
        any(term in question for term in ("之后", "以后", "完成后", "办完", "备案后", "下一步", "接下来"))
        and any(term in question for term in ("之前", "以前", "领取", "取得", "还需要", "还需", "还要", "手续", "下一步"))
    )
    if legacy_intent == "service_materials" and workflow_transition:
        primary = "service_workflow"
    strategy = strategy_for(primary)
    application_type, change_subtype = extract_application_slots(question)
    authority_relation = extract_authority_relation(question)
    missing: list[str] = []
    ambiguities: list[str] = []

    is_mining_right_materials = primary == "service_materials" and any(
        term in question for term in ("采矿权", "采矿证", "采矿许可证")
    )
    is_mining_right_materials = is_mining_right_materials or (
        primary == "service_materials"
        and any(
            term in question
            for term in ("矿区范围", "扩大范围", "缩小范围", "开采方式", "开采主矿种", "采矿权人名称", "采矿权转让")
        )
    )
    if is_mining_right_materials and application_type is None:
        missing.append("application_type")
        ambiguities.append("采矿权申请材料按新立、延续、变更和注销分别规定。")
    elif is_mining_right_materials and application_type == "change" and change_subtype is None:
        missing.append("change_subtype")
        ambiguities.append("采矿权变更包含多个材料清单不同的子类型。")

    if primary == "authority_jurisdiction" and authority_relation == "reserve_filing":
        if license_issuer_level not in {"ministry", "province"}:
            missing.append("license_issuer_level")
            ambiguities.append("矿产资源储量评审备案权限取决于许可证颁发机关。")

    secondary: tuple[str, ...] = ()
    if primary == "service_workflow" and any(term in question for term in ("材料", "资料", "要件")):
        secondary = ("service_materials",)
    elif primary == "service_workflow" and legacy_intent == "service_materials":
        secondary = ("service_materials",)
    elif primary == "service_materials" and any(term in question for term in ("流程", "程序", "下一步", "手续")):
        secondary = ("service_workflow",)

    target_entity = None
    if is_mining_right_materials:
        if change_subtype:
            target_entity = f"采矿权{CHANGE_SUBTYPE_LABELS[change_subtype]}登记"
        elif application_type:
            target_entity = f"采矿权{APPLICATION_LABELS[application_type]}登记"
        else:
            target_entity = "采矿权登记"
    elif primary == "authority_jurisdiction" and authority_relation == "reserve_filing":
        target_entity = "矿产资源储量评审备案"

    completed_stage = None
    target_outcome = None
    if primary == "service_workflow":
        stage_match = re.search(r"(.{2,40}?)(?:完成后|办完后|备案后|以后|之后)", question)
        if stage_match:
            completed_stage = stage_match.group(1).strip("，,。；;?？ ")[-40:]
        if any(term in question for term in ("领取采矿证", "领取采矿许可证", "取得采矿证", "取得采矿许可证")):
            target_outcome = "取得采矿许可证"

    return QueryClassification(
        version=CLASSIFICATION_VERSION,
        primary_intent=primary,
        secondary_intents=secondary,
        target_entity=target_entity,
        mineral=_mineral_from_question(question),
        business_action=_business_action(question, primary, application_type, change_subtype),
        completed_stage=completed_stage,
        target_outcome=target_outcome,
        document_types=document_types or strategy.document_types,
        evidence_slots=strategy.evidence_slots,
        output_shape=strategy.output_shape,
        ambiguities=tuple(ambiguities),
        missing_slots=tuple(missing),
        confidence=confidence,
        application_type=application_type,
        change_subtype=change_subtype,
        authority_relation=authority_relation,
        license_issuer_level=license_issuer_level,
        comparison_topic=_comparison_topic(question) if primary == "cross_document_comparison" else None,
        comparison_scope="representative" if primary == "cross_document_comparison" else None,
    )


def classification_from_payload(
    payload: dict[str, Any] | None,
    fallback: QueryClassification,
) -> QueryClassification:
    if not isinstance(payload, dict):
        return fallback
    primary = str(payload.get("primary_intent") or fallback.primary_intent).strip()
    if primary not in PRIMARY_INTENTS:
        primary = fallback.primary_intent
    strategy = strategy_for(primary)
    try:
        confidence = max(0.0, min(1.0, float(payload.get("confidence", fallback.confidence))))
    except (TypeError, ValueError):
        confidence = fallback.confidence
    application_type = _clean_optional(payload.get("application_type"), 40) or fallback.application_type
    if application_type not in {*APPLICATION_LABELS, None}:
        application_type = fallback.application_type
    change_subtype = _clean_optional(payload.get("change_subtype"), 60) or fallback.change_subtype
    if change_subtype not in {*CHANGE_SUBTYPE_LABELS, None}:
        change_subtype = fallback.change_subtype
    issuer = str(payload.get("license_issuer_level") or fallback.license_issuer_level).strip().lower()
    if issuer not in {"unknown", "ministry", "province"}:
        issuer = fallback.license_issuer_level
    known_missing_slots = {
        "application_type",
        "change_subtype",
        "authority_relation",
        "license_issuer_level",
        "completed_stage",
        "target_outcome",
        "comparison_topic",
        "comparison_scope",
        "technical_goal",
    }
    model_missing_slots = tuple(
        value
        for value in _clean_values(payload.get("missing_slots"))
        if value in known_missing_slots
    )
    result = QueryClassification(
        version=str(payload.get("version") or CLASSIFICATION_VERSION)[:20],
        primary_intent=primary,
        secondary_intents=tuple(
            value for value in _clean_values(payload.get("secondary_intents"), limit=4) if value in PRIMARY_INTENTS and value != primary
        ) or fallback.secondary_intents,
        target_entity=_clean_optional(payload.get("target_entity")) or fallback.target_entity,
        mineral=_clean_optional(payload.get("mineral"), 40) or fallback.mineral,
        business_action=_clean_optional(payload.get("business_action")) or fallback.business_action,
        completed_stage=_clean_optional(payload.get("completed_stage")) or fallback.completed_stage,
        target_outcome=_clean_optional(payload.get("target_outcome")) or fallback.target_outcome,
        document_types=_clean_values(payload.get("document_types")) or fallback.document_types or strategy.document_types,
        evidence_slots=_clean_values(payload.get("evidence_slots")) or fallback.evidence_slots or strategy.evidence_slots,
        output_shape=_clean_optional(payload.get("output_shape"), 80) or fallback.output_shape or strategy.output_shape,
        ambiguities=tuple(
            dict.fromkeys((*fallback.ambiguities, *_clean_values(payload.get("ambiguities"))))
        ),
        missing_slots=tuple(dict.fromkeys((*fallback.missing_slots, *model_missing_slots))),
        confidence=confidence,
        application_type=application_type,
        change_subtype=change_subtype,
        authority_relation=_clean_optional(payload.get("authority_relation"), 60) or fallback.authority_relation,
        license_issuer_level=issuer,
        comparison_topic=_clean_optional(payload.get("comparison_topic")) or fallback.comparison_topic,
        comparison_scope=_clean_optional(payload.get("comparison_scope"), 80) or fallback.comparison_scope,
    )
    return validate_required_slots(result)


def validate_required_slots(classification: QueryClassification) -> QueryClassification:
    missing = [value for value in classification.missing_slots if value]
    ambiguities = [value for value in classification.ambiguities if value]

    def require(slot: str, reason: str) -> None:
        if slot not in missing:
            missing.append(slot)
        if reason not in ambiguities:
            ambiguities.append(reason)

    if classification.primary_intent == "service_materials" and classification.target_entity and "采矿权" in classification.target_entity:
        if not classification.application_type:
            require("application_type", "采矿权申请材料按新立、延续、变更和注销分别规定。")
        elif classification.application_type == "change" and not classification.change_subtype:
            require("change_subtype", "采矿权变更包含多个材料清单不同的子类型。")
    if classification.primary_intent == "authority_jurisdiction" and classification.authority_relation == "reserve_filing":
        if classification.license_issuer_level not in {"ministry", "province"}:
            require("license_issuer_level", "矿产资源储量评审备案权限取决于许可证颁发机关。")

    resolved = classification.resolved_slots
    missing = [slot for slot in missing if slot not in resolved]
    return replace(
        classification,
        missing_slots=tuple(dict.fromkeys(missing)),
        ambiguities=tuple(dict.fromkeys(ambiguities)),
    )


def merge_slot_updates(
    classification: QueryClassification,
    slot_updates: dict[str, Any] | None,
) -> QueryClassification:
    if not slot_updates:
        return validate_required_slots(classification)
    allowed = {
        "application_type",
        "change_subtype",
        "authority_relation",
        "license_issuer_level",
        "completed_stage",
        "target_outcome",
        "comparison_topic",
        "comparison_scope",
    }
    updates = {
        key: str(value).strip()
        for key, value in slot_updates.items()
        if key in allowed and value is not None and str(value).strip()
    }
    if updates.get("application_type") not in {*APPLICATION_LABELS, None}:
        updates.pop("application_type", None)
    if updates.get("change_subtype") not in {*CHANGE_SUBTYPE_LABELS, None}:
        updates.pop("change_subtype", None)
    if updates.get("license_issuer_level") not in {"ministry", "province", "unknown", None}:
        updates.pop("license_issuer_level", None)
    merged = replace(classification, **updates)
    if merged.change_subtype and merged.application_type != "change":
        merged = replace(merged, application_type="change")
    target_entity = merged.target_entity
    business_action = merged.business_action
    if merged.primary_intent == "service_materials":
        if merged.change_subtype:
            target_entity = f"采矿权{CHANGE_SUBTYPE_LABELS[merged.change_subtype]}登记"
            business_action = CHANGE_SUBTYPE_LABELS[merged.change_subtype]
        elif merged.application_type:
            target_entity = f"采矿权{APPLICATION_LABELS[merged.application_type]}登记"
            business_action = f"采矿权{APPLICATION_LABELS[merged.application_type]}申请"
    return validate_required_slots(
        replace(merged, target_entity=target_entity, business_action=business_action)
    )
