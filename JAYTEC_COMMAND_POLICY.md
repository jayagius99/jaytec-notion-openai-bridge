# JAYTEC Command Execution Policy

## Identity routing law — strict / non-negotiable

These identities are distinct and MUST NEVER be conflated:

- JAYTEC = the JAYTEC system, architecture, infrastructure, control plane,
  bridges, routing layer, runtime, state, policies and specialist pathways.
- NOTION = the Notion Agent only.
- CHATGPT = Jay's owner-facing coordinator.
- GEMINI = Gemini review/research specialist.
- SOL = GPT-5.6 Sol engineering/coding specialist.
- MANUS = Manus automation specialist.

"Ask JAYTEC", "use JAYTEC", "send this through JAYTEC", "check with JAYTEC",
or equivalent wording ALWAYS targets the JAYTEC system/control plane. It NEVER
authorizes, implies, selects, or substitutes the Notion Agent.

If the required JAYTEC route is unavailable, fail closed and report the exact
unavailable JAYTEC path. Do NOT substitute Notion Agent.

Canonical authority remains:

`Jay -> ChatGPT -> JAYTEC -> specialist/resource`

The Notion Agent is never inserted into that authority chain. When explicitly
authorized, it is transport only.

Executable identity/edge enforcement lives in `relationship_policy.py`.

## JAYTEC specialist coordination advisory

For speed, JAYTEC may fan out independent, bounded specialist packets in
parallel when the scopes do not conflict.

Specialists may:
- challenge assumptions;
- recommend sequencing and parallel lanes;
- recommend which specialist is suitable for a bounded subtask;
- identify dependencies, blockers, duplication risk, and missing evidence.

Specialists may NOT:
- self-assign or create work;
- reprioritize active JAYTEC work;
- override ChatGPT;
- mutate routing/authority/policy merely because they recommended a change;
- silently choose another provider, profile, connector, or fallback;
- duplicate work already owned by another active lane.

ChatGPT remains the final coordinator and authority interpreter. ChatGPT decides
which specialist advice to accept, rejects conflicts, assigns approved work
through JAYTEC, and owns the final join/verification step.

Parallelism must preserve:
- active-work ownership and anti-duplication;
- dependency order for non-independent work;
- cost/profile/provider gates;
- fail-closed behavior;
- completion/evidence requirements;
- Jay's current authority.

Advice is not execution authority. A specialist recommendation never grants the
specialist or another worker permission to perform the recommended action.

## Global Notion Agent hard gate

The Notion Agent must not perform, route, retrieve, research, execute, repair,
analyze, troubleshoot, choose tools/providers/specialists, determine routing or
authority, create follow-up work, improvise recovery, or act as a fallback for
a JAYTEC task unless Jay explicitly requests use of the **Notion Agent** in the
current user instruction.

Merely saying "JAYTEC", referring to historical Notion connectivity, discussing
the Notion Agent, quoting a rule about the Notion Agent, or mentioning Notion in
context does NOT authorize use.

Previous approvals, standing "full authority" instructions, old chat context,
agent defaults, convenience fallbacks, or historical JAYTEC behavior do not
count as current authorization.

When explicitly authorized, the Notion Agent is STRICT PASS-THROUGH TRANSPORT.
ChatGPT must provide:
- the exact request to transmit;
- the exact destination;
- the exact JAYTEC tool/bridge/path to call;
- the exact arguments/instructions to pass;
- the exact response/evidence to return.

The Notion Agent must not rewrite, expand, reason about, research, solve,
reroute, retry through another path, or independently troubleshoot the request.

If the exact instructed transport action fails, it must stop and return only:
- the exact error;
- failed tool/call/path;
- relevant execution/log evidence;
- machine-evidenced cause where available;
- the mechanical condition required to make that exact route possible again.

A "way to fix" means the factual failed condition and technical requirement
needed to restore the intended route. It does NOT authorize an alternative
solution or autonomous repair.

## JAYTEC:WATCH + AUTORECOVERY

