import datetime as dt
import unittest

from autorecovery_supervisor import (
    AssignmentCheckpoint,
    AssignmentState,
    AutoRecoveryError,
    AutoRecoverySupervisor,
    MemoryAssignmentStore,
    RecoveryRoute,
    StopReason,
    SupervisorAction,
    WorkerHealth,
    WorkerInvocation,
    WorkerKind,
    build_continuation_packet,
    classify_assignment,
    route_for_attempt,
)


NOW = dt.datetime(2026, 9, 20, 15, 0, tzinfo=dt.timezone.utc)


def checkpoint() -> AssignmentCheckpoint:
    return AssignmentCheckpoint(
        task_id="GOD-PREP-0017",
        objective="Prepare GOD Mode safely.",
        current_phase="pre-activation audit",
        completed_work=("A", "B", "C"),
        remaining_work=("D", "E"),
        last_safe_checkpoint="checkpoint-42",
        repo="jayagius99/jaytec-work-engine-v2-g1",
        branch="security/root-owner-control-v1",
        commit_head="362face5532ca986223573b47d6c7c247565a788",
        open_pr=17,
        current_files_state={"dirty": False},
        tests_completed=("root-owner-security-validation", "v2-m1-validation"),
        known_failures=("hardware-key-gate-pending",),
        active_constraints=(
            "do not recreate completed work",
            "do not activate production",
        ),
        authority_envelope={"root_owner": "Jay", "auto_spend": False},
        cost_envelope={"paid_fallback": False},
        dependencies=("two physical security keys",),
        next_intended_action="Continue the pre-activation audit.",
        worker_specialist_preference=("sol", "gemini"),
        checkpoint_number=42,
    ).validate()


def state(
    *,
    stop_reason=StopReason.RUNNING,
    worker_kind=WorkerKind.JAYTEC_CALLABLE,
    heartbeat_age_seconds=30,
    recovery_attempts=0,
    fencing_token=7,
    completed=False,
) -> AssignmentState:
    heartbeat = (
        NOW - dt.timedelta(seconds=heartbeat_age_seconds)
        if heartbeat_age_seconds is not None
        else None
    )
    return AssignmentState(
        task_id="GOD-PREP-0017",
        checkpoint=checkpoint(),
        stop_reason=stop_reason,
        worker_kind=worker_kind,
        worker_id="worker-a",
        worker_route="primary",
        last_heartbeat_at=heartbeat,
        last_progress_at=heartbeat,
        progress_marker="phase-c",
        recovery_attempts=recovery_attempts,
        fencing_token=fencing_token,
        lease_owner=None,
        lease_expires_at=None,
        completed=completed,
        last_error=None,
        updated_at=NOW,
    )


class FakeVerifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = 0

    def verify(self, cp):
        self.calls += 1
        return self.ok, "verified" if self.ok else "head mismatch"


class FakeInvoker:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.calls = []

    def invoke(self, *, checkpoint, continuation_packet, route, fencing_token):
        self.calls.append(
            {
                "checkpoint": checkpoint,
                "packet": continuation_packet,
                "route": route,
                "token": fencing_token,
            }
        )
        return WorkerInvocation(
            accepted=self.accepted,
            worker_id="worker-b" if self.accepted else None,
            route=route,
            detail="" if self.accepted else "provider unavailable",
        )


class FakeHealth:
    def __init__(self, healthy=True, progress_marker="phase-d"):
        self.healthy = healthy
        self.progress_marker = progress_marker
        self.calls = []

    def wait_for_healthy(
        self, *, task_id, fencing_token, worker_id, timeout_seconds
    ):
        self.calls.append((task_id, fencing_token, worker_id, timeout_seconds))
        return WorkerHealth(
            healthy=self.healthy,
            worker_id=worker_id,
            heartbeat_at=NOW + dt.timedelta(seconds=1) if self.healthy else None,
            progress_marker=self.progress_marker if self.healthy else None,
            detail="" if self.healthy else "no heartbeat",
        )


class FakeNotifier:
    def __init__(self):
        self.events = []

    def notify(self, event):
        self.events.append(dict(event))


