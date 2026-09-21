from __future__ import annotations

import unittest

from watch_controller_advisor import (
    EXPECTED_SOL_MODEL,
    WatchControllerAdvisorError,
    advise,
    build_sol_packet,
    make_request,
    validate_request,
)


def request(*, review="EVIDENCE_ACCEPTED", unresolved=None, owner_boundary=False):
    return make_request(
        gate_id="G03",
        checkpoint_number=3,
        controller_generation=9,
        graph_sha256="a" * 64,
        gate_result_sha256="b" * 64,
        manifest_sha256="c" * 64,
        jaytec_review_id="d" * 64,
        jaytec_review_decision=review,
        evidence_reviewed=["tests pass", "manifest exact-head"],
        unresolved_items=list(unresolved or []),
        worker_id="worker-existing",
        fencing_token=14,
        current_direction="Complete G03 with evidence.",
        candidate_next_gate={
            "gate_id": "G04",
            "title": "Next bounded software gate",
            "phase": "BUILD",
            "owner_boundary": owner_boundary,
            "watch_eligible": not owner_boundary,
        },
    )


def sol_result(decision="APPROVE_CONTINUE", direction=""):
    return {
        "status": "SUCCESS",
        "model": EXPECTED_SOL_MODEL,
        "findings": ["reviewed current evidence"],
        "evidence": ["tests pass"],
        "confidence": "high",
        "conclusion": {
            "decision": decision,
            "direction": direction,
            "rationale": "Evidence is internally consistent for the bounded scope.",
        },
        "unresolved_items": [],
        "files_or_artifacts": [],
        "architecture_changes_required": [],
        "knowledge_writeback_proposal": [],
        "side_effects_attempted": [],
        "requested_operations": [],
        "bridge_diagnostics": {
            "provider_model": EXPECTED_SOL_MODEL,
            "model_identity_observed": True,
            "provider_fallbacks": False,
        },
    }


class WatchControllerAdvisorTests(unittest.TestCase):
    def test_request_round_trip_and_digest(self):
        value = request()
        self.assertEqual(validate_request(value), value)
        self.assertEqual(len(value["request_sha256"]), 64)

    def test_sol_packet_is_advisory_and_side_effect_free(self):
        packet = build_sol_packet(request())
        self.assertEqual(packet["allowed_operations"], [])
        self.assertEqual(packet["max_retries"], 0)
        ctx = packet["required_context"]
        self.assertEqual(ctx["authority_controller"], "CHATGPT_OPENAI_LEAD")
        self.assertEqual(ctx["specialist_authority"], "SUBORDINATE_ADVISORY_ONLY")
        self.assertEqual(ctx["return_to"], "WATCH_CONTROLLER")

    def test_approve_continue_passes_for_clean_evidence(self):
        out = advise(request(), engineering_dispatch=lambda _packet: sol_result())
        self.assertEqual(out["status"], "PASS")
        self.assertEqual(out["decision"], "APPROVE_CONTINUE")
        self.assertEqual(out["model"], EXPECTED_SOL_MODEL)
        self.assertEqual(out["worker_id"], "worker-existing")
        self.assertEqual(out["fencing_token"], 14)
        self.assertEqual(
            out["authority"],
            "ADVISORY_ONLY_NO_ASSIGNMENT_OWNER_AUTHORITY",
        )

    def test_redirect_passes_and_keeps_bounded_direction(self):
        out = advise(
            request(unresolved=["missing hostile review"]),
            engineering_dispatch=lambda _packet: sol_result(
                "REDIRECT",
                "Run the hostile review and return exact-head evidence.",
            ),
        )
        self.assertEqual(out["status"], "PASS")
        self.assertEqual(out["decision"], "REDIRECT")
        self.assertIn("hostile review", out["direction"])

    def test_sol_cannot_bypass_jaytec_replan(self):
        with self.assertRaisesRegex(WatchControllerAdvisorError, "CANNOT_BYPASS_REPLAN"):
            advise(
                request(review="REPLAN_REQUIRED"),
                engineering_dispatch=lambda _packet: sol_result(),
            )

    def test_sol_cannot_approve_unresolved(self):
        with self.assertRaisesRegex(WatchControllerAdvisorError, "CANNOT_APPROVE_UNRESOLVED"):
            advise(
                request(unresolved=["missing evidence"]),
                engineering_dispatch=lambda _packet: sol_result(),
            )

    def test_wrong_model_fails_closed(self):
        bad = sol_result()
        bad["model"] = "other/model"
        with self.assertRaisesRegex(WatchControllerAdvisorError, "MODEL_MISMATCH"):
            advise(request(), engineering_dispatch=lambda _packet: bad)

    def test_side_effect_claim_rejected(self):
        bad = sol_result()
        bad["side_effects_attempted"] = ["write repo"]
        with self.assertRaisesRegex(WatchControllerAdvisorError, "SIDE_EFFECT"):
            advise(request(), engineering_dispatch=lambda _packet: bad)

    def test_provider_unavailable_returns_safe_failed_closed(self):
        def fail(_packet):
            raise RuntimeError("provider detail should not escape")

        out = advise(request(), engineering_dispatch=fail)
        self.assertEqual(out["status"], "FAILED_CLOSED")
        self.assertEqual(out["reason"], "SOL_ADVISOR_UNAVAILABLE:RuntimeError")
        self.assertNotIn("provider detail", str(out))

    def test_request_digest_tampering_rejected(self):
        value = request()
        value["gate_id"] = "G04"
        with self.assertRaisesRegex(WatchControllerAdvisorError, "DIGEST_MISMATCH"):
            validate_request(value)


if __name__ == "__main__":
    unittest.main()