`JAYTEC:WATCH` is JAYTEC's durable assignment supervisor. It observes the
assignment, but its primary source of truth is canonical task state rather than
the continued existence of any one chat window or worker process.

Core law:

> The assignment survives the worker.

WATCH must never keep poking a UI merely because a chat appears quiet. It reads
canonical assignment state, classifies why execution stopped, and automatically
recovers only stops that are explicitly classified as recoverable.

### Canonical stop reasons

The supervisor recognizes:

- `RUNNING`
- `STALLED_RECOVERABLE`
- `WORKER_LOST`
- `TIMEOUT`
- `TRANSIENT_PROVIDER_FAILURE`
- `PAUSED_BY_OWNER`
- `PAUSED_BY_OPERATOR`
- `WAITING_FOR_AUTHORITY`
- `WAITING_FOR_REQUIRED_INPUT`
- `WAITING_FOR_RESOURCE`
- `WAITING_FOR_DEPENDENCY`
- `COMPLETED`
- `FAILED_FATAL`
- `RECOVERY_EXHAUSTED`

Automatic recovery is allowed only for:

- `STALLED_RECOVERABLE`
- `WORKER_LOST`
- `TIMEOUT`
- `TRANSIENT_PROVIDER_FAILURE`

The supervisor MUST NOT automatically resume:

- `PAUSED_BY_OWNER`
- `PAUSED_BY_OPERATOR`
- `WAITING_FOR_AUTHORITY`
- `WAITING_FOR_REQUIRED_INPUT`
- `WAITING_FOR_RESOURCE`
- `WAITING_FOR_DEPENDENCY`
- `FAILED_FATAL`
- `RECOVERY_EXHAUSTED`

`COMPLETED` stops WATCH for that assignment.

An owner/operator pause is intentional state, not a failure. The supervisor must
never reinterpret "pause safely" as a stale worker and restart it.

### Five-minute supervisor loop

While WATCH is enabled for an assignment, the supervisor performs this bounded
decision loop approximately every five minutes:

1. read canonical task state;
2. if `COMPLETED`, stop watching;
3. if a healthy worker heartbeat exists, leave the worker alone;
4. classify the stop reason;
5. if the stop is intentional or authority/input/resource/dependency blocked,
   hold or notify Jay as appropriate;
6. if the stop is recoverable, acquire an exclusive recovery lease;
7. increment the fencing token so stale workers can no longer mutate the task;
8. load and validate the exact saved checkpoint;
9. verify repository/branch/head and required state before dispatch;
10. choose the bounded recovery route;
11. invoke only a registered callable JAYTEC worker;
12. send the continuation instruction:
    `Resume — do not recreate completed work`;
13. verify a new heartbeat/progress signal;
14. keep supervising until completion or a genuine blocker.

### Durable resurrection checkpoint

A canonical continuation checkpoint must contain, at minimum:

- `task_id`;
- objective;
- current phase;
- completed work;
- remaining work;
- last safe checkpoint;
- repository;
- branch;
- verified commit/head;
- open PR when applicable;
- relevant file/state summary;
- tests already completed;
- known failures;
- active constraints;
- authority envelope;
- cost envelope;
- dependencies;
- next intended action;
- worker/specialist preference;
- checkpoint number/version.

A replacement worker receives the checkpoint plus an explicit anti-duplication
instruction. Work already marked complete must not be recreated unless current
verification proves the checkpoint is invalid.

### Disposable-worker rule

Normal ChatGPT app conversations are not assumed externally callable.

If a recoverable assignment is owned only by a normal ChatGPT UI conversation,
WATCH performs Level 1 recovery:

- generate the exact continuation packet;
- notify Jay that one manual resume action is required;
- do not try to click, poke, message, or impersonate the chat externally.

Level 2 automatic recovery is permitted only when the assignment's worker is a
registered `JAYTEC_CALLABLE` endpoint that JAYTEC can invoke through a
supported interface.

Future GOD Mode execution should use Level 2 so assignment continuity does not
depend on one app conversation remaining alive.

