from __future__ import annotations

import unittest

from watch_controller_advisor import (
    EXPECTED_DEEPSEEK_MODEL,
    EXPECTED_NEMO_MODEL,
    FREE_CONTROLLER_MODE,
    WatchControllerAdvisorError,
    advise_free_quorum,
    build_free_packet,
    make_request,
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
        fencing_token=15,
        current_direction="Complete G03 with evidence.",
        candidate_next_gate={
            "gate_id": "G04",
            "title": "Next bounded software gate",
            "phase": "BUILD",
            "owner_boundary": owner_boundary,
            "watch_eligible": not owner_boundary,
        },
    )


def specialist_result(model, *, decision="APPROVE_CONTINUE", direction="", status="SUCCESS"):
    return {
        "status": status,
        "model": model,
        "findings": ["reviewed current evidence"],
        "evidence": ["exact-head evidence"],
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
            "provider_model": model,
            "provider_fallbacks": False,
            "model_fallbacks": False,
        },
    }


class WatchControllerFreeQuorumTests(unittest.TestCase):
    def test_free_packets_are_side_effect_free_and_subordinate(self):
        for name in ("deepseek", "nemo"):
            packet = build_free_packet(request(), specialist=name)
            self.assertEqual(packet["allowed_operations"], [])
            self.assertEqual(packet["max_retries"], 0)
            ctx = packet["required_context"]
            self.assertEqual(ctx["authority_controller"], "CHATGPT_OPENAI_LEAD")
            self.assertEqual(ctx["specialist_authority"], "SUBORDINATE")
            self.assertEqual(ctx["controller_review_mode"], FREE_CONTROLLER_MODE)

    def test_clean_evidence_requires_both_free_reviewers(self):
        out = advise_free_quorum(
            request(),
            deepseek_dispatch=lambda _p: specialist_result(EXPECTED_DEEPSEEK_MODEL),
            nemo_dispatch=lambda _p: specialist_result(EXPECTED_NEMO_MODEL),
        )
        self.assertEqual(out["status"], "PASS")
        self.assertEqual(out["decision"], "APPROVE_CONTINUE")
        self.assertEqual(out["advisory_mode"], FREE_CONTROLLER_MODE)
        self.assertEqual(out["worker_id"], "worker-existing")
        self.assertEqual(out["fencing_token"], 15)
        self.assertEqual(
            [x["model"] for x in out["reviewers"]],
            [EXPECTED_DEEPSEEK_MODEL, EXPECTED_NEMO_MODEL],
        )

    def test_unresolved_evidence_rejects_without_provider_calls(self):
        called = []
        out = advise_free_quorum(
            request(unresolved=["missing proof"]),
            deepseek_dispatch=lambda _p: called.append("deepseek"),
            nemo_dispatch=lambda _p: called.append("nemo"),
        )
        self.assertEqual(called, [])
        self.assertEqual(out["decision"], "REJECT_EVIDENCE")
        self.assertFalse(out["provider_invoked"])

    def test_replan_rejects_without_provider_calls(self):
        called = []
        out = advise_free_quorum(
            request(review="REPLAN_REQUIRED"),
            deepseek_dispatch=lambda _p: called.append("deepseek"),
            nemo_dispatch=lambda _p: called.append("nemo"),
        )
        self.assertEqual(called, [])
        self.assertEqual(out["decision"], "REJECT_EVIDENCE")

    def test_owner_boundary_escalates_without_provider_calls(self):
        called = []
        out = advise_free_quorum(
            request(owner_boundary=True),
            deepseek_dispatch=lambda _p: called.append("deepseek"),
            nemo_dispatch=lambda _p: called.append("nemo"),
        )
        self.assertEqual(called, [])
        self.assertEqual(out["decision"], "ESCALATE_JAY")

    def test_disagreement_never_approves(self):
        out = advise_free_quorum(
            request(),
            deepseek_dispatch=lambda _p: specialist_result(EXPECTED_DEEPSEEK_MODEL),
            nemo_dispatch=lambda _p: specialist_result(
                EXPECTED_NEMO_MODEL,
                decision="REDIRECT",
                direction="Run one more proof.",
            ),
        )
        self.assertEqual(out["decision"], "REJECT_EVIDENCE")

    def test_provider_failure_fails_closed_without_sol_fallback(self):
        def fail(_packet):
            raise RuntimeError("unavailable")

        out = advise_free_quorum(
            request(),
            deepseek_dispatch=fail,
            nemo_dispatch=lambda _p: specialist_result(EXPECTED_NEMO_MODEL),
        )
        self.assertEqual(out["status"], "FAILED_CLOSED")
        self.assertIn("DEEPSEEK_UNAVAILABLE", out["reason"])
        self.assertNotIn("SOL", out["reason"])

    def test_provider_fallback_claim_is_rejected(self):
        bad = specialist_result(EXPECTED_DEEPSEEK_MODEL)
        bad["bridge_diagnostics"]["provider_fallbacks"] = True
        with self.assertRaisesRegex(
            WatchControllerAdvisorError,
            "PROVIDER_FALLBACK_FORBIDDEN",
        ):
            advise_free_quorum(
                request(),
                deepseek_dispatch=lambda _p: bad,
                nemo_dispatch=lambda _p: specialist_result(EXPECTED_NEMO_MODEL),
            )

    def test_wrong_exact_model_is_rejected(self):
        bad = specialist_result("deepseek/other:free")
        with self.assertRaisesRegex(WatchControllerAdvisorError, "MODEL_MISMATCH"):
            advise_free_quorum(
                request(),
                deepseek_dispatch=lambda _p: bad,
                nemo_dispatch=lambda _p: specialist_result(EXPECTED_NEMO_MODEL),
            )


if __name__ == "__main__":
    unittest.main()
