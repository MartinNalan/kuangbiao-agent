from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


PROMPT_REGISTRY_VERSION = "v3.0.1"


@dataclass(frozen=True)
class PromptSpec:
    stage: str
    version: str
    baseline: str
    calibrated: str = ""
    intent_rules: dict[str, str] | None = None


_INTENT_RULES = {
    "service_materials": (
        "材料问题必须服从已确认的办理类型和变更子类型；未确认的槽位只能请求确认，"
        "不能生成汇总材料清单或假设用户办理情形。"
    ),
    "service_workflow": (
        "流程问题必须保留已完成阶段、目标结果和待办动作；材料名称需要转换为可执行手续，"
        "但不得补充证据没有支持的前置审批。"
    ),
    "authority_jurisdiction": (
        "权限问题必须先识别具体权限关系。许可证颁发、矿业权出让、登记和评审备案不得混用。"
    ),
    "eligibility_condition": (
        "条件判断以目标动作作为主关系，按一般条件、特殊规定和限制分别核验，不可由单一关键词推断结论。"
    ),
    "cross_document_comparison": (
        "比较只能使用同一关系、同一维度下的直接条款；无关候选仅影响覆盖率，不能被表述为文件未规定。"
    ),
    "numeric_table_lookup": (
        "数值和表格必须同时保留指标行、列、方向、单位和表注；不能压缩多个方向或条件为一个数值。"
    ),
    "definition_lookup": (
        "定义类优先输出正式定义原文；复合术语没有独立定义时，应明确区分行业统称与组成概念定义。"
    ),
    "technical_method": (
        "技术要求的满足、替代或覆盖问题必须同时核对阶段最低要求与研究层级关系。"
        "不得假设用户未完成某项工作；原文仅称‘必要时’时，不能机械推导固定先后顺序。"
    ),
}


PROMPT_REGISTRY: dict[str, PromptSpec] = {
    "question_resolution": PromptSpec(
        stage="question_resolution",
        version=PROMPT_REGISTRY_VERSION,
        baseline=(
            "先输出结构化 QueryClassification，再决定是否需要确认。模型只能理解和抽取槽位，"
            "不能在这一阶段生成专业结论。"
        ),
        calibrated=(
            "校准要求：把用户的口语、错别字和省略还原为业务目标，但不得把未给出的决定性事实补成已知事实。"
        ),
        intent_rules=_INTENT_RULES,
    ),
    "retrieval_planner": PromptSpec(
        stage="retrieval_planner",
        version=PROMPT_REGISTRY_VERSION,
        baseline=(
            "检索计划必须遵循上游 QueryClassification；只可补充查询表达和证据槽位，不能重定义用户业务意图。"
        ),
        calibrated=(
            "校准要求：子查询必须填补不同证据槽位，不能仅替换同义词或扩展无关标准。"
        ),
        intent_rules=_INTENT_RULES,
    ),
    "answer": PromptSpec(
        stage="answer",
        version=PROMPT_REGISTRY_VERSION,
        baseline=(
            "答案必须与 QueryClassification 的输出形态一致；证据不足时说明缺口，不能用模型常识补写条款。"
        ),
        calibrated=(
            "校准要求：先回答用户真正要完成的动作，再给最短必要原文和来源；不要重复表格或证据列表。"
        ),
        intent_rules=_INTENT_RULES,
    ),
    "research_summary": PromptSpec(
        stage="research_summary",
        version=PROMPT_REGISTRY_VERSION,
        baseline=(
            "深度研究结论必须区分已审查范围、直接证据和真实缺口。候选未命中不等于该文件没有规定。"
        ),
        calibrated=(
            "校准要求：先给明确差异结论，再用统一维度表呈现证据；避免逐条复述原文。"
        ),
        intent_rules=_INTENT_RULES,
    ),
}


def prompt_text(
    settings: "Settings",
    stage: str,
    *,
    primary_intent: str | None = None,
) -> str:
    if not settings.prompt_registry_enabled:
        return ""
    spec = PROMPT_REGISTRY.get(stage)
    if spec is None:
        return ""
    parts = [f"Prompt Registry {spec.version} / {stage}", spec.baseline]
    enabled_intents = {
        value.strip()
        for value in settings.prompt_calibration_intents.split(",")
        if value.strip()
    }
    calibration_enabled = (
        settings.prompt_calibration_enabled
        and settings.prompt_calibration_variant == "calibrated"
        and (not enabled_intents or (primary_intent or "") in enabled_intents)
    )
    if calibration_enabled and spec.calibrated:
        parts.append(spec.calibrated)
    intent_rule = (spec.intent_rules or {}).get(primary_intent or "")
    if intent_rule:
        parts.append(intent_rule)
    return "\n".join(parts)


def registry_manifest() -> dict[str, object]:
    return {
        "version": PROMPT_REGISTRY_VERSION,
        "stages": {
            stage: {"version": spec.version, "has_calibrated_variant": bool(spec.calibrated)}
            for stage, spec in PROMPT_REGISTRY.items()
        },
    }
