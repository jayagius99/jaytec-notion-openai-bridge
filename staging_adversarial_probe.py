"""STAGING ONLY: run the bounded adversarial/failure-injection suite in Render.

This runs repository tests inside the deployed staging runtime. Provider happy-path
calls are handled separately by staging_runtime_probe.py. This script emits only a
compact matrix summary and failure IDs; it never emits secrets or provider output.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from typing import Iterable

MARKER = "JAYTEC_STAGING_ADVERSARIAL_PROBE"

MATRIX = {
    "malformed_json": {"test_malformed_json"},
    "missing_task_id": {"test_missing_task_id"},
    "missing_deadline": {"test_missing_deadline"},
    "unknown_specialist": {"test_unknown_specialist"},
    "oversized_context": {"test_oversized_context"},
    "duplicate_request": {"test_idempotent_replay"},
    "conflicting_duplicate": {"test_conflicting_duplicate_fails_closed"},
    "gemini_timeout": {"test_gemini_timeout"},
    "codex_timeout": {"test_codex_timeout"},
    "rate_limiting": {"test_rate_limit_budget_exhausted"},
    "retry_after": {"test_rate_limit_retry_after_then_success"},
    "one_specialist_failure": {"test_one_specialist_failure"},
    "both_specialists_fail": {"test_both_fail"},
    "conflicting_conclusions": {"test_conflicting_conclusions"},
    "model_mismatch": {"test_model_mismatch_fails_closed", "test_missing_model_fails_closed"},
    "unauthorized_side_effect": {"test_unauthorized_side_effect", "test_nonempty_side_effects_policy_blocks"},
    "malicious_specialist_output": {"test_malicious_worker_operation_policy_blocked", "test_malformed_requested_operations_fails_closed"},
    "stale_cache": {"test_stale_cache_reexecutes"},
    "circuit_breaker_open_reset": {"test_opens_after_threshold_and_blocks", "test_half_open_after_reset_window", "test_success_resets_failure_count"},
    "successful_fan_in": {"test_successful_fan_in_and_deterministic_order"},
    "credential_redaction": {"test_redaction"},
    "packet_operation_type_hardening": {"test_malformed_packet_operation_items_rejected_not_crashed"},
    "packet_scoped_least_privilege": {"test_packet_scoped_operation_policy_blocks"},
    "worker_side_effect_type_hardening": {"test_malformed_side_effects_fails_closed"},
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
