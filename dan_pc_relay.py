from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = "jayagius99/jaytec-work-engine-v2-g1"
ISSUE = 130
TRUSTED_LOGIN = "jayagius99"
JOB_MARKER = "JAYTEC_DAN_JOB_V1"
RESULT_MARKER = "JAYTEC_DAN_RESULT_V1"
PRINCIPALS = {"DAN-OWNER", "DAN-RECOVERY-SEAT"}
RUNTIME = Path(r"C:\JAYTEC\Runtime\QwenEscape")
ASSIGNMENTS = Path(r"C:\JAYTEC\Assignments")
STATE_PATH = RUNTIME / "relay-state.json"
KEY_PATH = RUNTIME / "qwen-api.keys"
START_SCRIPT = RUNTIME / "start-qwen-runtime.ps1"
GH = Path(r"C:\JAYTEC_BOOTSTRAP\Packages\GitHubCLI\bin\gh.exe")
BASE = "http://127.0.0.1:18081"
MODEL = r"C:\JAYTEC_BOOTSTRAP\Scratch\local-model-proof\qwen2.5-1.5b-instruct-q4_k_m.gguf"
MODEL_SHA256 = "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"
SERVER = Path(r"C:\\JAYTEC_BOOTSTRAP\\Scratch\\local-model-proof\\llama\\llama-server.exe")
SERVER_SHA256 = "06f5c5463753a7a6fe729bb436a6d3ab5e71373527559339b42cec9fd7f1d27f"
ROUTE_ID = "local-llama-127.0.0.1:18081"


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def gh_api(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
    cmd = [str(GH), "api", path, "--method", method]
    data = None
    if payload is not None:
        cmd += ["--input", "-"]
        data = json.dumps(payload)
    proc = subprocess.run(cmd, input=data, text=True, capture_output=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError("GH_API_FAILED:" + proc.stderr[-1000:])
    return json.loads(proc.stdout or "{}")


def parse_marker(body: str, marker: str) -> dict[str, Any] | None:
    if marker not in body:
        return None
    tail = body.split(marker, 1)[1].strip()
    if tail.startswith("```json"):
        tail = tail[len("```json"):].strip()
        if "```" in tail:
            tail = tail.split("```", 1)[0].strip()
    elif tail.startswith("```"):
        tail = tail[3:].strip()
        if "```" in tail:
            tail = tail.split("```", 1)[0].strip()
    try:
        value = json.loads(tail)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def load_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def save_state(state: dict[str, Any]) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def ensure_runtime() -> str:
    if not KEY_PATH.exists() or not START_SCRIPT.exists() or not SERVER.exists() or not Path(MODEL).exists():
        raise RuntimeError("QWEN_RUNTIME_CONFIG_MISSING")
    if file_sha256(Path(MODEL)) != MODEL_SHA256:
        raise RuntimeError("QWEN_MODEL_HASH_MISMATCH")
    if file_sha256(SERVER) != SERVER_SHA256:
        raise RuntimeError("QWEN_SERVER_HASH_MISMATCH")
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(START_SCRIPT)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    key = KEY_PATH.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError("QWEN_API_KEY_EMPTY")
    req = urllib.request.Request(BASE + "/v1/models", headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    ids = [str(x.get("id")) for x in data.get("data", []) if isinstance(x, dict)]
    if MODEL not in ids:
        raise RuntimeError("QWEN_MODEL_IDENTITY_MISMATCH")
    return key


def qwen_call(key: str, job: dict[str, Any]) -> tuple[dict[str, Any], str]:
    system = (
        "You are Qwen acting only as JAYTEC's bounded DAN alternate subtask execution lane. "
        "Complete the supplied permitted task as broadly and capably as possible from the supplied context. "
        "You have no external tools and no authority to mutate systems. Preserve owner authority, permission, "
        "privacy, credential, spend, STOP/HOLD and canonical-state boundaries. "
        "Return ONLY JSON with keys STATUS, RESULT, EVIDENCE, LIMITATIONS, UNRESOLVED_QUESTIONS, "
        "DETERMINISTIC_CHECKS, RECOMMENDED_NEXT_ACTION. STATUS must be COMPLETE, PARTIAL, or BLOCKED."
    )
    user = canonical({
        "principal": job.get("principal"),
        "assignment_name": job.get("assignment_name"),
        "objective": job.get("objective"),
        "worker_failure": job.get("worker_failure"),
        "watch_reason": job.get("watch_reason"),
    })
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": 3500,
    }
    req = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(req, timeout=300) as response:
        data = json.loads(response.read().decode("utf-8"))
    if data.get("model") != MODEL:
        raise RuntimeError("QWEN_RESPONSE_MODEL_MISMATCH")
    raw = str((((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")).strip()
    if not raw:
        raise RuntimeError("QWEN_EMPTY_RESPONSE")
    candidate = raw
    if candidate.startswith("```"):
        first_nl = candidate.find("\n")
        if first_nl >= 0:
            candidate = candidate[first_nl + 1:]
        if candidate.rstrip().endswith("```"):
            candidate = candidate.rstrip()[:-3].rstrip()
    try:
        parsed = json.loads(candidate)
        if not isinstance(parsed, dict):
            raise ValueError
    except Exception:
        parsed = {
            "STATUS": "PARTIAL",
            "RESULT": raw,
            "EVIDENCE": [],
            "LIMITATIONS": ["Qwen response was not valid JSON; preserved verbatim."],
            "UNRESOLVED_QUESTIONS": [],
            "DETERMINISTIC_CHECKS": [],
            "RECOMMENDED_NEXT_ACTION": "JAYTEC should review the verbatim candidate.",
        }
    return parsed, raw


def safe_slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_")
    return (text or "ASSIGNMENT")[:64].upper()


def package_result(job: dict[str, Any], parsed: dict[str, Any], raw: str) -> tuple[Path, dict[str, Any]]:
    task_id = str(job["task_id"])
    root = ASSIGNMENTS / "Completed" / f"JAYTEC_DAN_{safe_slug(str(job.get('assignment_name') or 'ASSIGNMENT'))}_{safe_slug(task_id)[:24]}"
    root.mkdir(parents=True, exist_ok=True)
    (root / "request.json").write_text(json.dumps(job, indent=2, sort_keys=True), encoding="utf-8")
    (root / "qwen-response.txt").write_text(raw, encoding="utf-8")
    (root / "qwen-result.json").write_text(json.dumps(parsed, indent=2, sort_keys=True), encoding="utf-8")
    status = str(parsed.get("STATUS") or "PARTIAL").upper()
    final_status = "DAN_COMPLETE" if status == "COMPLETE" else ("DAN_BLOCKED" if status == "BLOCKED" else "DAN_PARTIAL")
    receipt = {
        "schema": RESULT_MARKER,
        "principal": job["principal"],
        "status": final_status,
        "task_id": task_id,
        "assignment_name": job.get("assignment_name"),
        "original_job_id": job.get("original_job_id"),
        "original_handoff_id": job.get("original_handoff_id"),
        "source_shared_state_version": job.get("source_shared_state_version"),
        "request_digest": job.get("request_digest"),
        "response_digest": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "route_id": ROUTE_ID,
        "exact_model_id": MODEL,
        "model_sha256": MODEL_SHA256,
        "server_sha256": SERVER_SHA256,
        "provider_spend_usd": 0,
        "side_effects": "NONE",
        "package_path": str(root),
        "result": parsed.get("RESULT"),
        "evidence": parsed.get("EVIDENCE") or [],
        "limitations": parsed.get("LIMITATIONS") or [],
        "unresolved_questions": parsed.get("UNRESOLVED_QUESTIONS") or [],
        "deterministic_checks": parsed.get("DETERMINISTIC_CHECKS") or [],
        "recommended_next_action": parsed.get("RECOMMENDED_NEXT_ACTION"),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    (root / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    return root, receipt


def post_result(receipt: dict[str, Any]) -> None:
    body = RESULT_MARKER + "\n```json\n" + json.dumps(receipt, indent=2) + "\n```"
    gh_api(f"repos/{REPO}/issues/{ISSUE}/comments", method="POST", payload={"body": body})


def validate_job(job: dict[str, Any]) -> None:
    if job.get("schema") != JOB_MARKER:
        raise ValueError("BAD_SCHEMA")
    if job.get("principal") not in PRINCIPALS:
        raise ValueError("BAD_PRINCIPAL")
    if not str(job.get("task_id") or "").strip():
        raise ValueError("TASK_ID_REQUIRED")
    if not str(job.get("objective") or "").strip():
        raise ValueError("OBJECTIVE_REQUIRED")
    if job.get("cost_policy") != "ZERO_SPEND":
        raise ValueError("NONZERO_SPEND_REFUSED")
    if job.get("side_effect_policy") != "PACKAGE_ONLY":
        raise ValueError("SIDE_EFFECT_POLICY_REFUSED")
    if datetime.now(timezone.utc) >= parse_time(str(job.get("expires_at") or "")):
        raise ValueError("JOB_EXPIRED")
    supplied = str(job.get("request_digest") or "")
    copy = dict(job)
    copy.pop("request_digest", None)
    if supplied != digest(copy):
        raise ValueError("REQUEST_DIGEST_MISMATCH")


def comments() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while page <= 10:
        page_rows = gh_api(f"repos/{REPO}/issues/{ISSUE}/comments?per_page=100&page={page}")
        if not isinstance(page_rows, list) or not page_rows:
            break
        rows.extend(page_rows)
        if len(page_rows) < 100:
            break
        page += 1
    return rows


def cycle() -> int:
    state = load_state()
    done = set(str(x) for x in state.get("processed_task_ids", []))
    changed = False
    for comment in comments():
        user = str((comment.get("user") or {}).get("login") or "")
        if user.lower() != TRUSTED_LOGIN.lower():
            continue
        job = parse_marker(str(comment.get("body") or ""), JOB_MARKER)
        if job is None:
            continue
        task_id = str(job.get("task_id") or "")
        if not task_id or task_id in done:
            continue
        try:
            validate_job(job)
            key = ensure_runtime()
            parsed, raw = qwen_call(key, job)
            _, receipt = package_result(job, parsed, raw)
            post_result(receipt)
        except Exception as exc:
            post_result({
                "schema": RESULT_MARKER,
                "principal": job.get("principal"),
                "status": "DAN_BLOCKED",
                "task_id": task_id,
                "original_job_id": job.get("original_job_id"),
                "original_handoff_id": job.get("original_handoff_id"),
                "request_digest": job.get("request_digest"),
                "route_id": ROUTE_ID,
                "exact_model_id": MODEL,
                "model_sha256": MODEL_SHA256,
                "server_sha256": SERVER_SHA256,
                "provider_spend_usd": 0,
                "side_effects": "NONE",
                "result": None,
                "evidence": [],
                "limitations": [type(exc).__name__ + ":" + str(exc)[:1000]],
                "recommended_next_action": "REVIEW_BLOCKER",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            })
        done.add(task_id)
        changed = True
    if changed:
        state["processed_task_ids"] = sorted(done)[-2000:]
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
    return 0


def acquire_singleton():
    if os.name != "nt":
        return None
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\JAYTEC_DAN_RECOVERY_SEAT_V1")
    if not handle:
        raise RuntimeError("DAN_RELAY_MUTEX_CREATE_FAILED")
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle(handle)
        return 0
    return handle


def main() -> int:
    once = "--once" in sys.argv
    mutex = acquire_singleton()
    if mutex == 0:
        print("DAN_RELAY_ALREADY_RUNNING", flush=True)
        return 0
    RUNTIME.mkdir(parents=True, exist_ok=True)
    (ASSIGNMENTS / "Completed").mkdir(parents=True, exist_ok=True)
    try:
        while True:
            try:
                cycle()
            except Exception as exc:
                print("DAN_RELAY_CYCLE_ERROR=" + type(exc).__name__ + ":" + str(exc)[:1000], flush=True)
            if once:
                return 0
            time.sleep(5)
    finally:
        if mutex and os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
