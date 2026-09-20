import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from autorecovery_watch_ingress import (
    FORGE_TASK_ID,
    WatchIngressError,
    _broker_context,
    _observed_refs,
    execute_watch_cycle,
)
from manus_governance import specialist_request
from autorecovery_supervisor import (
    AssignmentCheckpoint,
    AssignmentState,
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
        objective="Continue Forge preparation.",
        current_phase="pre-activation",
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
        next_intended_action="Continue safely.",
        worker_specialist_preference=("manus-lite",),
        checkpoint_number=1,
    ).validate()


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

    def get(self, task_id):
        return self.state if task_id == FORGE_TASK_ID else None

    def heartbeat(self, task_id, *, fencing_token, worker_id, progress_marker=None, now=None):
        assert task_id == FORGE_TASK_ID
        assert fencing_token == self.state.fencing_token
        assert worker_id == self.state.worker_id
        self.heartbeats.append(progress_marker)
        self.state = AssignmentState(
            **{
                **self.state.__dict__,
                "stop_reason": StopReason.RUNNING,
                "last_heartbeat_at": self.state.updated_at,
                "last_progress_at": self.state.updated_at,
                "progress_marker": progress_marker,
                "recovery_attempts": 0 if progress_marker is not None else self.state.recovery_attempts,
                "last_error": None,
            }
        )

    def mark_stop(self, *args, **kwargs):
        raise AssertionError("successful broker handoff must not stop assignment")


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
                {
                    "task_id": FORGE_TASK_ID,
                    "observed_refs": refs,
                    "github_broker_context": broker_payload(refs),
                },
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
                {
                    "task_id": FORGE_TASK_ID,
                    "observed_refs": refs,
                    "github_broker_context": broker_payload(refs),
                },
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
                {
                    "task_id": FORGE_TASK_ID,
                    "observed_refs": {"main": "a" * 40},
                },
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
                {
                    "task_id": FORGE_TASK_ID,
                    "observed_refs": {"main": "a" * 40},
                },
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
