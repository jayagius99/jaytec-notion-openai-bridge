import unittest
from types import SimpleNamespace

from autorecovery_components import (
    ManusLiteHealthProbe,
    ManusLiteRecoveryInvoker,
    ObservedRefsCheckpointVerifier,
)
from autorecovery_supervisor import (
    AssignmentCheckpoint,
    RecoveryRoute,
)


HEAD = "a" * 40


def checkpoint(authority=None):
    return AssignmentCheckpoint(
        task_id="FORGE-GENESIS-ACTIVATION-001",
        objective="Prepare Forge safely.",
        current_phase="software convergence",
        completed_work=("watch hardened",),
        remaining_work=("audit",),
        last_safe_checkpoint="checkpoint-1",
        repo="jayagius99/jaytec-work-engine-v2-g1",
        branch="security/root-owner-control-v1",
        commit_head=HEAD,
        open_pr=17,
        current_files_state={"dirty": False},
        tests_completed=("watch-ci",),
        known_failures=(),
        active_constraints=("no activation",),
        authority_envelope=authority or {
            "root_owner": "Jay",
            "allowed_actions": ["create isolated branches", "modify safe files", "open pull requests"],
            "connector_purposes": {"github": "write"},
            "connector_mutation_authorized": True,
        },
        cost_envelope={"paid_fallback": False},
        dependencies=(),
        next_intended_action="Continue safe software work.",
        worker_specialist_preference=("manus-lite",),
        checkpoint_number=1,
    ).validate()


class FakeRegistry:
    pass


class FakeRuntime:
    def __init__(self, start=None, status=None):
        self.start = start or {
            "status": "STARTED",
            "provider_task_id": "manus-1",
            "requested_profile": "lite",
            "observed_profile_verified": True,
        }
        self.status = status or {"status": "PENDING"}
        self.requests = []

    def start_task_idempotent(self, raw, registry):
        self.requests.append(raw)
        return dict(self.start)

    def task_status(self, worker_id):
        return dict(self.status)


class RuntimeComponentTests(unittest.TestCase):
    def test_exact_head_verifier_accepts_only_attested_branch_head(self):
        verifier = ObservedRefsCheckpointVerifier(
            {"security/root-owner-control-v1": HEAD}
        )
        self.assertTrue(verifier.verify(checkpoint())[0])
        mismatch = ObservedRefsCheckpointVerifier(
            {"security/root-owner-control-v1": "b" * 40}
        )
        self.assertEqual(
            mismatch.verify(checkpoint()),
            (False, "CHECKPOINT_HEAD_MISMATCH"),
        )

    def test_unattested_branch_fails_closed(self):
        verifier = ObservedRefsCheckpointVerifier({"main": HEAD})
        self.assertEqual(
            verifier.verify(checkpoint()),
            (False, "CHECKPOINT_BRANCH_NOT_ATTESTED"),
        )

    def test_invoker_starts_lite_worker_with_fencing_identity(self):
        runtime = FakeRuntime()
        invoker = ManusLiteRecoveryInvoker(runtime, FakeRegistry())
        result = invoker.invoke(
            checkpoint=checkpoint(),
            continuation_packet={"instruction": "Resume"},
            route=RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT,
            fencing_token=9,
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.worker_id, "manus-1")
        self.assertIn("recovery:9", runtime.requests[0])
        self.assertIn('"github": "write"', runtime.requests[0])
        self.assertIn("Do not activate Forge", runtime.requests[0])

    def test_invoker_rejects_broad_connector_scope(self):
        cp = checkpoint({
            "root_owner": "Jay",
            "allowed_actions": ["inspect"],
            "connector_purposes": {"github": "read", "render": "read"},
            "connector_mutation_authorized": False,
        })
        result = ManusLiteRecoveryInvoker(FakeRuntime(), FakeRegistry()).invoke(
            checkpoint=cp,
            continuation_packet={},
            route=RecoveryRoute.SAME_WORKER_PROVIDER,
            fencing_token=1,
        )
        self.assertFalse(result.accepted)

    def test_health_pending_is_healthy(self):
        health = ManusLiteHealthProbe(FakeRuntime()).wait_for_healthy(
            task_id="task",
            fencing_token=1,
            worker_id="manus-1",
            timeout_seconds=15,
        )
        self.assertTrue(health.healthy)
        self.assertEqual(health.progress_marker, "MANUS_PENDING")

    def test_verified_complete_is_terminal_marker_not_failure(self):
        runtime = FakeRuntime(status={
            "status": "VERIFIED_COMPLETE",
            "result": {"status": "SUCCESS", "summary": "done"},
        })
        health = ManusLiteHealthProbe(runtime).wait_for_healthy(
            task_id="task",
            fencing_token=1,
            worker_id="manus-1",
            timeout_seconds=15,
        )
        self.assertTrue(health.healthy)
        self.assertTrue(health.progress_marker.startswith("MANUS_TERMINAL:SUCCESS:"))


if __name__ == "__main__":
    unittest.main()
