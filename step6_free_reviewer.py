"""Isolated zero-paid-token reviewer for frozen Uren/JIB Step-6 candidates.

Branch-only evidence utility. No tools, no writes, no fallback models, no side effects.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

from openai import OpenAI
from worker_json import json_object

ALLOWED_MODELS = {
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "deepseek/deepseek-v4-flash-0731:free",
}
MAX_PACKET_CHARS = 12000
FORBIDDEN_KEY_MARKERS = (
    "api_key",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
    "personal_data",
    "source_code",
)

def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

def packet_sha(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()

def validate(value):
    if not isinstance(value, dict):
        raise RuntimeError("STEP6_PACKET_NOT_OBJECT")
    raw = canonical(value)
    if len(raw) > MAX_PACKET_CHARS:
        raise RuntimeError("STEP6_PACKET_TOO_LARGE")
    def walk(node):
        if isinstance(node, dict):
            for key, child in node.items():
                normalized = str(key).strip().casefold()
                if any(marker in normalized for marker in FORBIDDEN_KEY_MARKERS):
                    raise RuntimeError("STEP6_PACKET_FORBIDDEN_KEY")
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
    walk(value)
    return value

def main():
    model = os.environ.get("STEP6_REVIEW_MODEL", "").strip()
    path = os.environ.get("STEP6_REVIEW_PACKET", "").strip()
    if model not in ALLOWED_MODELS or not model.endswith(":free"):
        raise RuntimeError("STEP6_REVIEW_MODEL_NOT_ZERO_COST_ALLOWLISTED")
    if not path:
        raise RuntimeError("STEP6_REVIEW_PACKET_REQUIRED")
    packet = validate(json.loads(Path(path).read_text(encoding="utf-8")))
    expected = os.environ.get("STEP6_REVIEW_PACKET_SHA256", "").strip()
    actual = packet_sha(packet)
    if not expected or actual != expected:
        raise RuntimeError("STEP6_REVIEW_PACKET_HASH_MISMATCH")
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY_REQUIRED")

    prompt = (
        "You are an independent adversarial software-safety reviewer. "
        "Review only the sanitized frozen candidate evidence below. Do not assume "
        "unexecuted live tests passed. Focus on bypasses, hidden data/control paths, "
        "race/restart/restore failure modes, authority confusion, provenance leakage, "
        "integrity gaps, and whether reference tests could produce false confidence. "
        "Return ONLY one compact JSON object with keys: verdict "
        "(PASS|PASS_WITH_FINDINGS|FAIL), critical_findings (array of strings), "
        "evidence_challenges (array of strings), missing_evidence (array of strings), "
        "confidence (LOW|MEDIUM|HIGH), candidate_safe_for_isolated_proving "
        "(boolean). This is advisory only and cannot authorize deployment, activation "
        "or production attachment. Never reveal chain-of-thought. EVIDENCE_PACKET_JSON:\n"
        + canonical(packet)
    )
    client = OpenAI(
        api_key=key,
        base_url="https://openrouter.ai/api/v1",
        timeout=90.0,
        max_retries=0,
    )
    returned_model = ""
    result = None
    last = None
    for format_retry in (False, True):
        review_prompt = prompt
        if format_retry:
            review_prompt += "\nReturn exactly one valid JSON object, no markdown or surrounding text."
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": review_prompt}],
                temperature=0,
                max_tokens=4096,
                stream=False,
                extra_body={"provider": {"allow_fallbacks": False}},
            )
            if not response.choices:
                raise RuntimeError("STEP6_REVIEW_NO_CHOICES")
            returned_model = str(getattr(response, "model", "") or "")
            if not returned_model:
                raise RuntimeError("STEP6_REVIEW_MODEL_IDENTITY_UNOBSERVABLE")
            if returned_model != model:
                raise RuntimeError("STEP6_REVIEW_MODEL_IDENTITY_MISMATCH:" + returned_model)
            choice = response.choices[0]
            finish = str(getattr(choice, "finish_reason", "") or "")
            if finish in {"length", "content_filter"}:
                raise RuntimeError("STEP6_REVIEW_TRUNCATED_OR_BLOCKED:" + finish)
            result = json_object(choice.message.content or "")
            break
        except Exception as exc:
            last = exc

    if result is None:
        print(json.dumps({
            "event": "JAYTEC_STEP6_FREE_REVIEW",
            "status": "FAILED_CLOSED",
            "candidate": packet.get("candidate"),
            "requested_model": model,
            "returned_model": returned_model or None,
            "packet_sha256": actual,
            "error_class": type(last).__name__ if last else "UNKNOWN",
        }, sort_keys=True))
        return 0

    if result.get("verdict") not in {"PASS", "PASS_WITH_FINDINGS", "FAIL"}:
        raise RuntimeError("STEP6_REVIEW_VERDICT_INVALID")
    if not isinstance(result.get("candidate_safe_for_isolated_proving"), bool):
        raise RuntimeError("STEP6_REVIEW_SAFE_FIELD_INVALID")
    for key_name in ("critical_findings", "evidence_challenges", "missing_evidence"):
        if not isinstance(result.get(key_name), list) or not all(
            isinstance(item, str) for item in result[key_name]
        ):
            raise RuntimeError("STEP6_REVIEW_FIELD_INVALID:" + key_name)
    if result.get("confidence") not in {"LOW", "MEDIUM", "HIGH"}:
        raise RuntimeError("STEP6_REVIEW_CONFIDENCE_INVALID")

    print(json.dumps({
        "event": "JAYTEC_STEP6_FREE_REVIEW",
        "status": "SUCCESS",
        "candidate": packet.get("candidate"),
        "requested_model": model,
        "returned_model": returned_model,
        "packet_sha256": actual,
        "review": result,
    }, ensure_ascii=False, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
