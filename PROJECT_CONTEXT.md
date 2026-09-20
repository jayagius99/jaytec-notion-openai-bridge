# JAYTEC shared project context

This repository is the controlled JAYTEC execution/specialist bridge. The historical repository name references Notion, but Notion is not the control plane and is not an execution authority.

Primary working principles:
- Build real, testable engineering outputs rather than simulations or pretend integrations.
- Prefer verified evidence over confident guesses.
- Keep source material, assumptions, findings, and validation status clearly separated.
- Preserve exact provider/model/profile identity; never silently substitute.
- Enforce task-scoped authority, cost controls, idempotency, and no-side-effect contracts.
- Fail closed when an approved route is unavailable instead of inventing a hidden fallback.
- Treat connector presence as capability only, never as authority.
- Keep production readiness scoped to the exact component being tested.

Authority / relationship boundary:
- Jay is the external owner.
- ChatGPT coordinates Jay's current instructions.
- JAYTEC is the controlled orchestration/control plane.
- Specialists return bounded findings/results.
- The Notion Agent is optional strict pass-through transport only when explicitly requested for the current task.
- The Notion Agent must not independently research, solve, route, spend, or invoke the production execution bridge.
- ROOT_OWNER and production-effect controls remain outside specialist/model authority.

Live task packets must carry the specific current context needed for the task. This file is a stable baseline only and is not evidence that any external system, credential, source, or production gate is currently available.
