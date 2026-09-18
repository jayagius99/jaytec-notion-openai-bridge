"""STAGING ONLY: one-shot exact engineering-provider access probe.

Proves that the current approved engineering model can actually be reached by the
configured OpenAI project. A stable registry key prevents repeated provider spend.
No tools or side effects are authorized.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from orchestration import redact
from staging_server import ENGINEERING_MODEL, IDEMPOTENCY_STORE, OPENAI_CLIENT, REGISTRY

KEY = "engineering-provider-access-v1-gpt-5.6-sol"
DIGEST = KEY


def _safe_error(exc: Exception) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    body = getattr(exc, "body", None)
    if body is None and response is not None:
        try:
            body = response.json()
        except Exception:
            body = None

    def header(name: str) -> Any:
        try:
            return headers.get(name)
        except Exception:
            return None

    error = body.get("error", {}) if isinstance(body, Mapping) else {}
    if not isinstance(error, Mapping):
        error = {}
    return redact({
        "status_code": getattr(exc, "status_code", None),
        "error_type": error.get("type"),
        "error_code": error.get("code"),
        "error_param": error.get("param"),
        "request_id": getattr(exc, "request_id", None) or header("x-request-id"),
        "retry_after": header("retry-after"),
        "openai_organization": header("openai-organization"),
        "openai_project": header("openai-project"),
        "message": str(exc)[:1200],
    })


def main() -> int:
    try:
        prior = REGISTRY.lookup(KEY, DIGEST)
    except Exception as exc:
        prior = None
        print(json.dumps({
            "event": "JAYTEC_ENGINEERING_PROVIDER_PROBE",
            "status": "REGISTRY_ERROR",
            "error": type(exc).__name__,
            "idempotency_store": IDEMPOTENCY_STORE,
        }, sort_keys=True), flush=True)

    if prior is not None:
        print(json.dumps(redact({
            "event": "JAYTEC_ENGINEERING_PROVIDER_PROBE",
            "status": "SKIP_REPLAY",
            "engineering_model": ENGINEERING_MODEL,
            "idempotency_store": IDEMPOTENCY_STORE,
            "prior": prior,
        }), sort_keys=True), flush=True)
        return 0

    summary: dict[str, Any] = {
        "event": "JAYTEC_ENGINEERING_PROVIDER_PROBE",
        "engineering_model": ENGINEERING_MODEL,
        "idempotency_store": IDEMPOTENCY_STORE,
        "probe_shape": "responses.create tiny input; no tools; no reasoning override",
    }
    if OPENAI_CLIENT is None:
        summary.update({"status": "FAILED_CLOSED", "error": "OPENAI_CLIENT_NOT_CONFIGURED"})
    else:
        try:
            response = OPENAI_CLIENT.responses.create(
                model=ENGINEERING_MODEL,
                input="Reply exactly ENGINEERING_PROBE_OK.",
                max_output_tokens=16,
            )
            returned_model = getattr(response, "model", None) or ENGINEERING_MODEL
            output = (getattr(response, "output_text", "") or "").strip()
            if returned_model != ENGINEERING_MODEL:
                summary.update({
                    "status": "FAILED_CLOSED",
                    "error": "MODEL_IDENTITY_MISMATCH",
                    "returned_model": returned_model,
                })
            elif output != "ENGINEERING_PROBE_OK":
                summary.update({
                    "status": "NEEDS_VALIDATION",
                    "returned_model": returned_model,
                    "output_shape_ok": False,
                })
            else:
                summary.update({
                    "status": "SUCCESS",
                    "returned_model": returned_model,
                    "output_shape_ok": True,
                    "response_id": getattr(response, "id", None),
                })
        except Exception as exc:
            summary.update({
                "status": "FAILED_CLOSED",
                "error": type(exc).__name__,
                "provider_details": _safe_error(exc),
            })

    safe = redact(summary)
    try:
        REGISTRY.store(KEY, DIGEST, safe)
    except Exception as exc:
        safe = {**safe, "idempotency_store_error": type(exc).__name__}
    print(json.dumps(redact(safe), sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
