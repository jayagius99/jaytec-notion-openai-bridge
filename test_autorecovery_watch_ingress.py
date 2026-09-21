import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from autorecovery_watch_ingress import (
    FORGE_TASK_ID,
    WATCH_HEARTBEAT_TIMEOUT_SECONDS,
    WatchIngressError,
    _broker_context,
    _master_gate_context,
    _observed_refs,
    _reconcile_known_local_preflight_exhaustion,
    execute_watch_cycle,
)
from manus_governance import specialist_request
from autorecovery_supervisor import (
    AssignmentCheckpoint,
    AssignmentState,
    MemoryAssignmentStore,
    StopReason,
    WorkerKind,
)


def broker_payload(refs):
    value = {
        "schema_version": "JAYTEC_GITHUB_BROKER_CONTEXT_V1",
        "kind": "PRIVATE_REPO_BOOTSTRAP",
        "repo": "jayagius99/jaytec-work-engine-v2-g1",
        "refs": dict(refs),
        "issues": [],
        "pull_requests": [],
        "open_pull_requests": [],
        "authority": "READ_EVIDENCE_ONLY_NO_TOKEN_EXPORT",
    }
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    value["sha256"] = hashlib.sha256(encoded).hexdigest()
    return value


def broker_checkpoint():
    return AssignmentCheckpoint(
        task_id=FORGE_TASK_ID,
        objective="Advance canonical Forge master gate G03: Security Audit #47.",
        current_phase="G03/SECURITY: Security Audit #47",
        completed_work=("watch",),
        remaining_work=("continue",),
        last_safe_checkpoint="saved",
        repo="jayagius99/jaytec-work-engine-v2-g1",
        branch="security/root-owner-control-v1",
        commit_head="b" * 40,
        open_pr=17,
        current_files_state={"dirty": False},
        tests_completed=("watch",),
        known_failures=(),
        active_constraints=("NO FORGE ACTIVATION",),
        authority_envelope={
            "allowed_actions": ["inspect repository"],
            "connector_purposes": {"github": "inspect"},
            "connector_mutation_authorized": False,
        },
        cost_envelope={"paid_fallback": False},
        dependencies=(),
        next_intended_action="Work only on G03 and return exact evidence.",
        worker_specialist_preference=("manus-lite",),
        checkpoint_number=103,
    ).validate()


def legacy_checkpoint():
    checkpoint = broker_checkpoint()
    return AssignmentCheckpoint(
        **{
            **checkpoint.__dict__,
            "objective": (
                "Continue Forge/Genesis preparation from the exact saved state. "
                "Complete every safe software-only preparation, verification, "
                "hardening and evidence step possible without activating Forge."
            ),
            "current_phase": "pre-activation software convergence and evidence hardening",
            "next_intended_action": (
                "Refresh live state, collision-check active work, then advance "
                "the highest-priority safe software-only convergence step."
            ),
            "checkpoint_number": 1,
        }
    ).validate()


def master_gate_payload():
    return {
        "schema_version": "FORGE_MASTER_GATE_DIRECTIVE_V1",
        "gate_id": "G03",
        "phase": "SECURITY",
        "title": "Security Audit #47",
        "status": "IN_PROGRESS",
        "depends_on": ["G02"],
        "evidence": [
            "rule->enforcement->bypass map",
            "alternate-route denial tests",
        ],
        "graph_sha256": "c" * 64,
        "checkpoint_number": 103,
    }


def expected_gate_receipt(fence=9):
    return (
        FORGE_TASK_ID
        + f":fence:{fence}:master-gate:G03:"
        + ("c" * 16)
    )


def gate_receipt_evidence(fence=9):
    return {
        "kind": "audit_record",
        "source": "JAYTEC_MASTER_GATE_HANDOFF",
        "reference": expected_gate_receipt(fence),
        "observed_at": "2026-09-21T05:45:00Z",
        "claim": "Result was produced after the exact G03 handoff.",
        "supports": ["instruction_match_verified"],
    }


