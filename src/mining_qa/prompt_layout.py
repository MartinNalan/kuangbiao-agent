from __future__ import annotations

import json
from typing import Any


LEGACY_LAYOUT = "legacy"
SCHEMA_PREFIX_LAYOUT = "schema_prefix"
ALLOWED_PROMPT_LAYOUTS = {LEGACY_LAYOUT, SCHEMA_PREFIX_LAYOUT}


def unwrap_output_schema_envelope(value: Any) -> Any:
    """Recover a response that incorrectly wraps data in ``output_schema``.

    The schema-prefix contract requires the response fields at the JSON root.
    This narrow compatibility guard only unwraps the exact one-key envelope,
    so a legitimate payload can never lose sibling fields silently.
    """

    if (
        isinstance(value, dict)
        and set(value) == {"output_schema"}
        and isinstance(value.get("output_schema"), dict)
    ):
        return value["output_schema"]
    return value


def structured_prompt_messages(
    *,
    layout: str,
    base_system: str,
    dynamic_system_suffix: str,
    dynamic_payload: dict[str, Any],
    output_schema: dict[str, Any],
) -> list[dict[str, str]]:
    """Build a JSON request while keeping the legacy arm byte-compatible.

    In the optimized arm, the invariant schema precedes intent-specific rules
    and all per-question data. DeepSeek can therefore reuse it as part of the
    cached prefix across questions and intents.
    """

    selected = layout if layout in ALLOWED_PROMPT_LAYOUTS else LEGACY_LAYOUT
    if selected == LEGACY_LAYOUT:
        system_content = f"{base_system}{dynamic_system_suffix}"
        user_payload = {**dynamic_payload, "output_schema": output_schema}
    else:
        schema_contract = json.dumps(
            output_schema,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        system_content = (
            f"{base_system}固定输出结构如下：\n{schema_contract}\n"
            "返回对象的顶层必须直接包含上述字段；不得增加 output_schema 外层包装。\n"
            f"{dynamic_system_suffix}"
        )
        user_payload = dynamic_payload
    return [
        {"role": "system", "content": system_content},
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False),
        },
    ]
