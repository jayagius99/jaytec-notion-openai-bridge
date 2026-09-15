from __future__ import annotations

import json
import re
from typing import Any, Dict

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def strip_code_fence(text: str) -> str:
    stripped = (text or "").strip()
    match = _CODE_FENCE_RE.match(stripped)
    if match:
        return (match.group(1) or "").strip()
    return stripped


def json_object(text: str) -> Dict[str, Any]:
    cleaned = strip_code_fence(text)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"worker returned invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("worker JSON root must be an object")
    return value