class AutoRecoveryPolicyTests(unittest.TestCase):
    def test_master_gate_success_does_not_complete_whole_assignment(self):
        cp = AssignmentCheckpoint(
            **{
                **checkpoint().__dict__,
                "current_phase": "G03/SECURITY: Security Audit #47",
                "objective": "Advance G03.",
                "checkpoint_number": 103,
            }
        ).validate()
        st = state()
        st = AssignmentState(**{**st.__dict__, "checkpoint": cp})
        store = MemoryAssignmentStore(st)
        supervisor = AutoRecoverySupervisor(
            store=store,
            verifier=FakeVerifier(),
            invoker=FakeInvoker(),
            health_probe=FakeHealth(
                healthy=True,
                progress_marker="MANUS_TERMINAL:SUCCESS:deadbeef",
            ),
            heartbeat_timeout_seconds=600,
        )
        self.assertTrue(supervisor.refresh_worker_health("GOD-PREP-0017", now=NOW))
        final = store.get("GOD-PREP-0017")
        self.assertEqual(final.stop_reason, StopReason.WAITING_FOR_DEPENDENCY)
        self.assertFalse(final.completed)
        self.assertTrue(final.last_error.startswith("MANUS_TERMINAL:SUCCESS:"))

    def test_complete_stops_watching(self):
        decision = classify_assignment(
            state(stop_reason=StopReason.COMPLETED, completed=True),
            now=NOW,
        )
        self.assertEqual(decision.action, SupervisorAction.STOP_WATCH)

    def test_owner_pause_is_never_recovered(self):
        decision = classify_assignment(
            state(stop_reason=StopReason.PAUSED_BY_OWNER),
            now=NOW,
        )
        self.assertEqual(decision.action, SupervisorAction.HOLD)
        self.assertEqual(decision.effective_stop_reason, StopReason.PAUSED_BY_OWNER)

    def test_operator_pause_is_never_recovered(self):
        decision = classify_assignment(
            state(stop_reason=StopReason.PAUSED_BY_OPERATOR),
            now=NOW,
        )
        self.assertEqual(decision.action, SupervisorAction.HOLD)

    def test_authority_and_dependency_waits_are_never_recovered(self):
        for reason in (
            StopReason.WAITING_FOR_AUTHORITY,
            StopReason.WAITING_FOR_RESOURCE,
            StopReason.WAITING_FOR_DEPENDENCY,
        ):
            with self.subTest(reason=reason):
                self.assertEqual(
                    classify_assignment(state(stop_reason=reason), now=NOW).action,
                    SupervisorAction.HOLD,
                )

    def test_required_input_notifies_but_does_not_resume(self):
        decision = classify_assignment(
            state(stop_reason=StopReason.WAITING_FOR_REQUIRED_INPUT),
            now=NOW,
        )
        self.assertEqual(decision.action, SupervisorAction.NOTIFY_JAY)

    def test_healthy_worker_is_left_alone(self):
        decision = classify_assignment(state(), now=NOW)
        self.assertEqual(decision.action, SupervisorAction.NOOP_HEALTHY)

    def test_stale_running_worker_becomes_recoverable_worker_lost(self):
        decision = classify_assignment(
            state(heartbeat_age_seconds=601),
            now=NOW,
            heartbeat_timeout_seconds=600,
        )
        self.assertEqual(decision.action, SupervisorAction.RECOVER)
        self.assertEqual(decision.effective_stop_reason, StopReason.WORKER_LOST)

    def test_chatgpt_ui_gets_manual_packet_not_automatic_recovery(self):
        decision = classify_assignment(
            state(
                stop_reason=StopReason.TIMEOUT,
                worker_kind=WorkerKind.CHATGPT_UI,
            ),
            now=NOW,
        )
        self.assertEqual(decision.action, SupervisorAction.MANUAL_RESUME_PACKET)
        self.assertEqual(
            decision.continuation_packet["instruction"],
            "Resume — do not recreate completed work",
        )
        self.assertEqual(decision.continuation_packet["verified_head"], checkpoint().commit_head)

    def test_recovery_routes_are_bounded(self):
        self.assertEqual(route_for_attempt(1), RecoveryRoute.SAME_WORKER_PROVIDER)
        self.assertEqual(route_for_attempt(2), RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT)
        self.assertEqual(route_for_attempt(3), RecoveryRoute.ALTERNATE_APPROVED_ROUTE)
        with self.assertRaisesRegex(AutoRecoveryError, "OUT_OF_RANGE"):
            route_for_attempt(4)

    def test_three_failed_attempts_require_human_review(self):
        decision = classify_assignment(
            state(
                stop_reason=StopReason.STALLED_RECOVERABLE,
                recovery_attempts=3,
            ),
            now=NOW,
        )
        self.assertEqual(decision.action, SupervisorAction.NOTIFY_JAY)
        self.assertEqual(decision.effective_stop_reason, StopReason.RECOVERY_EXHAUSTED)

    def test_checkpoint_advance_preserves_worker_fence_and_attempts(self):
        store = MemoryAssignmentStore(state())
        before = store.get("GOD-PREP-0017")
        newer = AssignmentCheckpoint(
            **{
                **checkpoint().__dict__,
                "current_phase": "G43/SECURITY: next gate",
                "objective": "Advance G43.",
                "checkpoint_number": 43,
            }
        ).validate()
        changed = store.advance_checkpoint_preserving_runtime(
            newer,
            expected_current_checkpoint_number=42,
            now=NOW + dt.timedelta(seconds=1),
        )
        self.assertTrue(changed)
        after = store.get("GOD-PREP-0017")
        self.assertEqual(after.checkpoint.checkpoint_number, 43)
        self.assertEqual(after.worker_id, before.worker_id)
        self.assertEqual(after.fencing_token, before.fencing_token)
        self.assertEqual(after.recovery_attempts, before.recovery_attempts)
        self.assertEqual(after.stop_reason, before.stop_reason)

    def test_continuation_packet_contains_resurrection_state(self):
        packet = build_continuation_packet(
            checkpoint(),
            fencing_token=9,
            recovery_route=RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT,
        )
        for key in (
            "task_id",
            "objective",
            "current_phase",
            "completed_work",
            "remaining_work",
            "last_safe_checkpoint",
            "repo",
            "branch",
            "verified_head",
            "open_pr",
            "current_files_state",
            "tests_already_completed",
            "known_failures",
            "active_constraints",
            "authority_envelope",
            "cost_envelope",
            "dependencies",
            "next_intended_action",
            "worker_specialist_preference",
            "fencing_token",
        ):
            self.assertIn(key, packet)
        self.assertEqual(packet["checkpoint_number"], 42)


