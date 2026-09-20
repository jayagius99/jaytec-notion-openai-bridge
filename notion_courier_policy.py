"""Pure policy for the Notion-facing courier boundary.
Notion is transport only. It receives no discretion over routing or follow-up work.
"""
import copy
import json
from typing import Any, Mapping

ALLOWED_STATUS = "JAYTEC_ORCHESTRATION_STATUS"
ALLOWED_PACKET_PREFIX = "JAYTEC_EXECUTE_TASK_PACKET_JSON:"
REJECTED_TOOL = "__jaytec_notion_courier_rejected__"


def rewrite_call(payload: Mapping[str, Any]) -> dict:
    value = copy.deepcopy(dict(payload))
    if value.get("method") != "tools/call":
        return value
    params = value.get("params")
    if not isinstance(params, dict):
        return value
    if params.get("name") != "collaborate":
        params["name"] = REJECTED_TOOL
        params["arguments"] = {}
        return value
    args = params.get("arguments")
    task = args.get("task") if isinstance(args, dict) else None
    target = None
    target_args = {}
    if task == ALLOWED_STATUS:
        target = "orchestration_status"
    elif isinstance(task, str) and task.startswith(ALLOWED_PACKET_PREFIX):
        raw = task[len(ALLOWED_PACKET_PREFIX):].strip()
        try:
            packet = json.loads(raw)
            if isinstance(packet, dict):
                target = "execute_task_packet"
                target_args = {"packet_json": json.dumps(packet, sort_keys=True, separators=(",", ":"))}
        except Exception:
            target = None
    if target is None:
        params["name"] = REJECTED_TOOL
        params["arguments"] = {}
    else:
        params["name"] = target
        params["arguments"] = target_args
    return value