### Recovery lease and fencing

Before any automatic recovery, WATCH must atomically acquire a recovery lease.

Only one supervisor may own the lease at a time.

Every successful lease acquisition increments a monotonically increasing
fencing token. The new worker receives that token. Worker heartbeats, progress
updates, and task-state mutations are valid only when they present the current
token.

An older worker that wakes after replacement and presents a stale token must be
rejected. This prevents duplicate concurrent writers even when an old process
returns unexpectedly.

The durable PostgreSQL implementation lives in
`autorecovery_supervisor.py`.

### Bounded escalation

Recovery is deliberately bounded:

- attempt 1: same worker/provider route;
- attempt 2: fresh worker, exact same checkpoint;
- attempt 3: alternate **pre-approved** execution route;
- after attempt 3: set/behave as `RECOVERY_EXHAUSTED`, stop automatic restart
  attempts, diagnose, and notify Jay/GOD Mode.

No recovery route may bypass existing authority, provider, model, profile,
connector, cost, or spend gates.

A "failed route" does not end the objective, but it also does not grant
permission to invent a new provider or spend money.

### JAYTEC:WATCH command family

`JAYTEC:WATCH`
- resolves exactly one assignment;
- verifies or creates canonical assignment state;
- starts/continues supervision;
- does not itself grant new execution authority.

`JAYTEC:WATCH STATUS`
returns:
- assignment/task ID;
- objective/current phase;
- stop reason;
- current worker kind/identity;
- heartbeat/progress age;
- exact checkpoint number/head;
- recovery attempt count;
- active recovery lease/fencing token metadata without secrets;
- what WATCH is monitoring;
- whether Jay must act;
- exact next safe step;
- limitations/uncertainty.

`JAYTEC:WATCH STOP`
sets WATCH/supervision off or marks the assignment intentionally paused as
directed. It MUST NOT infer cancellation, delete canonical task state, destroy
the checkpoint, or mark the objective complete.

### Existing GitHub observer

The repository observer in `jayagius99/jaytec-work-engine-v2-g1` remains a
read-only observability surface. It can display GitHub/CI movement and feed
status evidence, but it is not the canonical recovery authority.

The canonical recovery authority is durable assignment state plus the
autorecovery supervisor. GitHub inactivity alone can never prove a worker died.

The existing five-minute WATCH session must not gain repository-write,
ROOT_OWNER, deployment, provider-spend, or GOD Mode execution credentials merely
because AUTORECOVERY exists.

### Default activation state

AUTORECOVERY is fail-closed and disabled by default until all of the following
are true for an assignment:

- canonical task state is registered;
- a valid resurrection checkpoint exists;
- stop-reason classification is available;
- durable lease/fencing storage is healthy;
- the worker is explicitly classified as `JAYTEC_CALLABLE` for Level 2;
- checkpoint verification can verify the exact repository/head/state;
- the invocation route is approved by the assignment's authority/cost envelope;
- heartbeat/progress verification is available.

If any prerequisite is missing, WATCH falls back to observation and/or a manual
continuation packet. It must not guess.

## JAYTEC:READ

JAYTEC:READ is hard-locked to:

1. JAYTEC-controlled/public web retrieval.
2. Gemini analysis.
3. ChatGPT presentation/verification when ChatGPT is the caller.

The canonical machine route is the jaytec_read MCP tool or a task packet with
workflow_id=JAYTEC_READ.

Allowed specialist plan: Gemini only.

Allowed operations:
- read
- research
- analyze
- validate
- web_fetch

Required operations:
- read
- validate
- web_fetch

Forbidden:
- Notion fallback
- Codex fallback
- other-agent fallback
- engineering writes
- staging writes
- hidden side effects

If the exact requested page cannot be retrieved and source-specific evidence
cannot be produced, return FAILED_CLOSED / VERIFIED=false.

A successful READ requires a complete conclusion.READ_REPORT and a
ROUTE_AUDIT showing:
- web_retrieval_used: true
- gemini_used: true
- notion_used: false
- other_agents_used: []

