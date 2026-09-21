from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
import unittest

from autorecovery_components import (
    ManusLiteRecoveryInvoker,
    master_gate_handoff_id,
)
from autorecovery_watch_ingress import (
    WatchIngressError,
    _master_gate_context,
    execute_watch_cycle,
)
from autorecovery_supervisor import AssignmentState, StopReason
from test_autorecovery_components import FakeRegistry, FakeRuntime, checkpoint
from test_autorecovery_watch_ingress import (
    FakeBrokerStore,
    broker_checkpoint,
    cycle_payload,
    master_gate_payload,
)


REVIEW_ID = "d" * 64


def redirect_gate():
    value = master_gate_payload()
    value.update(
        {
            "controller_action": "REDIRECT",
            "controller_review_id": REVIEW_ID,
            "controller_direction": (
                "Stay on G03. Close the missing alternate-route denial proof "
                "and return fresh evidence bound to this redirect."
            ),
        }
    )
    return value


class RedirectRuntime:
    def __init__(self):
        self.handoffs = []
        self.redirected = False

    def task_status_readonly(self, worker_id):
        assert worker_id == "worker-existing"
        if self.redirected:
            return {
                "status": "PENDING",
                "provider_task_id": worker_id,
                "requested_profile": "lite",
                "observed_profile": "lite",
            }
        return {
            "status": "VERIFIED_COMPLETE",
            "result": {
                "status": "SUCCESS",
                "summary": "stale pre-redirect success",
                "evidence": [
                    {
                        "kind": "audit_record",
                        "source": "JAYTEC_MASTER_GATE_HANDOFF",
                        "reference": (
                            "FORGE-GENESIS-ACTIVATION-001:"
                            "fence:9:master-gate:G03:" + ("c" * 16)
                        ),
                    }
                ],
                "specialist_requests": [],
            },
        }

    def task_status(self, worker_id):
        assert worker_id == "worker-existing"
        return {
            "status": "PENDING",
            "provider_task_id": worker_id,
            "requested_profile": "lite",
            "observed_profile": "lite",
        }

    def continue_task_handoff(self, worker_id, **kwargs):
        assert worker_id == "worker-existing"
        self.handoffs.append((worker_id, dict(kwargs)))
        self.redirected = True
        return {
            "status": "CONTINUED",
            "provider_task_id": worker_id,
            "requested_profile": "lite",
            "observed_profile_verified": True,
            "idempotent_replay": False,
        }


