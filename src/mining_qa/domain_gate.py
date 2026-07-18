import re
from dataclasses import dataclass

from .domain_lexicon import matched_lexicon_entries


STANDARD_NO_RE = re.compile(r"\b(?:GB|GB/T|DZ/T|DZ|NB/T|HJ|YS/T)\s*\d{3,6}(?:\.\d+)?[-－]\d{4}\b", re.I)
STRONG_DOMAIN_KEYWORDS = {
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
    "工程间距",
    "勘查类型",
    "控制程度",
    "基本工程",
    "工业指标",
    "评审",
}

# These words occur in the product domain, but are not domain evidence by
# themselves. Treating them as sufficient admitted unrelated questions such
# as HTTP standards and financial reports into the private knowledge base.
WEAK_GENERIC_KEYWORDS = {
    "标准",
    "规范",
    "规程",
    "条款",
    "报告",
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

        matched_terms = [term for term in STRONG_DOMAIN_KEYWORDS if term in text]
        lexicon_matches = matched_lexicon_entries(text, purpose="domain_gate")
        matched_terms.extend(entry["user_expression"] for entry in lexicon_matches)

        standard_no = STANDARD_NO_RE.search(text)
        # DZ/T is a natural-resources industry-standard namespace. A generic
        # GB/T number is deliberately not enough to admit a question because
        # the same namespace covers every industry.
        if standard_no and standard_no.group(0).upper().replace(" ", "").startswith("DZ"):
            matched_terms.append("natural_resources_standard_no")

        if matched_terms:
            return DomainDecision(True, "domain_terms_matched", sorted(set(matched_terms)))

        weak_terms = [term for term in WEAK_GENERIC_KEYWORDS if term in text]
        if weak_terms:
            return DomainDecision(False, "generic_terms_without_domain_context", sorted(weak_terms))

        return DomainDecision(False, "no_domain_terms", [])
