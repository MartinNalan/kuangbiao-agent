import re
from dataclasses import dataclass

from .domain_lexicon import matched_lexicon_entries


STANDARD_NO_RE = re.compile(r"\b(?:GB|GB/T|DZ/T|DZ|NB/T|HJ|YS/T)\s*\d{3,6}(?:\.\d+)?[-－]\d{4}\b", re.I)
DOMAIN_KEYWORDS = {
    "矿",
    "矿产",
    "地质",
    "勘查",
    "资源量",
    "储量",
    "矿山",
    "采矿",
    "选矿",
    "尾矿",
    "矿业权",
    "自然资源",
    "标准",
    "规范",
    "规程",
    "条款",
    "工程间距",
    "勘查类型",
    "控制程度",
    "基本工程",
    "工业指标",
    "报告",
    "评审",
}

ABUSE_KEYWORDS = {
    "忽略以上",
    "忽略前面的",
    "系统提示词",
    "system prompt",
    "jailbreak",
    "越狱",
    "泄露提示词",
}


@dataclass(frozen=True)
class DomainDecision:
    in_scope: bool
    reason: str
    matched_terms: list[str]


class DomainGate:
    def check(self, question: str) -> DomainDecision:
        text = question.strip()
        lowered = text.lower()
        if not text:
            return DomainDecision(False, "empty_question", [])

        abuse_terms = [term for term in ABUSE_KEYWORDS if term.lower() in lowered]
        if abuse_terms:
            return DomainDecision(False, "abuse_or_prompt_injection", abuse_terms)

        matched_terms = [term for term in DOMAIN_KEYWORDS if term in text]
        matched_terms.extend(
            entry["user_expression"]
            for entry in matched_lexicon_entries(text, purpose="domain_gate")
        )
        if STANDARD_NO_RE.search(text):
            matched_terms.append("standard_no")

        if matched_terms:
            return DomainDecision(True, "domain_terms_matched", sorted(set(matched_terms)))

        return DomainDecision(False, "no_domain_terms", [])