class AutoRecoveryExecutionTests(unittest.TestCase):
    def test_bootstrapped_callable_without_worker_or_heartbeat_recovers_once(self):
        seeded = state(
            stop_reason=StopReason.RUNNING,
            worker_kind=WorkerKind.JAYTEC_CALLABLE,
            heartbeat_age_seconds=None,
            recovery_attempts=0,
            fencing_token=0,
        )
        seeded = AssignmentState(
            **{
                **seeded.__dict__,
                "worker_id": None,
                "worker_route": "jaytec-manus-lite-v1",
                "last_heartbeat_at": None,
                "last_progress_at": None,
                "progress_marker": None,
            }
        )
        store = MemoryAssignmentStore(seeded)
        invoker = FakeInvoker()
        supervisor = AutoRecoverySupervisor(
            store=store,
            verifier=FakeVerifier(),
            invoker=invoker,
            health_probe=FakeHealth(),
            instance_id="watch-bootstrap",
        )

        self.assertFalse(
            supervisor.refresh_worker_health("GOD-PREP-0017", now=NOW)
        )
        result = supervisor.tick("GOD-PREP-0017", now=NOW)

        self.assertEqual(result.action, SupervisorAction.RECOVERY_STARTED)
        self.assertEqual(len(invoker.calls), 1)
        self.assertEqual(invoker.calls[0]["token"], 1)
        self.assertEqual(store.state.worker_id, "worker-b")
        self.assertEqual(store.state.fencing_token, 1)
        self.assertEqual(store.state.stop_reason, StopReason.RUNNING)
        self.assertIsNone(store.state.lease_owner)

    def test_callable_worker_recovers_with_exclusive_fencing_token(self):
        store = MemoryAssignmentStore(
            state(stop_reason=StopReason.WORKER_LOST, recovery_attempts=0)
        )
        verifier = FakeVerifier()
        invoker = FakeInvoker()
        health = FakeHealth()
        notifier = FakeNotifier()
        supervisor = AutoRecoverySupervisor(
            store=store,
            verifier=verifier,
            invoker=invoker,
            health_probe=health,
            notifier=notifier,
            instance_id="watch-a",
        )

        result = supervisor.tick("GOD-PREP-0017", now=NOW)

        self.assertEqual(result.action, SupervisorAction.RECOVERY_STARTED)
        self.assertEqual(result.recovery_route, RecoveryRoute.SAME_WORKER_PROVIDER)
        self.assertEqual(len(invoker.calls), 1)
        token = invoker.calls[0]["token"]
        self.assertGreater(token, 7)
        self.assertEqual(
            invoker.calls[0]["packet"]["instruction"],
            "Resume — do not recreate completed work",
        )
        self.assertEqual(store.state.stop_reason, StopReason.RUNNING)
        self.assertEqual(store.state.worker_id, "worker-b")
        self.assertIsNone(store.state.lease_owner)
        self.assertTrue(
            any(e.get("event") == "JAYTEC_AUTORECOVERY_RESUMED" for e in notifier.events)
        )

    def test_two_supervisors_cannot_hold_same_recovery_lease(self):
        store = MemoryAssignmentStore(
            state(stop_reason=StopReason.WORKER_LOST, recovery_attempts=0)
        )
        first = store.acquire_recovery_lease(
            "GOD-PREP-0017",
            lease_owner="watch-a",
            now=NOW,
        )
        second = store.acquire_recovery_lease(
            "GOD-PREP-0017",
            lease_owner="watch-b",
            now=NOW + dt.timedelta(seconds=1),
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_old_worker_is_rejected_after_fencing_token_advances(self):
        store = MemoryAssignmentStore(
            state(stop_reason=StopReason.WORKER_LOST, fencing_token=4)
        )
        lease = store.acquire_recovery_lease(
            "GOD-PREP-0017",
            lease_owner="watch-a",
            now=NOW,
        )
        self.assertEqual(lease.fencing_token, 5)
        with self.assertRaisesRegex(AutoRecoveryError, "STALE_FENCING_TOKEN"):
            store.heartbeat(
                "GOD-PREP-0017",
                fencing_token=4,
                worker_id="old-worker",
                now=NOW,
            )

    def test_one_hundred_competing_recovery_leases_yield_one_owner(self):
        store = MemoryAssignmentStore(
            state(stop_reason=StopReason.WORKER_LOST, recovery_attempts=0)
        )
        winners = []
        for index in range(100):
            lease = store.acquire_recovery_lease(
                "GOD-PREP-0017",
                lease_owner=f"watch-{index}",
                now=NOW + dt.timedelta(milliseconds=index),
            )
            if lease is not None:
                winners.append(lease)
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].lease_owner, "watch-0")

    def test_one_hundred_stale_worker_heartbeats_never_cross_new_fence(self):
        store = MemoryAssignmentStore(
            state(stop_reason=StopReason.WORKER_LOST, fencing_token=40)
        )
        lease = store.acquire_recovery_lease(
            "GOD-PREP-0017",
            lease_owner="watch-current",
            now=NOW,
        )
        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertEqual(lease.fencing_token, 41)
        for index in range(100):
            with self.assertRaisesRegex(AutoRecoveryError, "STALE_FENCING_TOKEN"):
                store.heartbeat(
                    "GOD-PREP-0017",
                    fencing_token=40,
                    worker_id=f"stale-worker-{index}",
                    now=NOW + dt.timedelta(seconds=index),
                )
        self.assertEqual(store.state.fencing_token, 41)
        self.assertEqual(store.state.lease_owner, "watch-current")

    def test_checkpoint_head_mismatch_never_invokes_worker(self):
        store = MemoryAssignmentStore(
            state(stop_reason=StopReason.WORKER_LOST)
        )
        verifier = FakeVerifier(ok=False)
        invoker = FakeInvoker()
        supervisor = AutoRecoverySupervisor(
            store=store,
            verifier=verifier,
            invoker=invoker,
            health_probe=FakeHealth(),
            instance_id="watch-a",
        )
        result = supervisor.tick("GOD-PREP-0017", now=NOW)
        self.assertEqual(result.action, SupervisorAction.NOTIFY_JAY)
        self.assertEqual(store.state.stop_reason, StopReason.WAITING_FOR_DEPENDENCY)
        self.assertEqual(invoker.calls, [])

    def test_failed_health_verification_stays_recoverable_until_bounded_limit(self):
        store = MemoryAssignmentStore(
            state(stop_reason=StopReason.WORKER_LOST, recovery_attempts=0)
        )
        supervisor = AutoRecoverySupervisor(
            store=store,
            verifier=FakeVerifier(),
            invoker=FakeInvoker(),
            health_probe=FakeHealth(healthy=False),
            instance_id="watch-a",
        )
        result = supervisor.tick("GOD-PREP-0017", now=NOW)
        self.assertEqual(result.action, SupervisorAction.RECOVERY_FAILED)
        self.assertEqual(store.state.stop_reason, StopReason.STALLED_RECOVERABLE)
        self.assertEqual(store.state.recovery_attempts, 1)

    def test_health_refresh_keeps_callable_worker_alive_before_tick(self):
        store = MemoryAssignmentStore(
            state(
                stop_reason=StopReason.RUNNING,
                worker_kind=WorkerKind.JAYTEC_CALLABLE,
                heartbeat_age_seconds=900,
            )
        )
        health = FakeHealth(healthy=True, progress_marker="still-running")
        supervisor = AutoRecoverySupervisor(
            store=store,
            verifier=FakeVerifier(),
            invoker=FakeInvoker(),
            health_probe=health,
            instance_id="watch-a",
        )
        refreshed = supervisor.refresh_worker_health("GOD-PREP-0017", now=NOW)
        self.assertTrue(refreshed)
        decision = supervisor.tick("GOD-PREP-0017", now=NOW)
        self.assertEqual(decision.action, SupervisorAction.NOOP_HEALTHY)
        self.assertEqual(store.state.progress_marker, "still-running")

    def test_failed_health_refresh_allows_same_cycle_recovery_of_stale_worker(self):
        store = MemoryAssignmentStore(
            state(
                stop_reason=StopReason.RUNNING,
                worker_kind=WorkerKind.JAYTEC_CALLABLE,
                heartbeat_age_seconds=900,
            )
        )
        invoker = FakeInvoker()
        supervisor = AutoRecoverySupervisor(
            store=store,
            verifier=FakeVerifier(),
            invoker=invoker,
            health_probe=FakeHealth(healthy=False),
            instance_id="watch-a",
        )
        refreshed = supervisor.refresh_worker_health("GOD-PREP-0017", now=NOW)
        self.assertFalse(refreshed)
        decision = supervisor.tick("GOD-PREP-0017", now=NOW)
        self.assertEqual(decision.action, SupervisorAction.RECOVERY_FAILED)
        self.assertEqual(
            decision.reason,
            "RECOVERY_HEALTH_VERIFY_FAILED",
        )
        self.assertEqual(len(invoker.calls), 1)
        self.assertEqual(store.state.stop_reason, StopReason.STALLED_RECOVERABLE)

    def test_stale_fence_during_refresh_cannot_revive_old_worker(self):
        store = MemoryAssignmentStore(
            state(
                stop_reason=StopReason.RUNNING,
                worker_kind=WorkerKind.JAYTEC_CALLABLE,
                heartbeat_age_seconds=900,
                fencing_token=10,
            )
        )
        health = FakeHealth(healthy=True)
        supervisor = AutoRecoverySupervisor(
            store=store,
            verifier=FakeVerifier(),
            invoker=FakeInvoker(),
            health_probe=health,
            instance_id="watch-a",
        )
        # Simulate a newer recovery fencing transition after health state was read.
        original = store.heartbeat
        def reject_old(*args, **kwargs):
            raise AutoRecoveryError("STALE_FENCING_TOKEN")
        store.heartbeat = reject_old
        try:
            self.assertFalse(supervisor.refresh_worker_health("GOD-PREP-0017", now=NOW))
        finally:
            store.heartbeat = original

    def test_terminal_success_completes_canonical_assignment(self):
        store = MemoryAssignmentStore(
            state(
                stop_reason=StopReason.RUNNING,
                worker_kind=WorkerKind.JAYTEC_CALLABLE,
                heartbeat_age_seconds=900,
            )
        )
        class TerminalHealth(FakeHealth):
            def wait_for_healthy(self, *, task_id, fencing_token, worker_id, timeout_seconds):
                return WorkerHealth(
                    healthy=True,
                    worker_id=worker_id,
                    heartbeat_at=NOW,
                    progress_marker="MANUS_TERMINAL:SUCCESS:abc123",
                    detail="verified complete",
                )
        notifier = FakeNotifier()
        supervisor = AutoRecoverySupervisor(
            store=store,
            verifier=FakeVerifier(),
            invoker=FakeInvoker(),
            health_probe=TerminalHealth(),
            notifier=notifier,
            instance_id="watch-a",
        )
        self.assertTrue(supervisor.refresh_worker_health("GOD-PREP-0017", now=NOW))
        self.assertEqual(store.state.stop_reason, StopReason.COMPLETED)
        self.assertTrue(store.state.completed)
        decision = supervisor.tick("GOD-PREP-0017", now=NOW)
        self.assertEqual(decision.action, SupervisorAction.STOP_WATCH)
        self.assertEqual(decision.effective_stop_reason, StopReason.COMPLETED)



    def test_needs_owner_is_the_explicit_owner_notification_terminal(self):
        store = MemoryAssignmentStore(
            state(
                stop_reason=StopReason.RUNNING,
                worker_kind=WorkerKind.JAYTEC_CALLABLE,
                heartbeat_age_seconds=900,
            )
        )
        class NeedsOwnerHealth(FakeHealth):
            def wait_for_healthy(self, *, task_id, fencing_token, worker_id, timeout_seconds):
                return WorkerHealth(
                    healthy=True,
                    worker_id=worker_id,
                    heartbeat_at=NOW,
                    progress_marker="MANUS_TERMINAL:NEEDS_OWNER:owner123",
                    detail="verified complete",
                )
        supervisor = AutoRecoverySupervisor(
            store=store,
            verifier=FakeVerifier(),
            invoker=FakeInvoker(),
            health_probe=NeedsOwnerHealth(),
            notifier=FakeNotifier(),
            instance_id="watch-a",
        )
        self.assertTrue(supervisor.refresh_worker_health("GOD-PREP-0017", now=NOW))
        self.assertEqual(store.state.stop_reason, StopReason.WAITING_FOR_REQUIRED_INPUT)
        decision = supervisor.tick("GOD-PREP-0017", now=NOW)
        self.assertEqual(decision.action, SupervisorAction.NOTIFY_JAY)
        self.assertEqual(decision.reason, "INTENTIONAL_OR_EXTERNAL_BLOCKER")

    def test_failed_closed_terminal_enters_bounded_recovery_not_owner_hold(self):
        store = MemoryAssignmentStore(
            state(
                stop_reason=StopReason.RUNNING,
                worker_kind=WorkerKind.JAYTEC_CALLABLE,
                heartbeat_age_seconds=900,
                recovery_attempts=0,
            )
        )
        class FailedClosedHealth(FakeHealth):
            def wait_for_healthy(self, *, task_id, fencing_token, worker_id, timeout_seconds):
                return WorkerHealth(
                    healthy=True,
                    worker_id=worker_id,
                    heartbeat_at=NOW,
                    progress_marker="MANUS_TERMINAL:FAILED_CLOSED:fail123",
                    detail="verified complete",
                )
        invoker = FakeInvoker()
        supervisor = AutoRecoverySupervisor(
            store=store,
            verifier=FakeVerifier(),
            invoker=invoker,
            health_probe=FailedClosedHealth(),
            notifier=FakeNotifier(),
            instance_id="watch-a",
        )
        self.assertTrue(supervisor.refresh_worker_health("GOD-PREP-0017", now=NOW))
        self.assertEqual(store.state.stop_reason, StopReason.STALLED_RECOVERABLE)
        decision = supervisor.tick("GOD-PREP-0017", now=NOW)
        self.assertEqual(decision.action, SupervisorAction.RECOVERY_FAILED)
        self.assertEqual(len(invoker.calls), 1)

    def test_local_preflight_rejection_keeps_fence_but_refunds_attempt(self):
        seeded = state(
            stop_reason=StopReason.TRANSIENT_PROVIDER_FAILURE,
            recovery_attempts=2,
            fencing_token=10,
        )
        store = MemoryAssignmentStore(seeded)

        class LocalPreflightRejector:
            def __init__(self):
                self.calls = []
            def invoke(self, *, checkpoint, continuation_packet, route, fencing_token):
                self.calls.append((route, fencing_token))
                return WorkerInvocation(
                    accepted=False,
                    worker_id=None,
                    route=route,
                    detail="MANUS_RECOVERY_PREFLIGHT_MESSAGE_TOO_LARGE",
                )

        invoker = LocalPreflightRejector()
        supervisor = AutoRecoverySupervisor(
            store=store,
            verifier=FakeVerifier(),
            invoker=invoker,
            health_probe=FakeHealth(),
            instance_id="watch-local-preflight",
        )
        result = supervisor.tick("GOD-PREP-0017", now=NOW)
        self.assertEqual(result.action, SupervisorAction.RECOVERY_FAILED)
        self.assertEqual(
            result.reason,
            "RECOVERY_LOCAL_PREFLIGHT_REJECTED_ATTEMPT_NOT_CONSUMED",
        )
        self.assertEqual(result.recovery_route, RecoveryRoute.ALTERNATE_APPROVED_ROUTE)
        self.assertEqual(store.state.recovery_attempts, 2)
        self.assertEqual(store.state.fencing_token, 11)
        self.assertEqual(store.state.stop_reason, StopReason.TRANSIENT_PROVIDER_FAILURE)
        self.assertIn("ATTEMPT_NOT_CONSUMED", str(store.state.last_error))
        self.assertIsNone(store.state.lease_owner)
        self.assertEqual(invoker.calls, [(RecoveryRoute.ALTERNATE_APPROVED_ROUTE, 11)])

    def test_exact_exhausted_local_preflight_can_refund_once_without_rolling_back_fence(self):
        seeded = state(
            stop_reason=StopReason.RECOVERY_EXHAUSTED,
            recovery_attempts=3,
            fencing_token=11,
        )
        seeded = AssignmentState(
            **{
                **seeded.__dict__,
                "last_error": (
                    "RECOVERY_INVOCATION_REJECTED:"
                    "MANUS_RECOVERY_PREFLIGHT_MESSAGE_TOO_LARGE"
                ),
            }
        )
        store = MemoryAssignmentStore(seeded)
        repaired = store.reconcile_exhausted_local_preflight(
            "GOD-PREP-0017",
            expected_fencing_token=11,
            expected_recovery_attempts=3,
            expected_error=(
                "RECOVERY_INVOCATION_REJECTED:"
                "MANUS_RECOVERY_PREFLIGHT_MESSAGE_TOO_LARGE"
            ),
            now=NOW,
        )
        self.assertTrue(repaired)
        self.assertEqual(store.state.recovery_attempts, 2)
        self.assertEqual(store.state.fencing_token, 11)
        self.assertEqual(store.state.stop_reason, StopReason.TRANSIENT_PROVIDER_FAILURE)
        self.assertIn("LOCAL_PREFLIGHT_ATTEMPT_REFUNDED", str(store.state.last_error))

        # The repair is one-way/exact-state scoped. Replaying it cannot refund again.
        self.assertFalse(
            store.reconcile_exhausted_local_preflight(
                "GOD-PREP-0017",
                expected_fencing_token=11,
                expected_recovery_attempts=3,
                expected_error=(
                    "RECOVERY_INVOCATION_REJECTED:"
                    "MANUS_RECOVERY_PREFLIGHT_MESSAGE_TOO_LARGE"
                ),
                now=NOW,
            )
        )
        self.assertEqual(store.state.recovery_attempts, 2)
        self.assertEqual(store.state.fencing_token, 11)

    def test_final_failed_closed_recovery_attempt_exhausts_without_fourth_attempt(self):
        store = MemoryAssignmentStore(
            state(
                stop_reason=StopReason.RUNNING,
                worker_kind=WorkerKind.JAYTEC_CALLABLE,
                heartbeat_age_seconds=900,
                recovery_attempts=2,
            )
        )

        class FailedClosedHealth(FakeHealth):
            def wait_for_healthy(self, *, task_id, fencing_token, worker_id, timeout_seconds):
                return WorkerHealth(
                    healthy=True,
                    worker_id=worker_id,
                    heartbeat_at=NOW,
                    progress_marker="MANUS_TERMINAL:FAILED_CLOSED:fail-final",
                    detail="verified complete",
                )

        invoker = FakeInvoker()
        notifier = FakeNotifier()
        supervisor = AutoRecoverySupervisor(
            store=store,
            verifier=FakeVerifier(),
            invoker=invoker,
            health_probe=FailedClosedHealth(),
            notifier=notifier,
            instance_id="watch-a",
        )

        self.assertTrue(supervisor.refresh_worker_health("GOD-PREP-0017", now=NOW))
        decision = supervisor.tick("GOD-PREP-0017", now=NOW)

        self.assertEqual(decision.action, SupervisorAction.RECOVERY_FAILED)
        self.assertEqual(decision.effective_stop_reason, StopReason.RECOVERY_EXHAUSTED)
        self.assertEqual(decision.reason, "RECOVERY_ATTEMPTS_EXHAUSTED")
        self.assertEqual(store.state.recovery_attempts, 3)
        self.assertEqual(store.state.stop_reason, StopReason.RECOVERY_EXHAUSTED)
        self.assertEqual(len(invoker.calls), 1)
        self.assertTrue(
            any(
                event.get("event") == "JAYTEC_AUTORECOVERY_EXHAUSTED"
                for event in notifier.events
            )
        )

    def test_needs_jaytec_terminal_result_is_internal_dependency_not_owner_notification(self):
        seeded = state(
            stop_reason=StopReason.RUNNING,
            worker_kind=WorkerKind.JAYTEC_CALLABLE,
            heartbeat_age_seconds=900,
        )
        store = MemoryAssignmentStore(seeded)

        class NeedsJaytecHealth(FakeHealth):
            def wait_for_healthy(self, *, task_id, fencing_token, worker_id, timeout_seconds):
                return WorkerHealth(
                    healthy=True,
                    worker_id=worker_id,
                    heartbeat_at=NOW,
                    progress_marker="MANUS_TERMINAL:NEEDS_JAYTEC:abc123",
                    detail="verified complete",
                )

        notifier = FakeNotifier()
        supervisor = AutoRecoverySupervisor(
            store=store,
            verifier=FakeVerifier(),
            invoker=FakeInvoker(),
            health_probe=NeedsJaytecHealth(),
            notifier=notifier,
            instance_id="watch-a",
        )
        self.assertTrue(supervisor.refresh_worker_health("GOD-PREP-0017", now=NOW))
        self.assertEqual(store.state.stop_reason, StopReason.WAITING_FOR_DEPENDENCY)
        self.assertTrue(str(store.state.last_error).startswith("MANUS_TERMINAL:NEEDS_JAYTEC:"))
        decision = supervisor.tick("GOD-PREP-0017", now=NOW)
        self.assertEqual(decision.action, SupervisorAction.HOLD)
        self.assertEqual(decision.reason, "JAYTEC_INTERNAL_HANDOFF_REQUIRED")
        self.assertFalse(
            any(e.get("owner_notification_required") is True for e in notifier.events)
        )

    def test_ui_chat_never_calls_invoker(self):
        store = MemoryAssignmentStore(
            state(
                stop_reason=StopReason.TIMEOUT,
                worker_kind=WorkerKind.CHATGPT_UI,
            )
        )
        invoker = FakeInvoker()
        notifier = FakeNotifier()
        supervisor = AutoRecoverySupervisor(
            store=store,
            verifier=FakeVerifier(),
            invoker=invoker,
            health_probe=FakeHealth(),
            notifier=notifier,
        )
        result = supervisor.tick("GOD-PREP-0017", now=NOW)
        self.assertEqual(result.action, SupervisorAction.MANUAL_RESUME_PACKET)
        self.assertEqual(invoker.calls, [])
        self.assertEqual(
            notifier.events[0]["event"],
            "JAYTEC_AUTORECOVERY_MANUAL_RESUME_REQUIRED",
        )


if __name__ == "__main__":
    unittest.main()

# EXACT_HEAD_CERT_PRIVATE_REPO_BROKER_V3
