# JAYTEC WATCH + AUTORECOVERY — Activation Runbook

Status: **installed in staging architecture, fail-closed by default**.

This runbook exists to prevent a future operator, GOD Mode worker, or recovery
supervisor from confusing "code exists" with "automatic recovery is active."

## Current safety posture

The current GOD Mode / ROOT_OWNER preparation assignment is intentionally
paused in a normal ChatGPT UI conversation.

Therefore:

- its canonical intended stop reason is an owner/operator pause;
- it MUST NOT be automatically resumed;
- a normal ChatGPT UI conversation is not a callable Level 2 worker;
- existing GitHub WATCH evidence is observer-only;
- PR #17 / ROOT_OWNER state must not be touched by AUTORECOVERY setup.

## Activation prerequisites

Automatic Level 2 recovery for an assignment may be enabled only after all of
these are independently proven:

1. **Durable schema**
   - `jaytec_assignment_state` exists in the intended JAYTEC database.
   - lease/fencing concurrency is tested.
   - schema migration is explicit and reviewed; status tools must never create
     it implicitly.

2. **Canonical resurrection checkpoint**
   - objective, phase, completed/remaining work, safe checkpoint, repo,
     branch/head, open PR, file/state summary, tests, failures, constraints,
     authority/cost envelopes, dependencies, next action and worker preference
     are recorded.
   - the checkpoint is validated before registration.

3. **Exact checkpoint verifier**
   - registered as `github_exact_head_v1` or a future explicitly approved
     verifier.
   - verifies repository, branch, head and any assignment-specific invariants.
   - mismatch blocks recovery rather than "fixing" state.

4. **Callable worker route**
   - normal ChatGPT UI is not sufficient.
   - at least one worker endpoint is explicitly registered as
     `JAYTEC_CALLABLE`.
   - the endpoint accepts the continuation packet and current fencing token.
   - provider/model/profile/connector/spend rules remain independently enforced.

5. **Fenced heartbeat/progress**
   - worker heartbeat/state writes require the current fencing token.
   - stale tokens are rejected.
   - genuine progress resets the bounded recovery-attempt count.

6. **Notification path**
   - human/input/authority blockers can reach Jay/GOD Mode.
   - notification does not itself grant execution authority.

7. **Five-minute supervisor runner**
   - runs one bounded supervisor tick approximately every five minutes.
   - duplicate runners are safe because only one recovery lease can win.
   - the runner is not allowed to buy compute/credits automatically.

8. **Adversarial acceptance**
   - PAUSED_BY_OWNER and PAUSED_BY_OPERATOR never resume.
   - WAITING_FOR_AUTHORITY / REQUIRED_INPUT / RESOURCE / DEPENDENCY never
     silently resume.
   - two simultaneous recovery checks start only one replacement worker.
   - an old worker waking after replacement is fenced out.
   - attempt 1 / 2 / 3 route sequence is enforced.
   - attempt 4 never occurs automatically.
   - normal ChatGPT UI work returns a manual continuation packet only.

## Activation environment contract

The staging runtime exposes these gates:

- `JAYTEC_AUTORECOVERY_ENABLED=1`
- `JAYTEC_AUTORECOVERY_SCHEMA_READY=1`
- `JAYTEC_AUTORECOVERY_CALLABLE_ROUTES=["<approved route id>"]`
- `JAYTEC_AUTORECOVERY_CHECKPOINT_VERIFIER=github_exact_head_v1`
- `JAYTEC_AUTORECOVERY_HEARTBEAT_MODE=fenced_postgres_v1`
- `JAYTEC_AUTORECOVERY_NOTIFICATION_MODE=event_log_v1` or an explicitly
  approved Jay notification adapter.

Setting `JAYTEC_AUTORECOVERY_ENABLED=1` alone is never enough. Missing or
invalid prerequisites produce `BLOCKED_FAIL_CLOSED`.

## Owner/operator pause rule

"Pause safely" is durable intent.

It must be represented as `PAUSED_BY_OWNER` or `PAUSED_BY_OPERATOR` in
canonical state before a Level 2 supervisor is active for that assignment.

No timeout, quiet GitHub history, stale chat window, expired heartbeat, or
provider availability change may override an intentional pause.

## Recovery routes

Attempt 1:
`SAME_WORKER_PROVIDER`

Attempt 2:
`FRESH_WORKER_SAME_CHECKPOINT`

Attempt 3:
`ALTERNATE_APPROVED_ROUTE`

After attempt 3:
`RECOVERY_EXHAUSTED` and notify/escalate.

"Alternate" means previously approved by the assignment authority/cost
envelope. It never means find a new paid provider, model, profile or connector.

## ChatGPT UI Level 1

For a UI-owned assignment, WATCH builds a continuation packet such as:

> Resume task GOD-PREP-0017 from checkpoint 42.  
> Current verified head: <sha>.  
> Completed work must not be repeated.  
> Resume — do not recreate completed work.

Jay performs the one supported manual resume action.

This remains the correct behavior until the assignment is transferred to a
registered callable JAYTEC worker.

## Level 2 future GOD Mode

When GOD Mode runs through a registered callable execution endpoint, its
assignment can outlive an individual worker:

```
JAYTEC canonical task state
        |
        v
WATCH + AUTORECOVERY
        |
  lease + fencing
        |
   callable worker
```

Individual workers may fail or be replaced; canonical assignment identity,
authority, checkpoint and completion state survive them.

## Current activation decision

**DO NOT enable Level 2 for the paused GOD Mode preparation assignment yet.**

The architecture is installed and testable, but the current assignment still
belongs to a normal ChatGPT UI worker and there is not yet a certified callable
GOD Mode execution endpoint registered for it.
