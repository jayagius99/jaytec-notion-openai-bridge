import unittest
from types import SimpleNamespace

from autorecovery_components import (
    HARD_RECOVERY_CONSTRAINTS,
    assignment_owner_redirect_handoff_id,
    ManusLiteHealthProbe,
    ManusLiteRecoveryInvoker,
    ManusLiteHealthProbe,
    master_gate_handoff_id,
    master_gate_result_has_receipt,
    ObservedRefsCheckpointVerifier,
)
from manus_adapter import MANUS_MAX_MESSAGE_CHARS
from manus_governance import build_minimal_task_packet
from manus_runtime import _prompt, parse_start_request
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
        self.handoffs = []

    def start_task_idempotent(self, raw, registry):
        self.requests.append(raw)
        return dict(self.start)

    def continue_task_handoff(self, *args, **kwargs):
        self.handoffs.append((args, kwargs))
        return {
            "status": "CONTINUED",
            "provider_task_id": args[0] if args else "",
            "requested_profile": "lite",
            "observed_profile_verified": True,
        }

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


    def test_large_private_broker_snapshot_is_compacted_below_manus_message_ceiling(self):
        runtime = FakeRuntime()
        huge_broker = {
            "schema_version": "JAYTEC_GITHUB_BROKER_CONTEXT_V1",
            "kind": "PRIVATE_REPO_BOOTSTRAP",
            "repo": "jayagius99/jaytec-work-engine-v2-g1",
            "sha256": "f" * 64,
            "padding": "X" * 4200,
        }
        invoker = ManusLiteRecoveryInvoker(
            runtime,
            FakeRegistry(),
            broker_context=huge_broker,
        )
        result = invoker.invoke(
            checkpoint=checkpoint(),
            continuation_packet={
                "instruction": "Resume — do not recreate completed work",
                "completed_work": ["x" * 500],
                "remaining_work": ["y" * 500],
            },
            route=RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT,
            fencing_token=4,
        )
        self.assertTrue(result.accepted)
        raw = runtime.requests[0]
        self.assertNotIn("X" * 200, raw)
        self.assertIn('"available": true', raw)
        self.assertIn('"sha256": "' + ("f" * 64) + '"', raw)
        self.assertIn("return NEEDS_JAYTEC", raw)

        req = parse_start_request(raw)
        packet = build_minimal_task_packet(
            task_id=req.task_id,
            objective=req.objective,
            scope=req.scope,
            authority_source=req.authority_source,
            allowed_actions=list(req.allowed_actions),
            required_context=req.required_context,
            constraints=list(req.constraints),
            reference_ids=list(req.reference_ids),
        )
        rendered = _prompt(packet)
        self.assertLessEqual(len(rendered), MANUS_MAX_MESSAGE_CHARS)

    def test_gate_directive_handoff_keeps_same_worker_and_lite_identity(self):
        runtime = FakeRuntime()
        cp = checkpoint()
        invoker = ManusLiteRecoveryInvoker(runtime, FakeRegistry())
        result = invoker.continue_gate_directive(
            checkpoint=cp,
            worker_id="manus-existing",
            fencing_token=4,
            gate_context={
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
                "instruction": "Work only on G03.",
            },
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.worker_id, "manus-existing")
        self.assertEqual(len(runtime.handoffs), 1)
        args, kwargs = runtime.handoffs[0]
        self.assertEqual(args[0], "manus-existing")
        self.assertIn("master-gate:G03:", kwargs["handoff_id"])
        self.assertEqual(
            kwargs["handoff_context"]["kind"],
            "MASTER_GATE_DIRECTIVE",
        )

    def test_assignment_owner_redirect_keeps_same_worker_and_is_idempotent(self):
        runtime = FakeRuntime()
        cp = checkpoint()
        directive = {
            "schema_version": "JAYTEC_ASSIGNMENT_CONTROLLER_DIRECTIVE_V1",
            "task_id": cp.task_id,
            "assignment_owner": "CHATGPT_ASSIGNMENT_OWNER",
            "gate_id": "G03",
            "checkpoint_number": cp.checkpoint_number,
            "review_id": "a" * 64,
            "request_id": "b" * 64,
            "jaytec_review_id": "c" * 64,
            "decision": "REDIRECT",
            "direction": "Close the missing alternate-route denial proof.",
            "result_receipt": "receipt",
            "result_sha256": "d" * 64,
            "manifest_sha256": "e" * 64,
            "worker_id": "manus-existing",
            "fencing_token": 4,
        }
        hid = assignment_owner_redirect_handoff_id(cp, 4, directive)
        result = ManusLiteRecoveryInvoker(
            runtime, FakeRegistry()
        ).continue_assignment_owner_directive(
            checkpoint=cp,
            worker_id="manus-existing",
            fencing_token=4,
            directive=directive,
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.worker_id, "manus-existing")
        self.assertEqual(len(runtime.handoffs), 1)
        args, kwargs = runtime.handoffs[0]
        self.assertEqual(args[0], "manus-existing")
        self.assertEqual(kwargs["handoff_id"], hid)
        self.assertIn("owner-redirect:G03:", hid)
        self.assertEqual(
            kwargs["handoff_context"]["kind"],
            "ASSIGNMENT_OWNER_REDIRECT",
        )
        self.assertEqual(
            kwargs["handoff_context"]["direction"],
            directive["direction"],
        )

    def test_gate_handoff_injects_exact_result_receipt_requirement(self):
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
            "instruction": "Work only on G03.",
        }
        receipt = master_gate_handoff_id(cp, 4, context)
        result = ManusLiteRecoveryInvoker(runtime, FakeRegistry()).continue_gate_directive(
            checkpoint=cp,
            worker_id="manus-existing",
            fencing_token=4,
            gate_context=context,
        )
        self.assertTrue(result.accepted)
        _, kwargs = runtime.handoffs[0]
        requirement = kwargs["handoff_context"]["result_receipt_requirement"]
        self.assertEqual(requirement["reference"], receipt)
        self.assertEqual(requirement["kind"], "audit_record")
        self.assertEqual(requirement["source"], "JAYTEC_MASTER_GATE_HANDOFF")
        manifest_requirement = kwargs["handoff_context"]["gate_evidence_manifest_requirement"]
        self.assertEqual(manifest_requirement["kind"], "artifact")
        self.assertEqual(manifest_requirement["source"], "JAYTEC_GATE_EVIDENCE_MANIFEST")
        self.assertIn("/gate_evidence/G03.json", manifest_requirement["reference_format"])

    def test_gate_result_receipt_match_is_exact(self):
        cp = checkpoint()
        context = {
            "gate_id": "G03",
            "graph_sha256": "a" * 64,
            "checkpoint_number": cp.checkpoint_number,
        }
        receipt = master_gate_handoff_id(cp, 4, context)
        good = {
            "evidence": [{
                "kind": "audit_record",
                "source": "JAYTEC_MASTER_GATE_HANDOFF",
                "reference": receipt,
            }]
        }
        stale = {
            "evidence": [{
                "kind": "audit_record",
                "source": "JAYTEC_MASTER_GATE_HANDOFF",
                "reference": receipt + "-stale",
            }]
        }
        self.assertTrue(master_gate_result_has_receipt(good, receipt))
        self.assertFalse(master_gate_result_has_receipt(stale, receipt))

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


    def test_hard_recovery_constraints_explicitly_forbid_notion_agent(self):
        joined = "\n".join(HARD_RECOVERY_CONSTRAINTS)
        self.assertIn("Use GitHub only for this recovery task", joined)
        self.assertIn("do not use Notion Agent", joined)

    def test_fresh_recovery_rejects_notion_scope_before_manus_call(self):
        runtime = FakeRuntime()
        cp = checkpoint({
            "root_owner": "Jay",
            "allowed_actions": ["inspect"],
            "connector_purposes": {"github": "read", "notion": "mcp"},
            "connector_mutation_authorized": False,
        })
        result = ManusLiteRecoveryInvoker(runtime, FakeRegistry()).invoke(
            checkpoint=cp,
            continuation_packet={},
            route=RecoveryRoute.SAME_WORKER_PROVIDER,
            fencing_token=1,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(runtime.requests, [])
        self.assertEqual(runtime.handoffs, [])

    def test_same_worker_handoff_rejects_notion_scope_before_manus_call(self):
        runtime = FakeRuntime()
        cp = checkpoint({
            "root_owner": "Jay",
            "allowed_actions": ["inspect"],
            "connector_purposes": {"github": "read", "notion": "mcp"},
            "connector_mutation_authorized": False,
        })
        result = ManusLiteRecoveryInvoker(runtime, FakeRegistry()).continue_existing(
            checkpoint=cp,
            worker_id="manus-existing",
            fencing_token=4,
            broker_context={"kind": "PRIVATE_REPO_BOOTSTRAP"},
        )
        self.assertFalse(result.accepted)
        self.assertEqual(runtime.requests, [])
        self.assertEqual(runtime.handoffs, [])

    def test_rejected_manus_start_preserves_safe_provider_error(self):
        runtime = FakeRuntime(start={
            "status": "FAILED_CLOSED",
            "error": "MANUS_HTTP_400:invalid_argument",
        })
        result = ManusLiteRecoveryInvoker(runtime, FakeRegistry()).invoke(
            checkpoint=checkpoint(),
            continuation_packet={},
            route=RecoveryRoute.SAME_WORKER_PROVIDER,
            fencing_token=1,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(
            result.detail,
            "MANUS_RECOVERY_NOT_STARTED:FAILED_CLOSED:MANUS_HTTP_400:invalid_argument",
        )

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
