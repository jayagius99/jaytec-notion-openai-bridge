# JAYTEC Durable Memory — Repositories and System Evolution

## Primary repositories

### jaytec-notion-openai-bridge
Infrastructure/orchestration bridge lineage. Hosts provider adapters, Manus
runtime/governance, WATCH recovery ingress, specialist routing, security policy,
cognition components, Proving Grounds integration and staging runtime.

### jaytec-work-engine-v2-g1
Canonical work/execution-control repository for the current Forge master-program
and WATCH lane. Hosts WATCH workflow/driver/controller state, master gates,
continuation/evidence ledgers and Forge/ROOT/Genesis work artifacts.

### jaytec-system-status-desktop
Windows Admin/Status/Command Hub lineage. The long-term direction is a control
surface rather than a passive dashboard.

Repository names are durable project references, but branch heads, PR numbers,
deploy IDs and active gates are live state and must be refreshed before use.

## Historical architecture evolution

JAYTEC began with a stronger Notion-gateway emphasis and evolved toward a
durable multi-service control plane.

Important historical role changes:
- Notion became a bus/gateway rather than reasoning authority.
- Codex was the original engineering specialist role.
- SOL replaced Codex as the primary engineering/reasoning specialist while some
  wire keys may retain legacy names for compatibility.
- Gemini historically served research but is not part of the default
  WATCH/Manus assistance trio.
- Manus became the bounded asynchronous worker/automation specialist.
- WATCH became the canonical unattended supervision/recovery/traffic-control
  lane.
- DeepSeek became the adversarial/security reviewer.
- Nemo/Nemotron became a bounded secondary reasoning/engineering reviewer.
- Proving Grounds became a first-class zero-authority test resource.

## G1 and V2 lineage

A major pre-V2 focus was the G1 isolation/execution-fabric gate:
- prove reliable execution;
- fix provider/rate-limit routing;
- prevent duplicate workers;
- certify recovery/fencing/idempotency;
- finish reliability/self-diagnosis before resuming UI acceptance work.

V2 should not resume merely because a feature works in isolation. Large
infrastructure changes require system-wide reconciliation and evidence.

## V2 Admin/Command Hub

The V2 desktop/admin direction includes:
- system state and health;
- canonical task/gate state;
- worker/fence visibility;
- provider/specialist availability;
- command issuance;
- approvals/blocks;
- deployment/recovery visibility;
- audit trail;
- upgrade readiness;
- ability to initiate major bounded projects.

A passive "green/red dashboard" is insufficient. Jay wants an operational
command hub.

## Later evolution

V3+ direction includes:
- stronger self-upgrade workflow;
- Full Control Lab / Proving Grounds;
- isolated descendants and canary/rollback;
- reduced provider dependence;
- owner-controlled infrastructure;
- persistent learning;
- technical sovereignty.

Evolution must be evidence-gated and reversible where possible.