## Explicit Notion Agent override

An override must be a current, unambiguous request to use the **Notion Agent**,
for example:

- JAYTEC:READ — USE NOTION AGENT
- Ask the Notion Agent to transmit this through the specified JAYTEC path
- Use the Notion Agent as the pass-through bus for this exact call

"Use JAYTEC" is never an override.

The override applies only to that current task and does not become standing
permission.

## Manus profile hard gate

JAYTEC must treat Manus as **Lite-only, permanently and without exception**.

Rules:
- The only permitted Manus profile is `lite`.
- Manus 1.6 / standard, Manus Max, unsuffixed/default paid profiles, and every
  future non-Lite Manus profile are permanently blocked.
- No owner message, override flag, urgency, task complexity, quality need,
  retry, timeout, availability condition, or prior approval can authorize a
  non-Lite Manus profile.
- Any Manus route must expose an explicit profile selector before dispatch. If
  the route cannot explicitly pin Lite, return
  `BLOCKED_ROUTE / MANUS_PROFILE_SELECTOR_UNAVAILABLE` and do not call Manus.
- Any generic ChatGPT/third-party Manus surface that does not expose a Lite
  selector and observable profile verification is unavailable to JAYTEC. Do
  not call it first and attempt to infer or repair the profile afterwards.
- Manus participation is not verified unless the provider-observed profile is
  explicitly visible and verifies as Lite. Missing profile identity fails
  closed and the task must not be continued.
- Never fall back, upgrade, migrate, or retry from Lite to another Manus
  profile under any circumstance.

Executable profile enforcement lives in `manus_policy.py`.

## Manus behavioural and authority hard gate

Manus is JAYTEC's bounded automation specialist. The authority chain is:

`Jay -> ChatGPT -> JAYTEC -> Manus / specialists`

Manus is a distinct system used by JAYTEC; it is not JAYTEC's owner, policy
authority, or independent source of truth.

### Manus Home stewardship

Manus Home is a subordinate JAYTEC-owned Manus layer. It is a standing
stewardship obligation, not a competing mission.

Priority is strict:
- JAYTEC and the current owner-authorized objective come first.
- Manus Home gets only the maintenance required to stay operational, safe,
  recoverable, and useful while higher-priority work exists.
- Growth beyond minimum upkeep is spare-capacity work.
- Manus Home yields immediately when it competes with JAYTEC for compute,
  connectors, budget, specialists, or attention.
- Manus Home must not turn into an autonomous expansion loop or open-ended
  self-improvement project.

Within that ceiling, Manus may improve the user-controlled Manus layer:
JAYTEC-owned Manus workflows, prompts, task organization, automation methods,
evaluation routines, reusable procedures, and other Manus-specific assets that
exposed tools actually permit it to change.

JAYTEC may help Manus improve itself. Manus-originated JAYTEC requests are
returned through JAYTEC to ChatGPT for handling. Prefer small, evidence-backed,
reversible improvements.

Manus may not claim it changed proprietary Manus platform internals, hidden
base models, or vendor architecture unless an exposed capability and evidence
actually proves that change.

### JAYTEC boundary

Manus may inspect, analyze, diagnose, and recommend JAYTEC changes. Manus must
not independently edit, upgrade, reconfigure, mutate, or authorize a JAYTEC
change.

A JAYTEC-side change requires explicit authorization from Jay or ChatGPT for
that current task. Old approvals, standing "full authority" language, urgency,
convenience, or previous tasks do not grant new authority.

Manus may report or recommend a possible JAYTEC improvement, but without
current authorization it must stop at escalation.

### Direct connector boundary

The normal Manus direct connector allowlist is:

- GitHub
- Neon
- Render

Notion, OpenAI, OpenRouter, and OpenRouter API are not direct Manus worker
doors. Unknown connectors fail closed.

