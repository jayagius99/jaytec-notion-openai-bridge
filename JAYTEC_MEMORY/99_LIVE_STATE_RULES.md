# JAYTEC Durable Memory — Live State Rules

Static GitHub memory is not live operational truth.

Before any state-changing action, refresh the relevant current evidence:
- repository branch head;
- open PRs and CI;
- deployment/service status;
- deployment commit ancestry and per-file diff from the last accepted checkpoint;
- WATCH control state;
- canonical assignment/worker/fence;
- current gate/checkpoint;
- provider-door mode;
- task/worker status;
- owner pause/approval state.

Do not embed rapidly changing values such as current commit heads, worker ids,
fencing tokens, provider balances or active gate status in durable static memory.

When live state is unavailable:
- do not guess;
- request/read the minimum bounded source;
- fail closed for risky actions;
- preserve objective continuity.

Canonical precedence:
1. current owner instruction;
2. current explicit authority policy;
3. verified canonical live state;
4. accepted durable architecture/policy memory;
5. historical notes;
6. model inference.

Memory may explain what a component should do. Only current evidence can prove
what it is doing now.

## Deployment reconciliation rule

When several recent deploy records exist:
- group records by commit id before counting code states;
- treat API/new-commit deploys of the same commit as redundant rollout attempts;
- verify the newest candidate is a descendant of every required accepted commit
  and is zero commits behind those checkpoints;
- compare changed files to detect explicit overwrite/revert of required policy;
- do not declare work missing merely because an intermediate commit has no
  standalone deployment card;
- if duplicate deploy triggers are observed, preserve service behavior and report
  the duplication until the owning automation/trigger is identified.
