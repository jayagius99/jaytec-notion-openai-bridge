# JAYTEC FIVE-SEAT CONTINUOUS WORKER FABRIC V1

Status: FS01 CONTRACT  
Authority: Jay / ROOT_OWNER -> ChatGPT transformation owner -> WATCH controller -> bounded workers  
Freeze reference: jayagius99/jaytec-work-engine-v2-g1#126  
Transformation control: jayagius99/jaytec-work-engine-v2-g1#127  
Bridge baseline: 16701957c1dabaf70937819a03ded0e9c9d4cb0d

## 1. Purpose

Create one durable JAYTEC execution fabric with exactly five worker seats that can execute independent, compatible tasks continuously while a separate singleton WATCH controller owns admission, review, remedies and final result routing.

The design must increase throughput without multiplying authority.

WATCH is not a worker seat. ChatGPT schedules are not worker seats. A chat is not a worker lifetime.

## 2. Existing runtime reused

This design is additive over the existing bridge runtime. It does not create a second queue or source of truth.

Current main already provides:

- `concurrency.py`: PostgresConcurrencyScheduler, concurrency classes A-E, dependency checks, duplicate detection, read/mutation/resource collision detection and atomic scheduler admission.
- `job_runtime.py`: durable jobs, ownership epochs, fence tokens, leases, operation idempotency, stale-fence rejection and partial-side-effect handling.
- `job_runtime_schema.sql`: jobs, steps, operations, events, Guardian findings and durable TaskPacket storage.
- `durable_tasks_runtime.py`: durable queue, claim flow and lease-keepalive worker execution.
- `reliable_server.py`: multi-worker process pool; current default is four parallel workers.
- Existing WATCH control/recovery in the work repository and staging WATCH runtime.

The five-seat transformation extends those mechanisms. It does not replace them.

## 3. Authority hierarchy

1. Jay / ROOT_OWNER
2. ChatGPT transformation/assignment authority inside delegated scope
3. WATCH singleton controller/reviewer
4. Worker seat lease holder
5. Specialist/provider invoked inside the worker's exact TaskPacket

A lower layer cannot increase its own authority.

A worker cannot:
- create or widen its TaskPacket;
- approve its own result;
- change WATCH policy;
- change another seat;
- consume another task's fence;
- promote canonical/high-risk work merely because its task succeeded;
- silently choose another model/provider/cost route.

WATCH cannot cross owner-only boundaries or silently widen a task's approved authority.

## 4. Exactly five worker seats

The only worker seat IDs are:

- WORKER-SEAT-1
- WORKER-SEAT-2
- WORKER-SEAT-3
- WORKER-SEAT-4
- WORKER-SEAT-5

The ceiling is exactly five active worker seat leases.

WATCH/controller/auditor/dead-man scheduler is out-of-band and consumes zero seats.

A seat is a concurrency/ownership slot, not a permanent project role. A compatible task can use any free seat if authority and scope rules permit.

## 5. Durable task lifecycle

Normal path:

INTAKE
-> QUEUED
-> CLAIMED
-> RUNNING
-> HANDOFF_PENDING_REVIEW
-> REVIEWING
-> SUCCEEDED

Exception/remedy states:

BLOCKED_OWNER
BLOCKED_DEPENDENCY
FAILED_SAFE
REWORK_QUEUED
QUARANTINED
CANCEL_REQUESTED
CANCELLED
STALE
SUPERSEDED
ESCALATED

Rules:

- No accepted task may exist only in chat memory.
- CLAIMED requires a durable seat claim plus task lease/fence/epoch.
- RUNNING requires the claim still to be current.
- Worker completion does not imply SUCCEEDED.
- Worker completion creates an immutable handoff and moves the task to HANDOFF_PENDING_REVIEW.
- After the handoff is durably written, the worker relinquishes the seat and retires from that task.
- WATCH performs the independent review from durable evidence.
- Only WATCH review may transition the task to SUCCEEDED, REWORK_QUEUED, BLOCKED/QUARANTINED or ESCALATED.
- Rework is a new worker claim with a newer ownership epoch/fence; the previous result remains immutable evidence.

## 6. Seat lifecycle

FREE
-> CLAIMED
-> RUNNING
-> HANDOFF_WRITTEN
-> RELEASING
-> FREE

Failure branch:

CLAIMED/RUNNING
-> SUSPECT
-> RECONCILING
-> FREE or QUARANTINED

A seat may return to FREE only when:

- its worker authority has ended;
- any required handoff/checkpoint is durable;
- no unresolved operation still requires that seat's old fence;
- unresolved external effects have been reconciled or the affected resource/task is quarantined.

Seat reuse never revives an old worker token.

## 7. Ownership token

Every running task must bind:

- job_id / task_id
- worker_id
- seat_id
- ownership_epoch
- fence_token
- lease_owner
- lease_expires_at
- execution_room_id
- idempotency_key
- exact TaskPacket hash
- exact read_scope
- exact mutation_scope
- exact resource_scope
- concurrency_class
- collision key / normalized target identity

Any state-changing write or result handoff must prove the current ownership tuple.

Expired or superseded authority is rejected even if the old worker later returns.

## 8. Concurrency classes

Existing A-E semantics remain authoritative:

A — read-only. May parallelize unless it reads a surface under active mutation.

B — scoped mutation. May parallelize only when read/write/resource scopes are disjoint.

C — shared/canonical-state mutation. Serialized against C/D/E and any overlapping scope.

D — external side effect/deployment/production mutation. Serialized against C/D/E and overlapping scope/resource.

E — global-exclusive/high-risk. Runs alone.

Five available seats do not weaken C/D/E serialization.

## 9. Work stealing

A free seat may claim the highest-priority runnable queued task only when:

- required capability is compatible;
- task dependencies are complete;
- cost/provider policy is satisfiable without fallback;
- exact scopes do not collide;
- concurrency class permits admission;
- no duplicate assignment is active;
- no unresolved side effect blocks the relevant task/resource;
- no owner-only boundary is present.

A seat is never allowed to broaden itself merely to avoid being idle.

## 10. Immutable worker handoff

Before a worker retires it writes one durable result envelope containing at minimum:

- task/job ID
- worker ID
- seat ID
- ownership epoch
- fence token
- TaskPacket hash
- starting checkpoint / source refs
- exact mutations/operations attempted
- resulting refs/SHAs/deploy IDs/artifact IDs where applicable
- tests/validation performed
- evidence references
- spend/provider/model identity actually used
- unresolved items
- partial-side-effect status
- proposed next action
- completion classification claimed by worker
- handoff digest

The handoff is candidate evidence only.

## 11. WATCH independent review

WATCH owns one singleton leader lease/fence.

For each HANDOFF_PENDING_REVIEW task, WATCH independently checks:

- worker/fence/seat lineage;
- TaskPacket and authority compliance;
- scope/collision compliance;
- idempotency/replay state;
- evidence existence and identity;
- test/validation sufficiency;
- external-side-effect reconciliation;
- provider/model/cost compliance;
- canonical/high-risk promotion requirements;
- stale baseline or changed dependency conditions.

WATCH decisions:

ACCEPT
REWORK
BLOCK
ESCALATE

ACCEPT moves the task to SUCCEEDED or to the next separately-authorized canonical integration stage.

REWORK returns a bounded remediation packet to the queue. It does not revive the retired worker.

BLOCK/QUARANTINE prevents dependent work from using ambiguous state.

ESCALATE returns one consolidated owner decision packet.

## 12. Event-driven continuous operation

Persistent runtime workers, not ChatGPT schedules, provide continuity.

When a seat is released or new runnable work appears:

1. dispatcher reevaluates runnable queue;
2. a compatible free seat claims immediately;
3. worker executes under lease heartbeat;
4. worker writes handoff and retires;
5. WATCH reviews;
6. accepted result unblocks dependents and triggers another admission pass.

The scheduled ChatGPT continuation task is only a dead-man/recovery/control-plane continuity mechanism. It must not masquerade as one of the five workers.

## 13. Failure and remedy model

### Worker disappears
Lease expires -> old fence loses authority -> task enters reconciliation -> unresolved operations checked -> safe replacement receives newer fence or task quarantines.

### Worker returns late
All writes/results using stale seat/epoch/fence are rejected.

### Duplicate claim
Atomic scheduler/seat uniqueness prevents the second current claim. Duplicate intent is deduped by task identity + overlapping scope.

### Duplicate result
Handoff idempotency/digest prevents a second result from changing terminal/review state.

### Partial external side effect
Task/resource becomes QUARANTINED. No blind retry. Verify destination state first, then classify VERIFIED_COMPLETE / VERIFIED_NOT_DONE / FAILED_SAFE.

### WATCH dies
WATCH leader lease expires. A replacement controller may take a newer leader epoch/fence, reconcile pending reviews/claims, and continue. Old controller writes are rejected.

### Provider/model unavailable
Fail closed according to TaskPacket policy. No silent provider/model fallback.

### Retry storm
Bounded attempts + next_attempt_at/backoff + circuit-break policy. Failed workers cannot spin indefinitely.

### Canonical integration collision
Even if multiple seats finish simultaneously, class C/D/E/canonical integration remains serialized.

## 14. Required implementation delta

Existing capabilities should be extended additively:

### Schema
- durable seat identity/state and unique active seat ownership;
- seat_id on claimed job/result lineage;
- immutable worker handoff records;
- review state/decision records;
- singleton WATCH leader lease/fence;
- quarantine/remedy metadata where existing operation state is insufficient.

### Scheduler/runtime
- fixed worker seat ceiling = 5;
- deterministic seat allocation;
- seat-aware atomic claim;
- seat release/retirement only after durable handoff;
- stale-seat/fence rejection;
- immediate admission after release/completion;
- compatible work stealing.

### WATCH
- dispatcher/reviewer role outside seat pool;
- singleton leadership;
- durable pending-review loop;
- ACCEPT/REWORK/BLOCK/ESCALATE;
- no self-approval and no implicit authority widening.

### Observability
- seat roster/read model;
- active task/seat/lease/fence;
- pending reviews;
- quarantines;
- controller leader identity;
- queue depth and runnable depth;
- evidence/result destination.

## 15. Required proof before cutover

The fabric cannot be certified on unit tests alone.

Required proof includes:

- exactly 5 seats and no sixth claim;
- 2-seat, 3-seat and 5-seat compatible parallel execution;
- colliding jobs rejected while unrelated jobs proceed;
- C/D/E serialization under full five-seat load;
- worker death + lease expiry + safe replacement;
- stale worker result/write rejection;
- duplicate claim/result protection;
- partial-side-effect quarantine/no blind retry;
- worker handoff -> seat free -> WATCH review ordering;
- WATCH crash/re-election and stale-controller rejection;
- REWORK creates a new claim/fence rather than reviving old worker;
- immediate refill/work stealing after seat release;
- chat/scheduled-run interruption does not lose accepted work;
- no hidden fallback/spend;
- rollback/cutover proof.

## 16. Frozen-work invariant

Until Jay explicitly releases freeze #126:

- do not resume Forge/G03 progression;
- do not resume Companion;
- do not resume PC setup;
- do not resume V2/Uren work;
- do not activate WATCH canonical execution.

Only this transformation and bounded independent review of it may advance.
