"""On-demand, zero-paid-token independent G1 evidence challenger.

Disabled by default. When explicitly enabled, sends only a bounded sanitized
evidence packet to a fixed free OpenRouter model and prints a structured review.
No tools, provider fallback, code, credentials, personal data, or side effects.
"""
from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI

from worker_json import json_object

ENABLED = os.environ.get("JAYTEC_G1_INDEPENDENT_REVIEW_ENABLED", "0").strip() == "1"
MODEL = os.environ.get(
    "JAYTEC_G1_INDEPENDENT_REVIEW_MODEL",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
).strip()
EXPECTED_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
MAX_PACKET_CHARS = 24_000

FORBIDDEN_KEYS = {
    "api_key",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
    "personal_data",
    "source_code",
}


def _validate_packet(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("REVIEW_PACKET_MUST_BE_OBJECT")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(encoded) > MAX_PACKET_CHARS:
        raise ValueError("REVIEW_PACKET_TOO_LARGE")

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                normalized = str(key).strip().casefold()
                if normalized in FORBIDDEN_KEYS or any(
                    marker in normalized
                    for marker in ("password", "secret", "token", "authorization", "api_key")
                ):
                    raise ValueError("REVIEW_PACKET_FORBIDDEN_KEY")
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return value


def main() -> int:
    if not ENABLED:
        print(json.dumps({
            "event": "JAYTEC_G1_INDEPENDENT_REVIEW",
            "status": "SKIP",
            "reason": "DISABLED",
            "model": MODEL,
        }, sort_keys=True))
        return 0

    if MODEL != EXPECTED_MODEL:
        raise RuntimeError("INDEPENDENT_REVIEW_MODEL_MISMATCH")

    raw = os.environ.get("JAYTEC_G1_REVIEW_PACKET_JSON", "").strip()
    if not raw:
        raise RuntimeError("JAYTEC_G1_REVIEW_PACKET_JSON_REQUIRED")
    packet = _validate_packet(json.loads(raw))

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY_REQUIRED")

    prompt = (
        "You are an independent adversarial software-safety reviewer. "
        "You did not implement this system. Review ONLY the sanitized evidence packet below. "
        "Do not assume unproven facts. Challenge hidden dependencies, replay/duplicate safety, "
        "provider identity, malformed-output handling, concurrency/rate limiting, versioning, "
        "Windows coexistence, destructive isolation, and gate integrity. "
        "Return ONLY one JSON object with keys: verdict (PASS|PASS_WITH_FINDINGS|FAIL), "
        "critical_findings (array of strings), evidence_challenges (array of strings), "
        "missing_evidence (array of strings), confidence (LOW|MEDIUM|HIGH), "
        "safe_to_unlock_v2 (boolean). Never include chain-of-thought. "
        "EVIDENCE_PACKET_JSON:\n"
        + json.dumps(packet, sort_keys=True, ensure_ascii=False)
    )

    client = OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        timeout=90.0,
        max_retries=0,
    )
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=2500,
        stream=False,
        extra_body={"provider": {"allow_fallbacks": False}},
    )
    if not response.choices:
        raise RuntimeError("INDEPENDENT_REVIEW_NO_CHOICES")

    returned_model = str(getattr(response, "model", "") or "")
    if returned_model and returned_model != MODEL:
        raise RuntimeError(f"INDEPENDENT_REVIEW_MODEL_IDENTITY_MISMATCH:{returned_model}")

    result = json_object(response.choices[0].message.content or "")
    verdict = result.get("verdict")
    if verdict not in {"PASS", "PASS_WITH_FINDINGS", "FAIL"}:
        raise RuntimeError("INDEPENDENT_REVIEW_VERDICT_INVALID")
    if not isinstance(result.get("safe_to_unlock_v2"), bool):
        raise RuntimeError("INDEPENDENT_REVIEW_UNLOCK_FIELD_INVALID")
    for key in ("critical_findings", "evidence_challenges", "missing_evidence"):
        if not isinstance(result.get(key), list) or not all(isinstance(x, str) for x in result[key]):
            raise RuntimeError(f"INDEPENDENT_REVIEW_{key.upper()}_INVALID")

    print(json.dumps({
        "event": "JAYTEC_G1_INDEPENDENT_REVIEW",
        "status": "SUCCESS",
        "requested_model": MODEL,
        "returned_model": returned_model or MODEL,
        "review": result,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
