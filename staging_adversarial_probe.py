"""STAGING ONLY: run the bounded adversarial/failure-injection suite in Render.

This runs repository tests inside the deployed staging runtime. Provider happy-path
calls are handled separately by staging_runtime_probe.py. This script emits only a
compact matrix summary and failure IDs; it never emits secrets or provider output.

MATRIX MUST enumerate every authoritative adversarial requirement explicitly.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from typing import Iterable

MARKER = "JAYTEC_STAGING_ADVERSARIAL_PROBE"

MATRIX = {
    # Packet structure / required identifiers
    "malformed_json": {"test_malformed_json"},
    "missing_task_id": {"test_missing_task_id"},
    "missing_subtask_id": {"test_missing_subtask_id"},
    "unknown_packet_fields": {"test_unknown_fields_fail_closed"},

    # Deadline validation
    "missing_deadline": {"test_missing_deadline"},
    "invalid_deadline": {"test_invalid_deadline", "test_deadline_requires_timezone"},
    "expired_deadline": {"test_expired_deadline"},

    # Specialist plan / fanout validation
    "unknown_specialist": {"test_unknown_specialist"},
    "duplicate_specialist": {"test_duplicate_specialist"},
    "invalid_max_fanout": {"test_invalid_max_fanout"},
    "fanout_greater_than_allowed": {"test_fanout_exceeds_allowed"},

    # Version contract
    "packet_version_mismatch": {"test_packet_version_mismatch"},
    "return_schema_version_mismatch": {"test_return_schema_version_mismatch"},

    # Packet constraints
    "oversized_context": {"test_oversized_context"},
    "invalid_allowed_operations_shape": {"test_malformed_packet_operation_items_rejected_not_crashed"},
    "production_write_attempt": {"test_unauthorized_side_effect"},

    # Idempotency
    "duplicate_request": {"test_idempotent_replay"},
    "conflicting_duplicate": {"test_conflicting_duplicate_fails_closed"},
    "stale_cache": {"test_stale_cache_reexecutes"},

    # Provider availability and retry budget
    "provider_unavailable": {"test_provider_unavailable_retries_and_fails_closed"},
    "retry_budget_exhaustion": {"test_rate_limit_budget_exhausted"},
    "rate_limiting": {"test_rate_limit_budget_exhausted"},
    "retry_after": {"test_rate_limit_retry_after_then_success"},

    # Timeout behavior
    "gemini_timeout": {"test_gemini_timeout"},
    "codex_timeout": {"test_codex_timeout"},

    # Worker contract hardening (fail-closed)
    "worker_root_non_mapping": {"test_dispatcher_must_return_mapping"},
    "missing_worker_required_fields": {
        "test_missing_worker_status_fails_closed",
        "test_missing_worker_findings_fails_closed",
        "test_missing_worker_evidence_fails_closed",
    },
    "unsupported_worker_status": {"test_worker_status_unsupported_value_fails_closed"},
    "wrong_worker_output_field_types": {
        "test_worker_status_wrong_type_fails_closed",
        "test_worker_findings_wrong_type_fails_closed",
        "test_worker_evidence_wrong_type_fails_closed",
        "test_worker_unresolved_items_wrong_type_fails_closed",
        "test_malformed_requested_operations_fails_closed",
        "test_malformed_side_effects_fails_closed",
    },
    "excessive_worker_output": {"test_worker_oversized_output_fails_closed"},

    # Model contract
    "model_mismatch": {"test_model_mismatch_fails_closed", "test_missing_model_fails_closed"},

    # Policy / least privilege
    "packet_scoped_least_privilege": {"test_packet_scoped_operation_policy_blocks"},
    "unauthorized_side_effect": {"test_nonempty_side_effects_policy_blocks"},
    "malicious_specialist_output": {"test_malicious_worker_operation_policy_blocked"},

    # Aggregation behavior
    "one_specialist_failure": {"test_one_specialist_failure"},
    "both_specialists_fail": {"test_both_fail"},
    "conflicting_conclusions": {"test_conflicting_conclusions"},
    "successful_fan_in": {"test_successful_fan_in_and_deterministic_order"},

    # Circuit breaker behavior
    "circuit_breaker_open_reset": {
        "test_opens_after_threshold_and_blocks",
        "test_half_open_after_reset_window",
        "test_success_resets_failure_count",
    },

    # Redaction / secret safety
    "credential_redaction": {"test_redaction"},
}


def _flatten(suite: unittest.TestSuite) -> Iterable[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def main() -> int:
    suite = unittest.defaultTestLoader.discover(".", pattern="test_*.py")
    discovered = list(_flatten(suite))
    discovered_names = {case.id().split(".")[-1] for case in discovered}

    missing_matrix_tests = {
        requirement: sorted(names - discovered_names)
        for requirement, names in MATRIX.items()
        if names - discovered_names
    }

    # Re-discover because flattening consumes nested suite iterators on some Python versions.
    suite = unittest.defaultTestLoader.discover(".", pattern="test_*.py")
    buffer = io.StringIO()
    result = unittest.TextTestRunner(stream=buffer, verbosity=1).run(suite)

    failed_ids = [case.id() for case, _ in result.failures]
    error_ids = [case.id() for case, _ in result.errors]
    matrix_pass = not missing_matrix_tests and result.wasSuccessful()
    summary = {
        "environment": "RENDER_STAGING_RUNTIME",
        "suite": "adversarial_failure_injection",
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "failed_test_ids": failed_ids,
        "error_test_ids": error_ids,
        "required_matrix_entries": len(MATRIX),
        "missing_matrix_tests": missing_matrix_tests,
        "probe_pass": matrix_pass,
        "note": "Synthetic failure injection in deployed staging runtime; real Gemini/Codex happy paths are validated separately by staging_runtime_probe.py.",
    }
    print(MARKER + " " + json.dumps(summary, sort_keys=True), flush=True)
    if not matrix_pass:
        # Test runner details contain only synthetic test data; include a short tail for diagnosis.
        tail = buffer.getvalue().splitlines()[-12:]
        print(MARKER + " " + json.dumps({"diagnostic_tail": tail}, sort_keys=True), flush=True)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
