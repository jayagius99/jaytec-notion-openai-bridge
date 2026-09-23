import unittest
from unittest import mock

import five_seat_production_admission as admission


class ProductionAdmissionProbeTests(unittest.TestCase):
    def test_packet_is_exact_low_risk_free_primary_shape(self):
        packet = admission.build_probe_packet(
            deadline="2026-09-23T10:00:00Z"
        )
        self.assertEqual(packet["task_id"], admission.PROBE_TASK_ID)\n        self.assertEqual(admission.PROBE_TASK_ID, "FS08-PRODUCTION-ADMISSION-003")
        self.assertEqual(
            packet["workflow_id"],
            "JAYTEC_ENGINEERING_FS08_PRODUCTION_ADMISSION_V1",
        )
        self.assertEqual(packet["specialist_plan"], ["codex"])
        self.assertEqual(packet["side_effect_policy"], "none")
        self.assertEqual(packet["max_retries"], 0)
        self.assertEqual(
            packet["required_context"]["authority_controller"],
            "CHATGPT_OPENAI_LEAD",
        )
        self.assertEqual(
            packet["required_context"]["specialist_authority"],
            "SUBORDINATE",
        )

    def test_packet_is_deterministic_for_same_deadline(self):
        first = admission.build_probe_packet(
            deadline="2026-09-23T10:00:00Z"
        )
        second = admission.build_probe_packet(
            deadline="2026-09-23T10:00:00Z"
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first["idempotency_key"],
            "fs08-production-admission-v3",
        )

    def test_seat_evidence_is_job_specific(self):
        report = {
            "seat_activity": {
                "WORKER-SEAT-1": {
                    "claims": 1,
                    "releases": 1,
                    "jobs": ["job-a"],
                },
                "WORKER-SEAT-2": {
                    "claims": 3,
                    "releases": 3,
                    "jobs": ["other"],
                },
            }
        }
        evidence = admission._seat_evidence(report, "job-a")
        self.assertEqual(evidence["seats_touched"], ["WORKER-SEAT-1"])
        self.assertEqual(evidence["claim_count"], 1)
        self.assertEqual(evidence["release_count"], 1)

    def test_recovery_count_is_job_specific(self):
        report = {
            "recovery_and_safety_events": [
                {"job_id": "job-a", "event_type": "RETRY"},
                {"job_id": "other", "event_type": "RETRY"},
            ]
        }
        self.assertEqual(admission._recovery_count(report, "job-a"), 1)

    def test_wait_for_local_watch_requires_matching_healthy_leader(self):
        class Reporter:
            def __init__(self):
                self.calls = 0

            def last_60_minutes(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "watch": {
                            "healthy": True,
                            "leader": "five-seat-watch:previous",
                        }
                    }
                return {
                    "watch": {
                        "healthy": True,
                        "leader": "five-seat-watch:current",
                        "leader_epoch": 12,
                        "fence_token": 12,
                    }
                }

        reporter = Reporter()
        result = admission.wait_for_local_watch(
            reporter=reporter,
            expected_leader="five-seat-watch:current",
            timeout_seconds=10,
            sleep_fn=lambda _seconds: None,
            monotonic_fn=lambda: 0.0,
        )
        self.assertEqual(result["leader"], "five-seat-watch:current")
        self.assertEqual(reporter.calls, 2)

    def test_wait_for_local_watch_fails_closed_on_timeout(self):
        class Reporter:
            def last_60_minutes(self, **_kwargs):
                return {
                    "watch": {
                        "healthy": True,
                        "leader": "five-seat-watch:previous",
                    }
                }

        ticks = iter([0.0, 2.0])
        with self.assertRaisesRegex(
            RuntimeError,
            "admission_probe_local_watch_not_ready",
        ):
            admission.wait_for_local_watch(
                reporter=Reporter(),
                expected_leader="five-seat-watch:current",
                timeout_seconds=1,
                sleep_fn=lambda _seconds: None,
                monotonic_fn=lambda: next(ticks),
            )

    def test_success_requires_exact_free_model_and_watch_accept(self):
        queue = object()

        class Authority:
            def current_state(self):
                return {"current_shared_state_version": 55}

        class Service:
            def job_status(self, _job_id):
                return {
                    "job": {
                        "fabric_state": "SUCCEEDED",
                        "watch_decision": "ACCEPT",
                    }
                }

        class Reporter:
            def last_60_minutes(self, **_kwargs):
                return {
                    "watch": {"healthy": True},
                    "summary": {"seats_total": 5, "seats_free": 5},
                    "seat_activity": {
                        "WORKER-SEAT-3": {
                            "claims": 1,
                            "releases": 1,
                            "jobs": ["fabric-proof"],
                        }
                    },
                    "recovery_and_safety_events": [],
                }

        with mock.patch.object(
            admission,
            "submit_low_risk_task_packet",
            return_value={"job_id": "fabric-proof"},
        ), mock.patch.object(
            admission,
            "_latest_handoff_evidence",
            return_value={
                "provider_identity": admission.EXPECTED_CODEX_MODEL,
                "partial_side_effect_status": "NONE",
                "cost_policy": {
                    "mode": "ZERO_SPEND",
                    "allow_paid": False,
                    "max_cost_usd": 0,
                    "provider_mode": "FREE_ONLY",
                },
            },
        ):
            result = admission.run_probe(
                database_url="postgresql://unused",
                queue=queue,
                authority=Authority(),
                service=Service(),
                reporter=Reporter(),
                deadline="2026-09-23T10:00:00Z",
                sleep_fn=lambda _seconds: None,
                monotonic_fn=lambda: 1.0,
            )
        self.assertTrue(result["passed"])
        self.assertEqual(result["watch_decision"], "ACCEPT")
        self.assertEqual(
            result["provider_identity"],
            admission.EXPECTED_CODEX_MODEL,
        )
        self.assertFalse(result["paid_fallback_allowed"])


if __name__ == "__main__":
    unittest.main()