def owner_redirect_payload(terminal, **overrides):
    value = {
        "schema_version": "JAYTEC_ASSIGNMENT_CONTROLLER_DIRECTIVE_V1",
        "task_id": FORGE_TASK_ID,
        "assignment_owner": "CHATGPT_ASSIGNMENT_OWNER",
        "gate_id": "G03",
        "checkpoint_number": 103,
        "review_id": "a" * 64,
        "request_id": "b" * 64,
        "jaytec_review_id": "c" * 64,
        "decision": "REDIRECT",
        "direction": "Close the missing alternate-route denial proof and return fresh evidence.",
        "result_receipt": expected_gate_receipt(9),
        "result_sha256": hashlib.sha256(
            json.dumps(
                dict(terminal),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
        "manifest_sha256": "d" * 64,
        "worker_id": "worker-existing",
        "fencing_token": 9,
    }
    value.update(overrides)
    return value


def cycle_payload(refs, *, include_broker=True):
    checkpoint = broker_checkpoint().to_dict()
    if checkpoint["branch"] not in refs:
        branch = next(iter(refs))
        checkpoint["branch"] = branch
        checkpoint["commit_head"] = refs[branch]
    payload = {
        "task_id": FORGE_TASK_ID,
        "observed_refs": dict(refs),
        "bootstrap_checkpoint": checkpoint,
        "master_gate": master_gate_payload(),
    }
    if include_broker:
        payload["github_broker_context"] = broker_payload(refs)
    return payload


def broker_specialist_request(context, *, specialist="github_broker"):
    return specialist_request(
        parent_task_id=FORGE_TASK_ID + ":recovery:2:fresh_worker_same_checkpoint",
        specialist=specialist,
        objective="Perform one bounded GitHub broker operation.",
        reason="Private repository evidence is required.",
        required_context=context,
    )


class FakeBrokerStore:
    def __init__(self):
        self.state = AssignmentState(
            task_id=FORGE_TASK_ID,
            checkpoint=broker_checkpoint(),
            stop_reason=StopReason.WAITING_FOR_DEPENDENCY,
            worker_kind=WorkerKind.JAYTEC_CALLABLE,
            worker_id="worker-existing",
            worker_route="jaytec-manus-lite-v1",
            last_heartbeat_at=None,
            last_progress_at=None,
            progress_marker="MANUS_TERMINAL:NEEDS_JAYTEC:deadbeef",
            recovery_attempts=2,
            fencing_token=9,
            lease_owner=None,
            lease_expires_at=None,
            completed=False,
            last_error="MANUS_TERMINAL:NEEDS_JAYTEC:deadbeef",
            updated_at=__import__("datetime").datetime(
                2026, 9, 20, 21, 0,
                tzinfo=__import__("datetime").timezone.utc,
            ),
        )
        self.heartbeats = []
        self.stops = []

    def get(self, task_id):
        return self.state if task_id == FORGE_TASK_ID else None

    def advance_checkpoint_preserving_runtime(
        self,
        checkpoint,
        *,
        expected_current_checkpoint_number,
        now=None,
    ):
        assert checkpoint.task_id == FORGE_TASK_ID
        assert self.state.checkpoint.checkpoint_number == expected_current_checkpoint_number
        if checkpoint.checkpoint_number <= self.state.checkpoint.checkpoint_number:
            return False
        current = now or self.state.updated_at
        self.state = AssignmentState(
            **{
                **self.state.__dict__,
                "checkpoint": checkpoint,
                "updated_at": current,
            }
        )
        return True

    def heartbeat(self, task_id, *, fencing_token, worker_id, progress_marker=None, now=None):
        assert task_id == FORGE_TASK_ID
        assert fencing_token == self.state.fencing_token
        assert worker_id == self.state.worker_id
        self.heartbeats.append(progress_marker)
        current = now or self.state.updated_at
        self.state = AssignmentState(
            **{
                **self.state.__dict__,
                "stop_reason": StopReason.RUNNING,
                "last_heartbeat_at": current,
                "last_progress_at": current if progress_marker else self.state.last_progress_at,
                "progress_marker": progress_marker or self.state.progress_marker,
                "recovery_attempts": 0 if progress_marker is not None else self.state.recovery_attempts,
                "last_error": None,
                "updated_at": current,
            }
        )

    def mark_stop(self, task_id, *, fencing_token, stop_reason, error=None, now=None):
        assert task_id == FORGE_TASK_ID
        assert fencing_token == self.state.fencing_token
        self.stops.append((stop_reason, error))
        current = now or self.state.updated_at
        self.state = AssignmentState(
            **{
                **self.state.__dict__,
                "stop_reason": stop_reason,
                "completed": stop_reason is StopReason.COMPLETED,
                "last_error": error,
                "updated_at": current,
            }
        )


class FakeBrokerRuntime:
    def __init__(self):
        self.handoffs = []

    def task_status_readonly(self, worker_id):
        assert worker_id == "worker-existing"
        return {
            "status": "VERIFIED_COMPLETE",
            "result": {
                "status": "NEEDS_JAYTEC",
                "summary": "Need private repo evidence.",
                "specialist_requests": [],
            },
        }

    def continue_task_handoff(self, worker_id, **kwargs):
        self.handoffs.append((worker_id, dict(kwargs)))
        return {
            "status": "CONTINUED",
            "provider_task_id": worker_id,
            "requested_profile": "lite",
            "observed_profile_verified": True,
        }



class FakePendingBrokerRuntime(FakeBrokerRuntime):
    def task_status_readonly(self, worker_id):
        assert worker_id == "worker-existing"
        return {
            "status": "PENDING",
            "provider_task_id": worker_id,
            "requested_profile": "lite",
            "observed_profile": "lite",
        }

    def task_status(self, worker_id):
        return self.task_status_readonly(worker_id)


class FakeSuccessBrokerRuntime(FakeBrokerRuntime):
    def task_status_readonly(self, worker_id):
        assert worker_id == "worker-existing"
        return {
            "status": "VERIFIED_COMPLETE",
            "result": {
                "status": "SUCCESS",
                "summary": "Handoff work completed.",
                "evidence": [gate_receipt_evidence()],
                "changes_made": [],
                "unresolved_items": [],
                "specialist_requests": [],
                "verification": {
                    "instruction_match_verified": True,
                    "scope_verified": True,
                    "evidence_verified": True,
                    "no_unauthorized_side_effects": True,
                    "duplicate_work_check_passed": True,
                },
            },
        }

    def task_status(self, worker_id):
        return self.task_status_readonly(worker_id)


class FakeTransientStatusFailureRuntime(FakeBrokerRuntime):
    def task_status(self, worker_id):
        assert worker_id == "worker-existing"
        raise RuntimeError("transient provider read failure")


class WatchIngressPolicyTests(unittest.TestCase):
    def test_only_canonical_forge_task_is_allowed_by_constant(self):
        self.assertEqual(FORGE_TASK_ID, "FORGE-GENESIS-ACTIVATION-001")

    def test_observed_refs_are_bounded_and_nonempty(self):
        with self.assertRaisesRegex(WatchIngressError, "REQUIRED"):
            _observed_refs({})
        refs = _observed_refs({"main": "a" * 40})
        self.assertEqual(refs["main"], "a" * 40)

    def test_ref_count_is_bounded(self):
        with self.assertRaisesRegex(WatchIngressError, "TOO_MANY"):
            _observed_refs({f"r-{i}": "a" * 40 for i in range(129)})


    def test_master_gate_must_match_checkpoint(self):
        checkpoint = broker_checkpoint()
        gate = _master_gate_context(master_gate_payload(), checkpoint)
        self.assertEqual(gate["gate_id"], "G03")

        bad_number = master_gate_payload()
        bad_number["checkpoint_number"] = 0
        with self.assertRaisesRegex(WatchIngressError, "CHECKPOINT_NUMBER_INVALID"):
            _master_gate_context(bad_number, checkpoint)

        mismatch = master_gate_payload()
        mismatch["checkpoint_number"] = 104
        with self.assertRaisesRegex(WatchIngressError, "CHECKPOINT_MISMATCH"):
            _master_gate_context(mismatch, checkpoint)

        dynamic_checkpoint = AssignmentCheckpoint(
            **{
                **checkpoint.__dict__,
                "objective": "Advance canonical Forge master gate G07: dynamic.",
                "current_phase": "G07/COGNITION: dynamic",
                "checkpoint_number": 104,
            }
        ).validate()
        dynamic_gate = master_gate_payload()
        dynamic_gate.update(
            {
                "gate_id": "G07",
                "phase": "COGNITION",
                "title": "dynamic",
                "depends_on": ["G04"],
                "checkpoint_number": 104,
            }
        )
        parsed = _master_gate_context(dynamic_gate, dynamic_checkpoint)
        self.assertEqual(parsed["gate_id"], "G07")
        self.assertEqual(parsed["checkpoint_number"], 104)

    def test_broker_context_digest_and_secret_fields_fail_closed(self):
        refs = {"security/root-owner-control-v1": "b" * 40}
        good = broker_payload(refs)
        self.assertEqual(_broker_context(good, refs)["repo"], good["repo"])

        bad_digest = dict(good)
        bad_digest["sha256"] = "0" * 64
        with self.assertRaisesRegex(WatchIngressError, "DIGEST_MISMATCH"):
            _broker_context(bad_digest, refs)

        secret = dict(good)
        secret["issues"] = [{"authorization": "Bearer must-not-pass"}]
        unsigned = {k: v for k, v in secret.items() if k != "sha256"}
        secret["sha256"] = hashlib.sha256(
            json.dumps(
                unsigned,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(WatchIngressError, "SECRET_FIELD_FORBIDDEN"):
            _broker_context(secret, refs)


        secret_value = dict(good)
        secret_value["issues"] = [{"body_excerpt": "token=supersecret12345678"}]
        unsigned = {k: v for k, v in secret_value.items() if k != "sha256"}
        secret_value["sha256"] = hashlib.sha256(
            json.dumps(
                unsigned,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(WatchIngressError, "SECRET_FIELD_FORBIDDEN"):
            _broker_context(secret_value, refs)

    def test_exact_g03_exhausted_local_preflight_is_refunded_once(self):
        cp = broker_checkpoint()
        st = AssignmentState(
            task_id=FORGE_TASK_ID,
            checkpoint=cp,
            stop_reason=StopReason.RECOVERY_EXHAUSTED,
            worker_kind=WorkerKind.JAYTEC_CALLABLE,
            worker_id="HtTFgzhr8sXKbJmQWn2PEA",
            worker_route="jaytec-manus-lite-v1",
            last_heartbeat_at=None,
            last_progress_at=None,
            progress_marker="MANUS_PENDING",
            recovery_attempts=3,
            fencing_token=11,
            lease_owner=None,
            lease_expires_at=None,
            completed=False,
            last_error=(
                "RECOVERY_INVOCATION_REJECTED:"
                "MANUS_RECOVERY_PREFLIGHT_MESSAGE_TOO_LARGE"
            ),
            updated_at=datetime(2026, 9, 21, 10, 6, tzinfo=timezone.utc),
        )
        store = MemoryAssignmentStore(st)
        repaired, refunded = _reconcile_known_local_preflight_exhaustion(
            store,
            store.get(FORGE_TASK_ID),
            cp,
            master_gate_payload(),
        )
        self.assertTrue(refunded)
        self.assertEqual(repaired.recovery_attempts, 2)
        self.assertEqual(repaired.fencing_token, 11)
        self.assertEqual(repaired.stop_reason, StopReason.TRANSIENT_PROVIDER_FAILURE)
        self.assertEqual(repaired.worker_id, "HtTFgzhr8sXKbJmQWn2PEA")
        self.assertEqual(repaired.checkpoint.checkpoint_number, 103)

        replayed, refunded_again = _reconcile_known_local_preflight_exhaustion(
            store,
            store.get(FORGE_TASK_ID),
            cp,
            master_gate_payload(),
        )
        self.assertFalse(refunded_again)
        self.assertEqual(replayed.recovery_attempts, 2)
        self.assertEqual(replayed.fencing_token, 11)

    def test_local_preflight_refund_refuses_any_state_drift(self):
        cp = broker_checkpoint()
        base = AssignmentState(
            task_id=FORGE_TASK_ID,
            checkpoint=cp,
            stop_reason=StopReason.RECOVERY_EXHAUSTED,
            worker_kind=WorkerKind.JAYTEC_CALLABLE,
            worker_id="HtTFgzhr8sXKbJmQWn2PEA",
            worker_route="jaytec-manus-lite-v1",
            last_heartbeat_at=None,
            last_progress_at=None,
            progress_marker="MANUS_PENDING",
            recovery_attempts=3,
            fencing_token=11,
            lease_owner=None,
            lease_expires_at=None,
            completed=False,
            last_error=(
                "RECOVERY_INVOCATION_REJECTED:"
                "MANUS_RECOVERY_PREFLIGHT_MESSAGE_TOO_LARGE"
            ),
            updated_at=datetime(2026, 9, 21, 10, 6, tzinfo=timezone.utc),
        )
        for field, value in (
            ("fencing_token", 12),
            ("recovery_attempts", 2),
            ("last_error", "RECOVERY_INVOCATION_REJECTED:OTHER"),
            ("worker_id", ""),
            ("stop_reason", StopReason.TRANSIENT_PROVIDER_FAILURE),
        ):
            with self.subTest(field=field):
                drifted = AssignmentState(**{**base.__dict__, field: value})
                store = MemoryAssignmentStore(drifted)
                observed, refunded = _reconcile_known_local_preflight_exhaustion(
                    store,
                    store.get(FORGE_TASK_ID),
                    cp,
                    master_gate_payload(),
                )
                self.assertFalse(refunded)
                self.assertEqual(observed.fencing_token, drifted.fencing_token)
                self.assertEqual(observed.recovery_attempts, drifted.recovery_attempts)
                self.assertEqual(observed.stop_reason, drifted.stop_reason)

    def test_needs_jaytec_broker_handoff_keeps_same_worker_and_fence(self):
        refs = {"security/root-owner-control-v1": "b" * 40}
        store = FakeBrokerStore()
        runtime = FakeBrokerRuntime()
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
            result = execute_watch_cycle(
                cycle_payload(refs),
                database_url="postgresql://unused",
                manus_runtime=runtime,
                registry=object(),
                runtime_components_registered=True,
                env={},
            )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["github_broker"], "HANDOFF_CONTINUED")
        self.assertEqual(result["decision"]["reason"], "JAYTEC_INTERNAL_HANDOFF_CONTINUED")
        self.assertEqual(result["assignment"]["worker_id"], "worker-existing")
        self.assertEqual(result["assignment"]["fencing_token"], 9)
        self.assertEqual(result["assignment"]["recovery_attempts"], 0)
        self.assertEqual(result["assignment"]["stop_reason"], "RUNNING")
        self.assertEqual(len(runtime.handoffs), 1)
        self.assertEqual(store.state.worker_id, "worker-existing")
        self.assertEqual(store.state.fencing_token, 9)
        self.assertEqual(len(store.heartbeats), 1)


    def test_post_handoff_pending_restores_running_without_duplicate_handoff(self):
        refs = {"security/root-owner-control-v1": "b" * 40}
        store = FakeBrokerStore()
        store.state = AssignmentState(
            **{
                **store.state.__dict__,
                "progress_marker": "JAYTEC_BROKER_HANDOFF:already-delivered",
                "recovery_attempts": 0,
            }
        )
        runtime = FakePendingBrokerRuntime()
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
            result = execute_watch_cycle(
                cycle_payload(refs),
                database_url="postgresql://unused",
                manus_runtime=runtime,
                registry=object(),
                runtime_components_registered=True,
                env={},
            )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["github_broker"], "WORKER_RESUMED_PENDING")
        self.assertEqual(result["decision"]["action"], "NOOP_HEALTHY")
        self.assertEqual(result["assignment"]["worker_id"], "worker-existing")
        self.assertEqual(result["assignment"]["fencing_token"], 9)
        self.assertEqual(result["assignment"]["recovery_attempts"], 0)
        self.assertEqual(result["assignment"]["stop_reason"], "RUNNING")
        self.assertEqual(runtime.handoffs, [])
        self.assertEqual(result["gate_handoff"], "NOT_NEEDED")
        self.assertEqual(store.stops, [])

    def test_stale_success_without_exact_gate_receipt_cannot_complete_gate(self):
        class StaleSuccessRuntime(FakeSuccessBrokerRuntime):
            def task_status_readonly(self, worker_id):
                value = super().task_status_readonly(worker_id)
                value["result"]["evidence"] = []
                return value

            def task_status(self, worker_id):
                return self.task_status_readonly(worker_id)

        refs = {"security/root-owner-control-v1": "b" * 40}
        store = FakeBrokerStore()
        store.state = AssignmentState(
            **{
                **store.state.__dict__,
                "stop_reason": StopReason.RUNNING,
                "last_error": None,
                "progress_marker": "MANUS_PENDING",
            }
        )
        runtime = StaleSuccessRuntime()
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
            result = execute_watch_cycle(
                cycle_payload(refs),
                database_url="postgresql://unused",
                manus_runtime=runtime,
                registry=object(),
                runtime_components_registered=True,
                env={},
            )
        self.assertEqual(result["status"], "PASS")
        self.assertNotEqual(
            result["decision"].get("reason"),
            "MASTER_GATE_RESULT_READY",
        )
        self.assertFalse(result["assignment"]["completed"])
        self.assertIn(
            result["assignment"]["progress_marker"],
            {"MANUS_PENDING_GATE_RECEIPT", "MANUS_PENDING"},
        )

    def test_post_handoff_new_success_terminal_uses_canonical_terminal_policy(self):
        refs = {"security/root-owner-control-v1": "b" * 40}
        store = FakeBrokerStore()
        store.state = AssignmentState(
            **{
                **store.state.__dict__,
                "progress_marker": "JAYTEC_BROKER_HANDOFF:already-delivered",
                "recovery_attempts": 0,
            }
        )
        runtime = FakeSuccessBrokerRuntime()
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
            result = execute_watch_cycle(
                cycle_payload(refs),
                database_url="postgresql://unused",
                manus_runtime=runtime,
                registry=object(),
                runtime_components_registered=True,
                env={},
            )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["github_broker"], "WORKER_TERMINAL_ADVANCED")
        self.assertEqual(result["decision"]["action"], "HOLD")
        self.assertEqual(result["decision"]["reason"], "MASTER_GATE_RESULT_READY")
        self.assertEqual(result["assignment"]["stop_reason"], "WAITING_FOR_DEPENDENCY")
        self.assertFalse(result["assignment"]["completed"])
        self.assertEqual(result["assignment"]["worker_id"], "worker-existing")
        self.assertEqual(result["assignment"]["fencing_token"], 9)
        self.assertEqual(runtime.handoffs, [])
        self.assertEqual(result["gate_handoff"], "NOT_NEEDED")
        self.assertEqual(result["gate_result"]["status"], "SUCCESS")



    def test_assignment_owner_redirect_resumes_same_worker_without_recovery_attempt(self):
        refs = {"security/root-owner-control-v1": "b" * 40}
        store = FakeBrokerStore()
        store.state = AssignmentState(
            **{
                **store.state.__dict__,
                "stop_reason": StopReason.WAITING_FOR_DEPENDENCY,
                "last_error": "MANUS_TERMINAL:SUCCESS:verified",
                "progress_marker": "MANUS_TERMINAL:SUCCESS:verified",
                "recovery_attempts": 2,
            }
        )
        runtime = FakeSuccessBrokerRuntime()
        terminal = runtime.task_status_readonly("worker-existing")["result"]
        payload = cycle_payload(refs)
        payload["assignment_owner_directive"] = owner_redirect_payload(terminal)
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
            result = execute_watch_cycle(
                payload,
                database_url="postgresql://unused",
                manus_runtime=runtime,
                registry=object(),
                runtime_components_registered=True,
                env={},
            )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(
            result["decision"]["reason"],
            "ASSIGNMENT_OWNER_REDIRECT_CONTINUED",
        )
        self.assertEqual(result["gate_handoff"], "OWNER_REDIRECT_CONTINUED")
        self.assertEqual(result["assignment"]["worker_id"], "worker-existing")
        self.assertEqual(result["assignment"]["fencing_token"], 9)
        self.assertEqual(result["assignment"]["recovery_attempts"], 0)
        self.assertEqual(result["assignment"]["stop_reason"], "RUNNING")
        self.assertTrue(
            result["assignment"]["progress_marker"].startswith(
                "ASSIGNMENT_OWNER_REDIRECT:"
            )
        )
        self.assertEqual(len(runtime.handoffs), 1)
        worker, kwargs = runtime.handoffs[0]
        self.assertEqual(worker, "worker-existing")
        self.assertIn("owner-redirect:G03:", kwargs["handoff_id"])
        self.assertEqual(
            kwargs["handoff_context"]["decision"],
            "REDIRECT",
        )

    def test_assignment_owner_redirect_wrong_result_hash_fails_before_handoff(self):
        refs = {"security/root-owner-control-v1": "b" * 40}
        store = FakeBrokerStore()
        store.state = AssignmentState(
            **{
                **store.state.__dict__,
                "stop_reason": StopReason.WAITING_FOR_DEPENDENCY,
                "last_error": "MANUS_TERMINAL:SUCCESS:verified",
                "progress_marker": "MANUS_TERMINAL:SUCCESS:verified",
                "recovery_attempts": 0,
            }
        )
        runtime = FakeSuccessBrokerRuntime()
        terminal = runtime.task_status_readonly("worker-existing")["result"]
        payload = cycle_payload(refs)
        payload["assignment_owner_directive"] = owner_redirect_payload(
            terminal,
            result_sha256="f" * 64,
        )
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
            result = execute_watch_cycle(
                payload,
                database_url="postgresql://unused",
                manus_runtime=runtime,
                registry=object(),
                runtime_components_registered=True,
                env={},
            )
        self.assertEqual(result["status"], "BLOCKED_FAIL_CLOSED")
        self.assertEqual(
            result["reason"],
            "ASSIGNMENT_OWNER_DIRECTIVE_RESULT_MISMATCH",
        )
        self.assertEqual(runtime.handoffs, [])

    def test_assignment_owner_redirect_wrong_fence_fails_before_handoff(self):
        refs = {"security/root-owner-control-v1": "b" * 40}
        store = FakeBrokerStore()
        store.state = AssignmentState(
            **{
                **store.state.__dict__,
                "stop_reason": StopReason.WAITING_FOR_DEPENDENCY,
                "last_error": "MANUS_TERMINAL:SUCCESS:verified",
                "progress_marker": "MANUS_TERMINAL:SUCCESS:verified",
                "recovery_attempts": 0,
            }
        )
        runtime = FakeSuccessBrokerRuntime()
        terminal = runtime.task_status_readonly("worker-existing")["result"]
        payload = cycle_payload(refs)
        payload["assignment_owner_directive"] = owner_redirect_payload(
            terminal,
            fencing_token=8,
        )
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
            result = execute_watch_cycle(
                payload,
                database_url="postgresql://unused",
                manus_runtime=runtime,
                registry=object(),
                runtime_components_registered=True,
                env={},
            )
        self.assertEqual(result["status"], "BLOCKED_FAIL_CLOSED")
        self.assertEqual(
            result["reason"],
            "ASSIGNMENT_OWNER_DIRECTIVE_FENCE_MISMATCH",
        )
        self.assertEqual(runtime.handoffs, [])


    def test_exact_legacy_checkpoint_migrates_once_to_first_master_gate(self):
        refs = {"security/root-owner-control-v1": "b" * 40}
        store = FakeBrokerStore()
        store.state = AssignmentState(
            **{
                **store.state.__dict__,
                "checkpoint": legacy_checkpoint(),
                "stop_reason": StopReason.RUNNING,
                "progress_marker": "MANUS_PENDING",
                "recovery_attempts": 0,
                "completed": False,
                "last_error": None,
                "lease_owner": None,
                "lease_expires_at": None,
            }
        )
        runtime = FakePendingBrokerRuntime()
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
            result = execute_watch_cycle(
                cycle_payload(refs),
                database_url="postgresql://unused",
                manus_runtime=runtime,
                registry=object(),
                runtime_components_registered=True,
                env={},
            )

        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["checkpoint_advanced"])
        self.assertEqual(result["assignment"]["checkpoint_number"], 103)
        self.assertEqual(result["assignment"]["worker_id"], "worker-existing")
        self.assertEqual(result["assignment"]["fencing_token"], 9)
        self.assertEqual(result["assignment"]["recovery_attempts"], 0)
        self.assertIn("MASTER_GATE_NAMESPACE_MIGRATED:G03:103", store.heartbeats)
        self.assertEqual(len(runtime.handoffs), 1)
        self.assertIn("master-gate:G03:", runtime.handoffs[0][1]["handoff_id"])

    def test_legacy_namespace_migration_is_exact_state_only(self):
        refs = {"security/root-owner-control-v1": "b" * 40}
        store = FakeBrokerStore()
        store.state = AssignmentState(
            **{
                **store.state.__dict__,
                "checkpoint": legacy_checkpoint(),
                "stop_reason": StopReason.RUNNING,
                "progress_marker": "OTHER_LEGACY_STATE",
                "recovery_attempts": 0,
                "completed": False,
                "last_error": None,
                "lease_owner": None,
                "lease_expires_at": None,
            }
        )
        runtime = FakePendingBrokerRuntime()
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
            result = execute_watch_cycle(
                cycle_payload(refs),
                database_url="postgresql://unused",
                manus_runtime=runtime,
                registry=object(),
                runtime_components_registered=True,
                env={},
            )

        self.assertEqual(result["status"], "BLOCKED_FAIL_CLOSED")
        self.assertEqual(result["reason"], "MASTER_GATE_CHECKPOINT_GAP_FORBIDDEN")
        self.assertEqual(store.state.checkpoint.checkpoint_number, 1)
        self.assertEqual(runtime.handoffs, [])

    def test_checkpoint_advance_requires_verified_prior_gate_success(self):
        refs = {"security/root-owner-control-v1": "b" * 40}
        store = FakeBrokerStore()
        runtime = FakePendingBrokerRuntime()
        active = SimpleNamespace(
            active=True,
            callable_worker_routes=("jaytec-manus-lite-v1",),
            to_dict=lambda: {},
        )
        next_checkpoint = AssignmentCheckpoint(
            **{
                **broker_checkpoint().__dict__,
                "objective": "Advance canonical Forge master gate G04: next.",
                "current_phase": "G04/CONTROL: next",
                "next_intended_action": "Work only on G04.",
                "checkpoint_number": 104,
            }
        ).validate()
        next_gate = master_gate_payload()
        next_gate.update(
            {
                "gate_id": "G04",
                "phase": "CONTROL",
                "title": "next",
                "depends_on": ["G03"],
                "checkpoint_number": 104,
            }
        )
        payload = cycle_payload(refs)
        payload["bootstrap_checkpoint"] = next_checkpoint.to_dict()
        payload["master_gate"] = next_gate

        with (
            patch(
                "autorecovery_watch_ingress.prepare_schema_if_authorized",
                return_value={"status": "PASS", "schema_present": True},
            ),
            patch("autorecovery_watch_ingress.runtime_status", return_value=active),
            patch("autorecovery_watch_ingress.PostgresAssignmentStore", return_value=store),
        ):
            result = execute_watch_cycle(
                payload,
                database_url="postgresql://unused",
                manus_runtime=runtime,
                registry=object(),
                runtime_components_registered=True,
                env={},
            )

        self.assertEqual(result["status"], "BLOCKED_FAIL_CLOSED")
        self.assertEqual(
            result["reason"],
            "MASTER_GATE_ADVANCE_REQUIRES_VERIFIED_PRIOR_SUCCESS",
        )
        self.assertEqual(store.state.checkpoint.checkpoint_number, 103)
        self.assertEqual(runtime.handoffs, [])

    def test_verified_gate_success_advances_one_checkpoint_and_reuses_worker(self):
        refs = {"security/root-owner-control-v1": "b" * 40}
        store = FakeBrokerStore()
        store.state = AssignmentState(
            **{
                **store.state.__dict__,
                "stop_reason": StopReason.WAITING_FOR_DEPENDENCY,
                "last_error": "MANUS_TERMINAL:SUCCESS:verified",
                "progress_marker": "MANUS_TERMINAL:SUCCESS:verified",
                "recovery_attempts": 0,
            }
        )
        runtime = FakePendingBrokerRuntime()
        active = SimpleNamespace(
            active=True,
            callable_worker_routes=("jaytec-manus-lite-v1",),
            to_dict=lambda: {},
        )
        next_checkpoint = AssignmentCheckpoint(
            **{
                **broker_checkpoint().__dict__,
                "objective": "Advance canonical Forge master gate G04: next.",
                "current_phase": "G04/CONTROL: next",
                "next_intended_action": "Work only on G04.",
                "checkpoint_number": 104,
            }
        ).validate()
        next_gate = master_gate_payload()
        next_gate.update(
            {
                "gate_id": "G04",
                "phase": "CONTROL",
                "title": "next",
                "depends_on": ["G03"],
                "checkpoint_number": 104,
            }
        )
        payload = cycle_payload(refs)
        payload["bootstrap_checkpoint"] = next_checkpoint.to_dict()
        payload["master_gate"] = next_gate

        with (
            patch(
                "autorecovery_watch_ingress.prepare_schema_if_authorized",
                return_value={"status": "PASS", "schema_present": True},
            ),
            patch("autorecovery_watch_ingress.runtime_status", return_value=active),
            patch("autorecovery_watch_ingress.PostgresAssignmentStore", return_value=store),
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
        self.assertTrue(result["checkpoint_advanced"])
        self.assertEqual(result["assignment"]["checkpoint_number"], 104)
        self.assertEqual(result["assignment"]["worker_id"], "worker-existing")
        self.assertEqual(result["assignment"]["fencing_token"], 9)
        self.assertEqual(result["assignment"]["recovery_attempts"], 0)
        self.assertEqual(len(runtime.handoffs), 1)
        self.assertIn("master-gate:G04:", runtime.handoffs[0][1]["handoff_id"])
        self.assertEqual(result["decision"]["action"], "NOOP_HEALTHY")

    def test_checkpoint_gap_is_rejected_even_after_prior_success(self):
        refs = {"security/root-owner-control-v1": "b" * 40}
        store = FakeBrokerStore()
        store.state = AssignmentState(
            **{
                **store.state.__dict__,
                "stop_reason": StopReason.WAITING_FOR_DEPENDENCY,
                "last_error": "MANUS_TERMINAL:SUCCESS:verified",
                "progress_marker": "MANUS_TERMINAL:SUCCESS:verified",
                "recovery_attempts": 0,
            }
        )
        runtime = FakePendingBrokerRuntime()
        active = SimpleNamespace(
            active=True,
            callable_worker_routes=("jaytec-manus-lite-v1",),
            to_dict=lambda: {},
        )
        gap_checkpoint = AssignmentCheckpoint(
            **{
                **broker_checkpoint().__dict__,
                "objective": "Advance canonical Forge master gate G07: gap.",
                "current_phase": "G07/COGNITION: gap",
                "next_intended_action": "Work only on G07.",
                "checkpoint_number": 105,
            }
        ).validate()
        gap_gate = master_gate_payload()
        gap_gate.update(
            {
                "gate_id": "G07",
                "phase": "COGNITION",
                "title": "gap",
                "depends_on": ["G04"],
                "checkpoint_number": 105,
            }
        )
        payload = cycle_payload(refs)
        payload["bootstrap_checkpoint"] = gap_checkpoint.to_dict()
        payload["master_gate"] = gap_gate

        with (
            patch(
                "autorecovery_watch_ingress.prepare_schema_if_authorized",
                return_value={"status": "PASS", "schema_present": True},
            ),
            patch("autorecovery_watch_ingress.runtime_status", return_value=active),
            patch("autorecovery_watch_ingress.PostgresAssignmentStore", return_value=store),
        ):
            result = execute_watch_cycle(
                payload,
                database_url="postgresql://unused",
                manus_runtime=runtime,
                registry=object(),
                runtime_components_registered=True,
                env={},
            )

        self.assertEqual(result["status"], "BLOCKED_FAIL_CLOSED")
        self.assertEqual(result["reason"], "MASTER_GATE_CHECKPOINT_GAP_FORBIDDEN")
        self.assertEqual(store.state.checkpoint.checkpoint_number, 103)
        self.assertEqual(runtime.handoffs, [])


    def test_one_transient_health_probe_failure_does_not_consume_recovery(self):
        self.assertGreater(WATCH_HEARTBEAT_TIMEOUT_SECONDS, 30 * 60)
        refs = {"security/root-owner-control-v1": "b" * 40}
        store = FakeBrokerStore()
        recent = datetime.now(timezone.utc) - timedelta(minutes=16)
        store.state = AssignmentState(
            **{
                **store.state.__dict__,
                "stop_reason": StopReason.RUNNING,
                "last_heartbeat_at": recent,
                "last_progress_at": recent,
                "progress_marker": "JAYTEC_BROKER_HANDOFF:already-delivered",
                "recovery_attempts": 0,
                "last_error": None,
                "updated_at": recent,
            }
        )
        runtime = FakeTransientStatusFailureRuntime()
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
            result = execute_watch_cycle(
                cycle_payload(refs),
                database_url="postgresql://unused",
                manus_runtime=runtime,
                registry=object(),
                runtime_components_registered=True,
                env={},
            )

        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["health_refreshed"])
        self.assertEqual(result["decision"]["action"], "NOOP_HEALTHY")
        self.assertEqual(result["decision"]["reason"], "HEALTHY_WORKER_HEARTBEAT")
        self.assertEqual(result["assignment"]["worker_id"], "worker-existing")
        self.assertEqual(result["assignment"]["fencing_token"], 9)
        self.assertEqual(result["assignment"]["recovery_attempts"], 0)
        self.assertEqual(result["assignment"]["stop_reason"], "RUNNING")
        self.assertEqual(store.stops, [])



    def test_broker_request_contract_rejects_unsupported_and_stale_mutations(self):
        from autorecovery_watch_ingress import (
            _normalize_broker_operation,
            _broker_results_match_requests,
        )

        refs = {
            "main": "a" * 40,
            "security/root-owner-control-v1": "b" * 40,
            "genesis/two-console-v1": "c" * 40,
        }

        valid_read = broker_specialist_request(
            {
                "operation": "read_file",
                "path": "src/example.py",
                "ref": "main",
                "start_line": 1,
                "end_line": 20,
            }
        )
        normalized = _normalize_broker_operation(
            valid_read,
            refs=refs,
            fencing_token=9,
            mutation_authorized=False,
        )
        self.assertEqual(normalized["operation"], "read_file")
        self.assertEqual(normalized["args"]["ref"], "main")

        bad_op = broker_specialist_request(
            {"operation": "merge_pr", "number": 17}
        )
        with self.assertRaisesRegex(WatchIngressError, "OPERATION_INVALID"):
            _normalize_broker_operation(
                bad_op,
                refs=refs,
                fencing_token=9,
                mutation_authorized=True,
            )

        stale_branch = broker_specialist_request(
            {
                "operation": "create_branch",
                "base_ref": "main",
                "new_branch": "watch/worker-8-stale",
            }
        )
        with self.assertRaisesRegex(WatchIngressError, "NEW_BRANCH_INVALID"):
            _normalize_broker_operation(
                stale_branch,
                refs=refs,
                fencing_token=9,
                mutation_authorized=True,
            )

        protected_write = broker_specialist_request(
            {
                "operation": "write_file",
                "branch": "main",
                "path": "safe.py",
                "content": "print('x')",
            }
        )
        with self.assertRaises(WatchIngressError):
            _normalize_broker_operation(
                protected_write,
                refs=refs,
                fencing_token=9,
                mutation_authorized=True,
            )

        request = {
            "request_id": "r1",
            "operation": "read_issue",
            "args": {"number": 59},
        }
        self.assertTrue(
            _broker_results_match_requests(
                {
                    "kind": "SPECIALIST_REQUEST_RESULTS",
                    "request_results": [
                        {
                            "request_id": "r1",
                            "operation": "read_issue",
                            "status": "SUCCESS",
                            "evidence": {},
                        }
                    ],
                },
                [request],
            )
        )
        self.assertFalse(
            _broker_results_match_requests(
                {
                    "kind": "SPECIALIST_REQUEST_RESULTS",
                    "request_results": [
                        {
                            "request_id": "different",
                            "operation": "read_issue",
                            "status": "SUCCESS",
                            "evidence": {},
                        }
                    ],
                },
                [request],
            )
        )

    def test_existing_checkpoint_must_be_re_attested_before_handoff(self):
        refs = {"security/root-owner-control-v1": "d" * 40}
        store = FakeBrokerStore()
        runtime = FakeBrokerRuntime()
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
            result = execute_watch_cycle(
                cycle_payload(refs),
                database_url="postgresql://unused",
                manus_runtime=runtime,
                registry=object(),
                runtime_components_registered=True,
                env={},
            )
        self.assertEqual(result["status"], "BLOCKED_FAIL_CLOSED")
        self.assertEqual(result["reason"], "CURRENT_CHECKPOINT_NOT_ATTESTED")
        self.assertEqual(runtime.handoffs, [])

    def test_disabled_runtime_does_not_require_manus_components(self):
        with patch(
            "autorecovery_watch_ingress.prepare_schema_if_authorized",
            return_value={"status": "PASS", "schema_present": True},
        ):
            result = execute_watch_cycle(
                cycle_payload({"main": "a" * 40}, include_broker=False),
                database_url="postgresql://placeholder/not-contacted",
                manus_runtime=None,
                registry=object(),
                runtime_components_registered=False,
                env={"JAYTEC_AUTORECOVERY_ENABLED": "0"},
            )
        self.assertEqual(result["status"], "BLOCKED_FAIL_CLOSED")
        self.assertEqual(result["reason"], "AUTORECOVERY_RUNTIME_NOT_ACTIVE")

    def test_requested_runtime_fails_closed_when_components_missing(self):
        with patch(
            "autorecovery_watch_ingress.prepare_schema_if_authorized",
            return_value={"status": "PASS", "schema_present": True},
        ):
            result = execute_watch_cycle(
                cycle_payload({"main": "a" * 40}, include_broker=False),
                database_url="postgresql://placeholder/not-contacted",
                manus_runtime=None,
                registry=object(),
                runtime_components_registered=False,
                env={
                    "JAYTEC_AUTORECOVERY_ENABLED": "1",
                    "JAYTEC_AUTORECOVERY_SCHEMA_READY": "1",
                    "JAYTEC_AUTORECOVERY_CALLABLE_ROUTES": '["jaytec-manus-lite-v1"]',
                    "JAYTEC_AUTORECOVERY_CHECKPOINT_VERIFIER": "github_exact_head_v1",
                    "JAYTEC_AUTORECOVERY_HEARTBEAT_MODE": "fenced_postgres_v1",
                    "JAYTEC_AUTORECOVERY_NOTIFICATION_MODE": "event_log_v1",
                },
            )
        self.assertEqual(result["status"], "BLOCKED_FAIL_CLOSED")
        self.assertEqual(result["reason"], "AUTORECOVERY_RUNTIME_NOT_ACTIVE")
        self.assertIn(
            "CALLABLE_RUNTIME_COMPONENTS_NOT_REGISTERED",
            result["runtime"]["blockers"],
        )


if __name__ == "__main__":
    unittest.main()

# CI_REFRESH_PRIVATE_REPO_BROKER_V1

# CI_REFRESH_MASTER_GATE_STEERING_V1

# CI_REFRESH_MASTER_GATE_STEERING_FIX_V2
