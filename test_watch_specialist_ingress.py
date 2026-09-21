from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from autorecovery_watch_ingress import execute_watch_cycle
from manus_governance import specialist_request
from test_autorecovery_watch_ingress import (
    FakeBrokerRuntime,
    FakeBrokerStore,
    cycle_payload,
)
from watch_specialist_broker import dispatch_manus_model_requests


def model_request(
    name: str,
    objective: str = "Provide bounded help.",
    *,
    emergency: bool = False,
):
    context = {"gate_id": "G03", "artifact_sha256": "a" * 64}
    if emergency:
        context["emergency"] = True
    return specialist_request(
        parent_task_id="FORGE-GENESIS-ACTIVATION-001:recovery:14:test",
        specialist=name,
        objective=objective,
        reason="Manus needs a JAYTEC specialist.",
        required_context=context,
    )


def result(model: str):
    return {
        "status": "SUCCESS",
        "model": model,
        "findings": ["finding"],
        "evidence": ["evidence"],
        "confidence": "high",
        "conclusion": {"direction": "continue bounded work"},
        "unresolved_items": [],
        "files_or_artifacts": [],
        "architecture_changes_required": [],
        "knowledge_writeback_proposal": [],
        "side_effects_attempted": [],
        "requested_operations": [],
    }


class FakeModelRequestRuntime(FakeBrokerRuntime):
    def __init__(self, names):
        super().__init__()
        self.names = names

    def task_status_readonly(self, worker_id):
        assert worker_id == "worker-existing"
        return {
            "status": "VERIFIED_COMPLETE",
            "result": {
                "status": "NEEDS_JAYTEC",
                "summary": "Need bounded specialist help.",
                "evidence": [],
                "changes_made": [],
                "unresolved_items": ["specialist help required"],
                "specialist_requests": [
                    __import__("json").dumps(
                        model_request(
                            name,
                            emergency=(name == "core_triad"),
                        ),
                        sort_keys=True,
                    )
                    for name in self.names
                ],
                "verification": {
                    "instruction_match_verified": True,
                    "scope_verified": True,
                    "evidence_verified": False,
                    "no_unauthorized_side_effects": True,
                    "duplicate_work_check_passed": True,
                },
            },
        }


class WatchSpecialistIngressTests(unittest.TestCase):
    def run_cycle(self, names, runner):
        refs = {"security/root-owner-control-v1": "b" * 40}
        store = FakeBrokerStore()
        runtime = FakeModelRequestRuntime(names)
        active = SimpleNamespace(
            active=True,
            callable_worker_routes=("jaytec-manus-lite-v1",),
            to_dict=lambda: {},
        )
        with (
            patch(
                "autorecovery_watch_ingress.prepare_schema_if_authorized",
                return_value={"status": "PASS", "schema_present": True},
            ),
            patch("autorecovery_watch_ingress.runtime_status", return_value=active),
            patch("autorecovery_watch_ingress.PostgresAssignmentStore", return_value=store),
        ):
            out = execute_watch_cycle(
                cycle_payload(refs, include_broker=False),
                database_url="postgresql://unused",
                manus_runtime=runtime,
                registry=object(),
                runtime_components_registered=True,
                specialist_runner=runner,
                env={},
            )
        return out, store, runtime

    def test_deepseek_result_returns_to_same_manus_worker_and_fence(self):
        def runner(requests):
            self.assertEqual([r["specialist"] for r in requests], ["deepseek"])
            return dispatch_manus_model_requests(
                requests,
                dispatchers={
                    "deepseek": lambda _packet: result(
                        "deepseek/deepseek-v4-flash-0731:free"
                    )
                },
            )

        out, store, runtime = self.run_cycle(["deepseek"], runner)
        self.assertEqual(out["status"], "PASS")
        self.assertEqual(
            out["decision"]["reason"],
            "JAYTEC_INTERNAL_ASSISTANCE_CONTINUED",
        )
        self.assertEqual(out["assignment"]["worker_id"], "worker-existing")
        self.assertEqual(out["assignment"]["fencing_token"], 9)
        self.assertEqual(len(runtime.handoffs), 1)
        worker_id, kwargs = runtime.handoffs[0]
        self.assertEqual(worker_id, "worker-existing")
        self.assertEqual(
            kwargs["handoff_context"]["kind"],
            "SPECIALIST_REQUEST_RESULTS",
        )
        row = kwargs["handoff_context"]["request_results"][0]
        self.assertEqual(row["specialist"], "deepseek")
        self.assertEqual(
            row["model"],
            "deepseek/deepseek-v4-flash-0731:free",
        )
        self.assertTrue(
            str(store.state.progress_marker).startswith(
                "JAYTEC_ASSISTANCE_HANDOFF:"
            )
        )

    def test_two_routine_specialists_return_as_one_handoff(self):
        models = {
            "deepseek": "deepseek/deepseek-v4-flash-0731:free",
            "nemo": "nvidia/nemotron-3-ultra-550b-a55b:free",
        }

        def runner(requests):
            return dispatch_manus_model_requests(
                requests,
                dispatchers={
                    name: (lambda _packet, m=model: result(m))
                    for name, model in models.items()
                },
            )

        out, _, runtime = self.run_cycle(["deepseek", "nemo"], runner)
        self.assertEqual(out["status"], "PASS")
        self.assertEqual(len(runtime.handoffs), 1)
        rows = runtime.handoffs[0][1]["handoff_context"]["request_results"]
        self.assertEqual(
            [row["specialist"] for row in rows],
            ["deepseek", "nemo"],
        )
        self.assertEqual(out["assignment"]["worker_id"], "worker-existing")
        self.assertEqual(out["assignment"]["fencing_token"], 9)

    def test_model_help_fails_closed_when_jaytec_runner_missing(self):
        out, _, runtime = self.run_cycle(["deepseek"], None)
        self.assertEqual(out["status"], "BLOCKED_FAIL_CLOSED")
        self.assertEqual(
            out["reason"],
            "MODEL_SPECIALIST_RUNNER_UNAVAILABLE",
        )
        self.assertEqual(runtime.handoffs, [])

    def test_core_triad_emergency_holds_without_direct_manus_handoff(self):
        def runner(_requests):
            raise AssertionError(
                "Core Triad emergency must surface before routine specialist dispatch"
            )

        out, _, runtime = self.run_cycle(["core_triad"], runner)
        self.assertEqual(out["status"], "PASS")
        self.assertEqual(out["decision"]["action"], "HOLD")
        self.assertEqual(
            out["decision"]["reason"],
            "JAYTEC_CORE_TRIAD_EMERGENCY_REQUIRED",
        )
        self.assertEqual(
            [r["request_id"] for r in out["core_triad_emergency_requests"]],
            [model_request("core_triad", emergency=True)["request_id"]],
        )
        self.assertEqual(runtime.handoffs, [])

    def test_gemini_request_is_rejected_in_watch_lane(self):
        out, _, runtime = self.run_cycle(["gemini"], lambda _requests: {})
        self.assertEqual(out["status"], "BLOCKED_FAIL_CLOSED")
        self.assertIn("NOT_ALLOWLISTED:gemini", out["reason"])
        self.assertEqual(runtime.handoffs, [])


if __name__ == "__main__":
    unittest.main()