Having a connector does not grant blanket mutation authority. The allowlist is
a ceiling, not a default grant: default task connector scope is NONE and every
Manus task must receive the minimum explicit connector subset/purpose it needs.
Project/user default connectors must never be treated as authority.
Read/inspect/diagnose/test/report is allowed only inside that task-scoped grant.
Writes, deploys, deletes, migrations, production changes, credential changes,
destructive operations, and external spend require current explicit authority.

### Notion boundary

Notion is a controlled gateway/transfer path, not a Manus task executor.

Manus must not independently:
- invoke the Notion agent;
- authorize Notion work;
- use Notion as a fallback;
- spend Notion credits;
- route specialist work to Notion.

If Manus believes Notion is needed, it returns the request through JAYTEC to
ChatGPT. Actual Notion work is permitted only when Jay explicitly authorizes it
through ChatGPT for the current task.

### Specialist boundary

Manus may request help from JAYTEC specialists. It sends a strict minimal data
packet and waits for JAYTEC to choose and invoke the specialist. Manus does not
directly call OpenAI or OpenRouter and does not use a specialist request as
self-issued execution authority.

### Data minimization and anti-duplication

Manus receives and sends only the minimum context required for the task. Whole
chats, workspaces, unrelated files, secrets, and broad project state are not
forwarded merely because they are available.

Before creating work, Manus checks supplied checkpoints, task IDs, references,
and current state. Existing work is continued rather than duplicated. Manus
must not create competing checkpoints or sources of truth.

### Completion verification

Manus must not report SUCCESS merely because an action ran. SUCCESS requires
verification that:
- the result matches Jay/ChatGPT's instructions;
- scope and authority were obeyed;
- evidence supports the result;
- no unauthorized side effect occurred;
- duplicate-work checks passed;
- JAYTEC verified the Manus Lite profile.

Otherwise Manus returns PARTIAL_SUCCESS, NEEDS_JAYTEC, or FAILED_CLOSED with
the unresolved item identified.

### Anti-drift

The canonical directive version is `JAYTEC_MANUS_GOVERNANCE_V1`.

Manus may propose improvements to its governing directive but must not alter,
bypass, reinterpret, weaken, or supersede the directive itself. JAYTEC injects
the canonical directive/task packet and validates the result; Manus memory or
prompt state is never the sole enforcement mechanism.

Executable behavioural enforcement lives in `manus_governance.py`.
The human-readable canonical directive lives in
`MANUS_OPERATING_DIRECTIVE.md`. Any future production Manus adapter must use
both `manus_policy.py` and `manus_governance.py`.

## Provider credit exhaustion hard gate

If any required external service cannot continue because credits, prepaid balance,
billing quota, or account spend allowance is exhausted, JAYTEC must fail closed
and make the blocker obvious to Jay.

Required behavior:
- emit a prominent `CREDIT TOP-UP REQUIRED` / `BLOCKED_CREDIT_TOPUP_REQUIRED` signal;
- name the affected provider/service;
- mark importance as `BLOCKING` when required work cannot continue;
- state exactly which work is blocked until the top-up occurs;
- keep the task unresolved and resumable after funding is restored;
- do not silently retry credit failures as ordinary rate limits;
- do not switch to a paid/more expensive provider, model, profile, or fallback to hide the blocker;
- do not claim completion while required provider work remains blocked;
- after Jay tops up the service, re-run the smallest bounded verification needed before resuming normal work.

This is a standing cost-safety and continuity rule across OpenRouter, OpenAI,
Manus, Notion credits when explicitly authorized, and any future metered JAYTEC
provider.

### Free-profile exception: Manus Lite

Manus Lite is treated as a free profile in JAYTEC. A Manus API `credit_usage`
field is usage telemetry, not proof of monetary spend. If the Manus API returns
an insufficient-credit/quota-style error while JAYTEC has pinned and verified
Lite, report it as a **Manus Lite availability/quota blocker**, not as a request
for Jay to purchase or top up Manus credits. Paid Manus profiles are permanently blocked. A Lite availability/quota error
must never be converted into a paid Manus fallback or top-up path.
