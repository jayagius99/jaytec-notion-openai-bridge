"""STAGING ONLY: bounded exact-Codex provider diagnostic.

Runs once per 24h idempotency window and never blocks staging boot.
The probe is deliberately tiny so a 429 is not confused with a large-prompt TPM event.
Only redacted provider diagnostics are printed/stored.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from openai import RateLimitError as OpenAIRateLimitError

from orchestration import redact
from staging_server import CODEX_MODEL, IDEMPOTENCY_STORE, OPENAI_CLIENT, REGISTRY

KEY = "codex-429-diagnostic-v2"
DIGEST = "codex-429-diagnostic-v2"


def _safe_429_details(exc: Exception) -> dict[str, Any]:
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

    details = {
        "status_code": getattr(exc, "status_code", None),
        "request_id": getattr(exc, "request_id", None) or header("x-request-id"),
        "retry_after": header("retry-after"),
        "x_ratelimit_limit_requests": header("x-ratelimit-limit-requests"),
        "x_ratelimit_remaining_requests": header("x-ratelimit-remaining-requests"),
        "x_ratelimit_reset_requests": header("x-ratelimit-reset-requests"),
        "x_ratelimit_limit_tokens": header("x-ratelimit-limit-tokens"),
        "x_ratelimit_remaining_tokens": header("x-ratelimit-remaining-tokens"),
        "x_ratelimit_reset_tokens": header("x-ratelimit-reset-tokens"),
        "openai_organization": header("openai-organization"),
        "openai_project": header("openai-project"),
        "body": body if isinstance(body, Mapping) else None,
        "message": str(exc)[:2000],
    }
    return redact({k: v for k, v in details.items() if v is not None})


def main() -> int:
    try:
        prior = REGISTRY.lookup(KEY, DIGEST)
    except Exception as exc:
        prior = None
        print(
            "JAYTEC_CODEX_429_DIAGNOSTIC "
            + json.dumps(
                {
                    "phase": "idempotency_lookup",
                    "status": "ERROR",
                    "error": type(exc).__name__,
                    "idempotency_store": IDEMPOTENCY_STORE,
                },
                sort_keys=True,
            ),
            flush=True,
        )

    if prior is not None:
        print(
            "JAYTEC_CODEX_429_DIAGNOSTIC "
            + json.dumps(
                redact(
                    {
                        "phase": "tiny_provider_probe",
                        "status": "SKIP_REPLAY",
                        "model": CODEX_MODEL,
                        "idempotency_store": IDEMPOTENCY_STORE,
                        "prior": prior,
                    }
                ),
                sort_keys=True,
            ),
            flush=True,
        )
        return 0

    summary: dict[str, Any] = {
        "phase": "tiny_provider_probe",
        "model": CODEX_MODEL,
        "idempotency_store": IDEMPOTENCY_STORE,
        "probe_shape": "responses.create tiny input; no tools; no reasoning override",
    }

    if OPENAI_CLIENT is None:
        summary.update({"status": "ERROR", "error": "OPENAI_CLIENT_NOT_CONFIGURED"})
    else:
        try:
            response = OPENAI_CLIENT.responses.create(
                model=CODEX_MODEL,
                input="Reply exactly OK.",
            )
            summary.update(
                {
                    "status": "SUCCESS",
                    "response_id": getattr(response, "id", None),
                    "returned_model": getattr(response, "model", None) or CODEX_MODEL,
                }
            )
        except OpenAIRateLimitError as exc:
            summary.update(
                {
                    "status": "RATE_LIMITED",
                    "provider_details": _safe_429_details(exc),
                }
            )
        except Exception as exc:
            summary.update(
                {
                    "status": "ERROR",
                    "error": type(exc).__name__,
                    "message": redact(str(exc)[:1000]),
                }
            )

    safe = redact(summary)
    try:
        REGISTRY.store(KEY, DIGEST, safe)
    except Exception as exc:
        safe = {**safe, "idempotency_store_error": type(exc).__name__}

    print(
        "JAYTEC_CODEX_429_DIAGNOSTIC " + json.dumps(redact(safe), sort_keys=True),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
