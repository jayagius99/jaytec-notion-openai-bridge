from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class WorkerJsonDiagnostics:
    input_length: int
    extracted_object: bool
    parser_error: Optional[str] = None


class WorkerJsonError(ValueError):
    """Fail-closed worker output parsing error with a stable reason code."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}:{message}")
        self.code = code
        self.detail = message


def strip_code_fence(text: str) -> str:
    stripped = (text or "").strip()
    match = _CODE_FENCE_RE.match(stripped)
    if match:
        return (match.group(1) or "").strip()
    return stripped


def _balanced_object_slice(text: str) -> Optional[str]:
    """Return one complete top-level JSON object from surrounding prose.

    This deliberately does not repair malformed or truncated JSON. It only
    extracts an already-complete balanced object while respecting quoted
    strings and escapes.
    """

    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
            if depth < 0:
                return None

    return None


def json_object_with_diagnostics(text: str) -> tuple[Dict[str, Any], WorkerJsonDiagnostics]:
    cleaned = strip_code_fence(text)
    if not cleaned:
        raise WorkerJsonError("EMPTY_RESPONSE", "worker returned empty output")

    try:
        value = json.loads(cleaned)
        extracted = False
    except json.JSONDecodeError as direct_exc:
        candidate = _balanced_object_slice(cleaned)
        if candidate is None:
            # No complete balanced object exists. This covers the observed
            # unterminated-string / unexpected-end cases without inventing data.
            raise WorkerJsonError(
                "INCOMPLETE_OR_INVALID_JSON",
                f"{direct_exc.msg} at char {direct_exc.pos}",
            ) from direct_exc
        try:
            value = json.loads(candidate)
            extracted = True
        except json.JSONDecodeError as candidate_exc:
            # A balanced object that is internally malformed must remain a
            # contract failure. Do not guess missing quotes/commas/fields.
            raise WorkerJsonError(
                "MALFORMED_JSON",
                f"{candidate_exc.msg} at char {candidate_exc.pos}",
            ) from candidate_exc

    if not isinstance(value, dict):
        raise WorkerJsonError("ROOT_NOT_OBJECT", "worker JSON root must be an object")

    return value, WorkerJsonDiagnostics(
        input_length=len(cleaned.encode("utf-8")),
        extracted_object=extracted,
        parser_error=None,
    )


def json_object(text: str) -> Dict[str, Any]:
    value, _ = json_object_with_diagnostics(text)
    return value