class ControllerRedirectTests(unittest.TestCase):
    def test_redirect_context_is_bounded_and_validated(self):
        parsed = _master_gate_context(redirect_gate(), broker_checkpoint())
        self.assertEqual(parsed["controller_action"], "REDIRECT")
        self.assertEqual(parsed["controller_review_id"], REVIEW_ID)
        self.assertIn("alternate-route denial proof", parsed["controller_direction"])

        broken = master_gate_payload()
        broken["controller_review_id"] = REVIEW_ID
        with self.assertRaisesRegex(
            WatchIngressError,
            "MASTER_GATE_CONTROLLER_CONTEXT_INCOMPLETE",
        ):
            _master_gate_context(broken, broker_checkpoint())

        bad = redirect_gate()
        bad["controller_review_id"] = "short"
        with self.assertRaisesRegex(
            WatchIngressError,
            "MASTER_GATE_CONTROLLER_REVIEW_ID_INVALID",
        ):
            _master_gate_context(bad, broker_checkpoint())

    def test_redirect_changes_handoff_receipt_id(self):
        cp = checkpoint()
        base = {
            "gate_id": "G03",
            "graph_sha256": "a" * 64,
            "checkpoint_number": cp.checkpoint_number,
        }
        normal = master_gate_handoff_id(cp, 4, base)
        redirected = master_gate_handoff_id(
            cp,
            4,
            {**base, "controller_review_id": REVIEW_ID},
        )
        self.assertNotEqual(normal, redirected)
        self.assertTrue(redirected.endswith(":review:" + REVIEW_ID[:16]))

    def test_gate_handoff_requires_manifest_and_carries_owner_direction(self):
        runtime = FakeRuntime()
        cp = checkpoint()
        context = {
            "schema_version": "FORGE_MASTER_GATE_DIRECTIVE_V1",
            "kind": "MASTER_GATE_DIRECTIVE",
            "gate_id": "G03",
            "phase": "SECURITY",
            "title": "Security Audit #47",
            "status": "IN_PROGRESS",
            "depends_on": ["G02"],
            "evidence": ["enforcement map"],
            "graph_sha256": "a" * 64,
            "checkpoint_number": cp.checkpoint_number,
            "controller_action": "REDIRECT",
            "controller_review_id": REVIEW_ID,
            "controller_direction": "Produce fresh alternate-route denial evidence.",
            "instruction": "Work only on G03.",
        }
        result = ManusLiteRecoveryInvoker(
            runtime,
            FakeRegistry(),
        ).continue_gate_directive(
            checkpoint=cp,
            worker_id="manus-existing",
            fencing_token=4,
            gate_context=context,
        )
        self.assertTrue(result.accepted)
        _, kwargs = runtime.handoffs[0]
        self.assertIn(":review:" + REVIEW_ID[:16], kwargs["handoff_id"])
        handoff = kwargs["handoff_context"]
        manifest = handoff["gate_evidence_manifest_requirement"]
        self.assertEqual(manifest["kind"], "artifact")
        self.assertEqual(manifest["source"], "JAYTEC_GATE_EVIDENCE_MANIFEST")
        self.assertIn("/gate_evidence/G03.json", manifest["reference_format"])
        redirect = handoff["assignment_controller_redirect"]
        self.assertEqual(redirect["action"], "REDIRECT")
        self.assertEqual(redirect["review_id"], REVIEW_ID)
        self.assertIn("fresh alternate-route", redirect["direction"])

    def test_redirect_reopens_same_fenced_worker_without_recovery_attempt(self):
        refs = {"security/root-owner-control-v1": "b" * 40}
        store = FakeBrokerStore()
        store.state = AssignmentState(
            **{
                **store.state.__dict__,
                "checkpoint": broker_checkpoint(),
                "stop_reason": StopReason.WAITING_FOR_DEPENDENCY,
                "last_error": "MANUS_TERMINAL:SUCCESS:stale",
                "progress_marker": "MANUS_TERMINAL:SUCCESS:stale",
                "recovery_attempts": 0,
                "fencing_token": 9,
            }
        )
        runtime = RedirectRuntime()
        active = SimpleNamespace(
            active=True,
            callable_worker_routes=("jaytec-manus-lite-v1",),
            to_dict=lambda: {},
        )
        payload = cycle_payload(refs)
        payload["master_gate"] = redirect_gate()

        with (
            patch(
                "autorecovery_watch_ingress.prepare_schema_if_authorized",
                return_value={"status": "PASS", "schema_present": True},
            ),
            patch(
                "autorecovery_watch_ingress.runtime_status",
                return_value=active,
            ),
            patch(
                "autorecovery_watch_ingress.PostgresAssignmentStore",
                return_value=store,
            ),
        ):
            result = execute_watch_cycle(
                payload,
                database_url="postgresql://unused",
                manus_runtime=runtime,
                registry=object(),
                runtime_components_registered=True,
                env={},
            )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["assignment"]["worker_id"], "worker-existing")
        self.assertEqual(result["assignment"]["fencing_token"], 9)
        self.assertEqual(result["assignment"]["recovery_attempts"], 0)
        self.assertEqual(len(runtime.handoffs), 1)
        _, kwargs = runtime.handoffs[0]
        self.assertIn(":review:" + REVIEW_ID[:16], kwargs["handoff_id"])
        self.assertEqual(
            kwargs["handoff_context"]["controller_direction"],
            redirect_gate()["controller_direction"],
        )


if __name__ == "__main__":
    unittest.main()
