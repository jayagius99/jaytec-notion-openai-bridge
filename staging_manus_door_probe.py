"""STAGING ONLY: harmless authenticated JAYTEC -> Manus door probe.

Read-only by design. Discovers the reserved MANUS project and candidate tasks but
never sends a message, starts a task, changes project state, or prints secrets.
"""
from __future__ import annotations

import json

from manus_adapter import ManusClient, ManusError, safe_identity_summary


def main() -> int:
    summary: dict[str, object] = {"event": "JAYTEC_MANUS_DOOR_PROBE", "status": "FAIL"}
    try:
        client = ManusClient()
        me = client.user_me()
        projects = client.list_projects()
        project_rows = projects.get("data") if isinstance(projects.get("data"), list) else []
        matches = [p for p in project_rows if isinstance(p, dict) and str(p.get("name", "")).strip().casefold() == "manus"]
        summary["auth"] = safe_identity_summary(me)
        summary["project_match_count"] = len(matches)
        if len(matches) == 1:
            project = matches[0]
            project_id = str(project.get("id", ""))
            tasks = client.list_tasks(project_id=project_id, limit=100)
            task_rows = tasks.get("data") if isinstance(tasks.get("data"), list) else []
            summary["manus_project"] = {"id": project_id, "name": project.get("name")}
            summary["task_count"] = len(task_rows)
            summary["tasks"] = [
                {
                    "id": row.get("id"),
                    "title": row.get("title"),
                    "status": row.get("status"),
                    "task_type": row.get("task_type"),
                    "credit_usage": row.get("credit_usage"),
                    "task_url": row.get("task_url"),
                }
                for row in task_rows[:20]
                if isinstance(row, dict)
            ]
            summary["status"] = "PASS"
        elif len(matches) == 0:
            summary["error"] = "MANUS_PROJECT_NOT_FOUND"
        else:
            summary["error"] = "MANUS_PROJECT_AMBIGUOUS"
    except ManusError as exc:
        summary["error"] = str(exc)
    except Exception as exc:
        summary["error"] = type(exc).__name__
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0 if summary["status"] == "PASS" else 4


if __name__ == "__main__":
    raise SystemExit(main())
