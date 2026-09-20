"""JAYTEC Proving Grounds V1.

A deterministic, non-thinking JAYTEC resource with specialist-like invocation
semantics. Forge may request it through JAYTEC, but it has no authority to plan,
route, broaden scope, choose providers, mutate production, or execute arbitrary
commands.

Only named suites in PROVING_GROUNDS_SUITES can run. Every invocation is bound
to the exact runtime commit and returns structured evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Mapping

RESOURCE_ID = "PROVING_GROUNDS"
RESOURCE_VERSION = "JAYTEC_PROVING_GROUNDS_V1"
MAX_REQUEST_BYTES = 16_384
MAX_OUTPUT_CHARS = 8_000
DEFAULT_TIMEOUT_SECONDS = 90
ALLOWED_OPERATIONS = ("test", "validate", "adversarial_test", "benchmark")

PROVING_GROUNDS_SUITES: dict[str, tuple[str, ...]] = {
    "forge-cognition-core": (
        "test_forge_cognition",
        "test_forge_cognition_engine",
        "test_forge_strategy_engine",
        "test_forge_strategic_adversarial",
    ),
    "authority-boundaries": (
        "test_relationship_policy",
        "test_dispatch_authority_boundary",
        "test_worker_policy_hardening",
    ),
    "watch-recovery": (
        "test_autorecovery_components",
        "test_autorecovery_supervisor",
        "test_autorecovery_watch_ingress",
    ),
    "manus-governance": (
        "test_manus_policy",
        "test_manus_governance",
        "test_manus_dispatch_contract",
        "test_manus_adversarial_matrix",
    ),
}


class ProvingGroundsError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProvingGroundsRequest:
    task_id: str
    run_id: str
    suite_id: str
    expected_runtime_commit: str
    operation: str = "test"

    @classmethod
    def parse(cls, raw: str) -> "ProvingGroundsRequest":
        if not isinstance(raw, str) or not raw.strip():
            raise ProvingGroundsError("REQUEST_REQUIRED")
        if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ProvingGroundsError("REQUEST_TOO_LARGE")
        try:
            value = json.loads(raw)
        except Exception as exc:
            raise ProvingGroundsError("REQUEST_JSON_INVALID") from exc
        if not isinstance(value, Mapping):
            raise ProvingGroundsError("REQUEST_OBJECT_REQUIRED")
        allowed = {
            "task_id",
            "run_id",
            "suite_id",
            "expected_runtime_commit",
            "operation",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ProvingGroundsError("UNKNOWN_FIELDS:" + ",".join(unknown))

        def text_field(name: str, maximum: int = 200) -> str:
            out = str(value.get(name) or "").strip()
            if not out:
                raise ProvingGroundsError(name.upper() + "_REQUIRED")
            if len(out) > maximum:
                raise ProvingGroundsError(name.upper() + "_TOO_LONG")
            return out

        suite_id = text_field("suite_id")
        if suite_id not in PROVING_GROUNDS_SUITES:
            raise ProvingGroundsError("SUITE_NOT_REGISTERED")
        operation = str(value.get("operation") or "test").strip()
        if operation not in ALLOWED_OPERATIONS:
            raise ProvingGroundsError("OPERATION_NOT_ALLOWED")
        expected = text_field("expected_runtime_commit", maximum=64).lower()
        if len(expected) != 40 or any(c not in "0123456789abcdef" for c in expected):
            raise ProvingGroundsError("EXPECTED_RUNTIME_COMMIT_INVALID")
        return cls(
            task_id=text_field("task_id"),
            run_id=text_field("run_id"),
            suite_id=suite_id,
            expected_runtime_commit=expected,
            operation=operation,
        )


def runtime_commit(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    for key in ("RENDER_GIT_COMMIT", "JAYTEC_RUNTIME_COMMIT", "SOURCE_COMMIT"):
        value = str(source.get(key) or "").strip().lower()
        if len(value) == 40 and all(c in "0123456789abcdef" for c in value):
            return value
    return ""


def roster_entry() -> dict[str, Any]:
    return {
        "kind": "JAYTEC_RESOURCE",
        "resource_id": RESOURCE_ID,
        "version": RESOURCE_VERSION,
        "role": "EVIDENCE_AND_ADVERSARIAL_VALIDATION",
        "authority": "NONE",
        "invoke_via": "JAYTEC",
        "can_plan": False,
        "can_route": False,
        "can_authorize": False,
        "can_modify_production": False,
        "arbitrary_command_execution": False,
        "allowed_operations": list(ALLOWED_OPERATIONS),
    }


def validate_roster_entry(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ProvingGroundsError("PROVING_GROUNDS_ROSTER_ENTRY_REQUIRED")
    expected = roster_entry()
    for key in (
        "kind",
        "resource_id",
        "version",
        "authority",
        "invoke_via",
        "can_plan",
        "can_route",
        "can_authorize",
        "can_modify_production",
        "arbitrary_command_execution",
    ):
        if value.get(key) != expected[key]:
            raise ProvingGroundsError("PROVING_GROUNDS_ROSTER_MISMATCH:" + key)


def catalog(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    commit = runtime_commit(env)
    return {
        "schema_version": RESOURCE_VERSION,
        "resource": roster_entry(),
        "runtime_commit": commit or None,
        "runtime_commit_verified": bool(commit),
        "suites": {
            suite_id: {"modules": list(modules)}
            for suite_id, modules in sorted(PROVING_GROUNDS_SUITES.items())
        },
        "forge_activated": False,
    }


def _safe_tail(value: str) -> str:
    text = str(value or "")
    return text[-MAX_OUTPUT_CHARS:]


def run_registered_suite(
    request_json: str,
    *,
    env: Mapping[str, str] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        request = ProvingGroundsRequest.parse(request_json)
        commit = runtime_commit(env)
        if not commit:
            raise ProvingGroundsError("RUNTIME_COMMIT_UNVERIFIED")
        if commit != request.expected_runtime_commit:
            raise ProvingGroundsError("RUNTIME_COMMIT_MISMATCH")

        modules = PROVING_GROUNDS_SUITES[request.suite_id]
        command = [sys.executable, "-m", "unittest", "-q", *modules]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        stdout = _safe_tail(completed.stdout)
        stderr = _safe_tail(completed.stderr)
        evidence_raw = (stdout + "\n" + stderr).encode("utf-8", errors="replace")
        return {
            "schema_version": RESOURCE_VERSION,
            "status": "PASS" if completed.returncode == 0 else "FAIL",
            "resource_id": RESOURCE_ID,
            "task_id": request.task_id,
            "run_id": request.run_id,
            "suite_id": request.suite_id,
            "operation": request.operation,
            "runtime_commit": commit,
            "modules": list(modules),
            "exit_code": completed.returncode,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "evidence_sha256": hashlib.sha256(evidence_raw).hexdigest(),
            "stdout_tail": stdout,
            "stderr_tail": stderr,
            "authority": "NONE",
            "side_effects_authorized": False,
            "arbitrary_command_execution": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "schema_version": RESOURCE_VERSION,
            "status": "TIMEOUT",
            "resource_id": RESOURCE_ID,
            "reason": "REGISTERED_SUITE_TIMEOUT",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "authority": "NONE",
            "side_effects_authorized": False,
        }
    except Exception as exc:
        return {
            "schema_version": RESOURCE_VERSION,
            "status": "FAILED_CLOSED",
            "resource_id": RESOURCE_ID,
            "reason": type(exc).__name__ + ":" + str(exc),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "authority": "NONE",
            "side_effects_authorized": False,
        }


__all__ = [
    "ALLOWED_OPERATIONS",
    "PROVING_GROUNDS_SUITES",
    "RESOURCE_ID",
    "RESOURCE_VERSION",
    "ProvingGroundsError",
    "ProvingGroundsRequest",
    "catalog",
    "roster_entry",
    "run_registered_suite",
    "runtime_commit",
    "validate_roster_entry",
]
