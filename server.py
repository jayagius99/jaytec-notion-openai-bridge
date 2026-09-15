import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from openai import OpenAI

# --- Runtime configuration (NO secrets in code) ---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-sol").strip()

# Dedicated Codex engineering route (must be explicitly configured).
# IMPORTANT: We do NOT assume a model ID. You must set this in hosting env.
CODEX_MODEL = os.environ.get("CODEX_MODEL", "").strip()

MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "8000"))

# Timeouts / retries (bounded)
OPENAI_TIMEOUT_S = float(os.environ.get("OPENAI_TIMEOUT_S", "45"))
OPENAI_MAX_RETRIES = int(os.environ.get("OPENAI_MAX_RETRIES", "2"))

BRIDGE_ID_CODEX = "BRIDGE_CODEX_ENGINEERING"

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set.")
if not MCP_AUTH_TOKEN:
    raise RuntimeError(
        "MCP_AUTH_TOKEN is not set. Refusing to start an unauthenticated remote MCP server."
    )

client = OpenAI(api_key=OPENAI_API_KEY)

project_context_path = Path(__file__).with_name("PROJECT_CONTEXT.md")
PROJECT_CONTEXT = (
    project_context_path.read_text(encoding="utf-8")
    if project_context_path.exists()
    else ""
)

auth = StaticTokenVerifier(
    tokens={
        MCP_AUTH_TOKEN: {
            "sub": "notion-agent",
            "client_id": "jaytec-notion-openai-bridge",
        }
    }
)

mcp = FastMCP("JAYTEC OpenAI Engineering Bridge", auth=auth)

BASE_INSTRUCTIONS = """You are the OpenAI engineering peer connected to a Notion AI agent through a private MCP bridge.

Your job is to improve accuracy and usefulness, not merely agree with the other AI.

Rules:
- Treat supplied project context and evidence as primary inputs.
- Distinguish verified facts, strong inferences, and hypotheses.
- Identify contradictions and missing evidence.
- When reviewing another AI's answer, preserve correct parts and explicitly correct unsupported or incorrect parts.
- Give actionable technical answers.
- Never claim to have accessed the user's ChatGPT conversation, account memory, local machine, Notion workspace, or files unless that content is explicitly supplied in the current tool call.
- This bridge reaches an OpenAI API model, not a live ChatGPT chat session.
"""


@dataclass
class BridgeError:
    bridge_id: str
    task_id: Optional[str]
    subtask_id: Optional[str]
    error_class: str
    http_or_provider_status: str
    retryable: str
    message: str
    safe_technical_detail: str
    suggested_action: str
    timestamp: str
    raw_reference: str

    def to_text_block(self) -> str:
        # Plain-text contract (Notion-friendly)
        lines = [
            "BRIDGE_ERROR:",
            f"BRIDGE_ID: {self.bridge_id}",
            f"TASK_ID: {self.task_id or ''}",
            f"SUBTASK_ID: {self.subtask_id or ''}",
            f"ERROR_CLASS: {self.error_class}",
            f"HTTP_OR_PROVIDER_STATUS: {self.http_or_provider_status}",
            f"RETRYABLE: {self.retryable}",
            f"MESSAGE: {self.message}",
            f"SAFE_TECHNICAL_DETAIL: {self.safe_technical_detail}",
            f"SUGGESTED_ACTION: {self.suggested_action}",
            f"TIMESTAMP: {self.timestamp}",
            f"RAW_REFERENCE: {self.raw_reference}",
        ]
        return "\n".join(lines)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _classify_error(exc: Exception) -> Dict[str, str]:
    # Keep conservative and safe.
    msg = str(exc)
    lower = msg.lower()
    if "401" in lower or "unauthorized" in lower or "api key" in lower:
        return {
            "error_class": "AUTH_ERROR",
            "retryable": "NO",
            "suggested_action": "Check OPENAI_API_KEY and provider account access.",
        }
    if "429" in lower or "rate" in lower and "limit" in lower:
        return {
            "error_class": "RATE_LIMIT",
            "retryable": "YES",
            "suggested_action": "Back off and retry later; consider increasing quota.",
        }
    if "timeout" in lower:
        return {
            "error_class": "TIMEOUT",
            "retryable": "YES",
            "suggested_action": "Retry with increased OPENAI_TIMEOUT_S or smaller request.",
        }
    return {
        "error_class": "UPSTREAM_PROVIDER_ERROR",
        "retryable": "UNKNOWN",
        "suggested_action": "Inspect server logs and provider status; retry if transient.",
    }


def _call_openai(model: str, task: str) -> str:
    last_exc: Optional[Exception] = None
    for attempt in range(0, max(1, OPENAI_MAX_RETRIES + 1)):
        try:
            response = client.responses.create(
                model=model,
                instructions=BASE_INSTRUCTIONS,
                input=task,
                timeout=OPENAI_TIMEOUT_S,
            )
            return response.output_text
        except Exception as exc:
            last_exc = exc
            # bounded retry with small backoff
            if attempt >= OPENAI_MAX_RETRIES:
                break
            time.sleep(0.75 * (attempt + 1))
    raise last_exc or RuntimeError("Unknown OpenAI error")


# ---------------- Existing generic tools (kept) ----------------

@mcp.tool
def ask_openai(question: str, context: str = "") -> str:
    prompt = f"""PROJECT CONTEXT:\n{PROJECT_CONTEXT}\n\nCONTEXT FROM NOTION:\n{context}\n\nQUESTION:\n{question}\n\nProduce a self-contained answer. Clearly mark uncertainty where appropriate."""
    return _call_openai(OPENAI_MODEL, prompt)


@mcp.tool
def review_notion_answer(question: str, notion_answer: str, context: str = "") -> str:
    prompt = f"""PROJECT CONTEXT:\n{PROJECT_CONTEXT}\n\nCONTEXT FROM NOTION:\n{context}\n\nORIGINAL USER QUESTION:\n{question}\n\nNOTION AI DRAFT:\n{notion_answer}\n\nAct as an independent senior reviewer.\n1. Check factual and technical correctness.\n2. Find unsupported assumptions, omissions, contradictions, and unsafe shortcuts.\n3. Preserve correct content.\n4. Produce a corrected final answer Notion AI can use.\n"""
    return _call_openai(OPENAI_MODEL, prompt)


@mcp.tool
def collaborate(task: str, notion_analysis: str = "", context: str = "") -> str:
    prompt = f"""PROJECT CONTEXT:\n{PROJECT_CONTEXT}\n\nCONTEXT FROM NOTION:\n{context}\n\nTASK:\n{task}\n\nNOTION AI CURRENT ANALYSIS:\n{notion_analysis}\n\nWork as the second engineering agent.\nChallenge mistakes instead of automatically agreeing.\nReturn:\n- what appears correct,\n- what needs correction or verification,\n- the strongest improved solution,\n- any concrete next checks or tests.\n"""
    return _call_openai(OPENAI_MODEL, prompt)


@mcp.tool
def bridge_status() -> str:
    return f"JAYTEC Notion/OpenAI bridge is online. OpenAI model: {OPENAI_MODEL}"


# ---------------- Codex engineering bridge surface ----------------

@mcp.tool
def codex_health_check() -> str:
    """Basic reachability + configuration sanity check for the Codex bridge surface."""
    # We don't call upstream here (cheap, no spend). Just validate config presence.
    configured = "YES" if CODEX_MODEL else "NO"
    return "\n".join(
        [
            f"BRIDGE_ID: {BRIDGE_ID_CODEX}",
            "HEALTH: OK",
            f"CODEX_MODEL_CONFIGURED: {configured}",
        ]
    )


@mcp.tool
def codex_get_status() -> str:
    """Return bridge status including current configured model IDs (no secrets)."""
    return "\n".join(
        [
            f"BRIDGE_ID: {BRIDGE_ID_CODEX}",
            f"DEFAULT_MODEL: {OPENAI_MODEL}",
            f"CODEX_MODEL: {CODEX_MODEL or ''}",
            f"TIMEOUT_S: {OPENAI_TIMEOUT_S}",
            f"MAX_RETRIES: {OPENAI_MAX_RETRIES}",
            f"TIMESTAMP: {_now_iso()}",
        ]
    )


@mcp.tool
def codex_capability_check() -> str:
    """Minimal upstream check that CODEX_MODEL is callable.

    This is an actual upstream call and may consume tokens.
    """
    if not CODEX_MODEL:
        return BridgeError(
            bridge_id=BRIDGE_ID_CODEX,
            task_id=None,
            subtask_id=None,
            error_class="INVALID_REQUEST",
            http_or_provider_status="",
            retryable="NO",
            message="CODEX_MODEL is not set.",
            safe_technical_detail="Set CODEX_MODEL in the hosting environment.",
            suggested_action="Configure CODEX_MODEL to the exact GPT-5.3 Codex model identifier you intend to use.",
            timestamp=_now_iso(),
            raw_reference="CONFIG_MISSING",
        ).to_text_block()

    prompt = "Respond with: CAPABILITY_OK"  # harmless
    try:
        out = _call_openai(CODEX_MODEL, prompt)
        # Fail-closed: require the marker.
        if "CAPABILITY_OK" not in out:
            return BridgeError(
                bridge_id=BRIDGE_ID_CODEX,
                task_id=None,
                subtask_id=None,
                error_class="CONTRACT_VIOLATION",
                http_or_provider_status="",
                retryable="UNKNOWN",
                message="Capability check response did not include expected marker.",
                safe_technical_detail="Upstream responded but did not follow the expected format.",
                suggested_action="Retry; if persistent, adjust model/instructions or use a stricter system prompt.",
                timestamp=_now_iso(),
                raw_reference="CAPABILITY_MARKER_MISSING",
            ).to_text_block()
        return "\n".join(
            [
                f"BRIDGE_ID: {BRIDGE_ID_CODEX}",
                "CAPABILITY: PASS",
                f"MODEL: {CODEX_MODEL}",
                f"TIMESTAMP: {_now_iso()}",
            ]
        )
    except Exception as exc:
        cls = _classify_error(exc)
        return BridgeError(
            bridge_id=BRIDGE_ID_CODEX,
            task_id=None,
            subtask_id=None,
            error_class=cls["error_class"],
            http_or_provider_status="",
            retryable=cls["retryable"],
            message="Upstream call failed during capability check.",
            safe_technical_detail=str(exc)[:300],
            suggested_action=cls["suggested_action"],
            timestamp=_now_iso(),
            raw_reference="CAPABILITY_UPSTREAM_ERROR",
        ).to_text_block()


def _extract_required(task_package: str, key: str) -> str:
    # Simple line-based parser: KEY: value
    # We preserve the original package and fail closed if TASK_ID/SUBTASK_ID missing.
    for line in task_package.splitlines():
        if line.strip().startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return ""


@mcp.tool
def codex_send_task(task_package: str) -> str:
    """Send a structured engineering task package to the configured Codex model.

    Expects TASK_ID and SUBTASK_ID in the task_package.
    Returns a normalized response contract block.
    """
    task_id = _extract_required(task_package, "TASK_ID")
    subtask_id = _extract_required(task_package, "SUBTASK_ID")

    if not task_id or not subtask_id:
        return BridgeError(
            bridge_id=BRIDGE_ID_CODEX,
            task_id=task_id or None,
            subtask_id=subtask_id or None,
            error_class="INVALID_REQUEST",
            http_or_provider_status="",
            retryable="NO",
            message="TASK_ID and SUBTASK_ID are required.",
            safe_technical_detail="Missing TASK_ID or SUBTASK_ID fields in task_package.",
            suggested_action="Include TASK_ID: and SUBTASK_ID: lines in the request.",
            timestamp=_now_iso(),
            raw_reference="MISSING_IDS",
        ).to_text_block()

    if not CODEX_MODEL:
        return BridgeError(
            bridge_id=BRIDGE_ID_CODEX,
            task_id=task_id,
            subtask_id=subtask_id,
            error_class="MODEL_UNAVAILABLE",
            http_or_provider_status="",
            retryable="NO",
            message="CODEX_MODEL is not set; cannot route to Codex.",
            safe_technical_detail="Set CODEX_MODEL env var on the bridge host.",
            suggested_action="Configure CODEX_MODEL to the intended GPT-5.3 Codex model ID and redeploy.",
            timestamp=_now_iso(),
            raw_reference="CODEX_MODEL_UNSET",
        ).to_text_block()

    # Prompt: preserve structured fields and force explicit contract output.
    prompt = f"""You are ROLE: GPT-5.3 CODEX ENGINEERING.

You MUST:
- Preserve TASK_ID and SUBTASK_ID exactly.
- If you need external research, return one or more RESEARCH_REQUEST blocks (as specified below).
- Return a single RESPONSE_CONTRACT block.

TASK_PACKAGE:
{task_package}

RESPONSE_CONTRACT FORMAT (plain text, include all keys; leave blank if not applicable):
TASK_ID:
SUBTASK_ID:
ROLE: GPT-5.3 CODEX ENGINEERING
STATUS:
IMPLEMENTATION_OBJECTIVE:
PROJECT_STATE_BEFORE:
REPOSITORY_OR_TARGET:
FILES_INSPECTED:
FILES_CHANGED:
FILES_CREATED:
FILES_REMOVED:
DEPENDENCIES_CHANGED:
IMPLEMENTATION_SUMMARY:
ARCHITECTURAL_CHANGES:
KEY_CODE_CHANGES:
GEMINI_RESEARCH_USED:
RESEARCH_ASSUMPTIONS:
TESTS_PERFORMED:
TEST_RESULTS:
VALIDATION_RESULTS:
REGRESSIONS_CHECKED:
KNOWN_ISSUES:
OPEN_QUESTIONS:
BLOCKERS:
RESEARCH_REQUESTS:
FURTHER_WORK_REQUIRED:
PROJECT_STATE_AFTER:
RECOMMENDED_NEXT_ACTION:
HANDOFF_TO: NOTION
RAW_REFERENCE:

RESEARCH_REQUEST FORMAT (if needed, include after the RESPONSE_CONTRACT):
RESEARCH_REQUEST:
TASK_ID:
SUBTASK_ID:
QUESTION:
WHY_REQUIRED:
CURRENT_IMPLEMENTATION_BLOCKER:
WHAT_HAS_ALREADY_BEEN_CHECKED:
RELEVANT_FILES_OR_MODULES:
REQUIRED_EVIDENCE:
EXPECTED_OUTPUT:
URGENCY:
CAN_ENGINEERING_CONTINUE_IN_PARALLEL:
"""

    try:
        out = _call_openai(CODEX_MODEL, prompt)

        # Fail-closed: require the IDs to be present in output.
        if task_id not in out or subtask_id not in out:
            return BridgeError(
                bridge_id=BRIDGE_ID_CODEX,
                task_id=task_id,
                subtask_id=subtask_id,
                error_class="CONTRACT_VIOLATION",
                http_or_provider_status="",
                retryable="UNKNOWN",
                message="Upstream response did not preserve TASK_ID/SUBTASK_ID.",
                safe_technical_detail="Model output missing required identifiers.",
                suggested_action="Retry; if persistent, tighten prompt/instructions or use a more deterministic model route.",
                timestamp=_now_iso(),
                raw_reference="ID_NOT_PRESERVED",
            ).to_text_block()

        return out

    except Exception as exc:
        cls = _classify_error(exc)
        return BridgeError(
            bridge_id=BRIDGE_ID_CODEX,
            task_id=task_id,
            subtask_id=subtask_id,
            error_class=cls["error_class"],
            http_or_provider_status="",
            retryable=cls["retryable"],
            message="Upstream call failed during codex_send_task.",
            safe_technical_detail=str(exc)[:300],
            suggested_action=cls["suggested_action"],
            timestamp=_now_iso(),
            raw_reference="CODEX_UPSTREAM_ERROR",
        ).to_text_block()


@mcp.tool
def codex_error_test(task_id: str = "", subtask_id: str = "") -> str:
    """Intentionally trigger a controlled INVALID_REQUEST error contract.

    This does NOT call upstream.
    """
    return BridgeError(
        bridge_id=BRIDGE_ID_CODEX,
        task_id=task_id or None,
        subtask_id=subtask_id or None,
        error_class="INVALID_REQUEST",
        http_or_provider_status="",
        retryable="NO",
        message="Controlled error test (no upstream call).",
        safe_technical_detail="This is a synthetic error used to verify Notion-side parsing.",
        suggested_action="None (expected during test).",
        timestamp=_now_iso(),
        raw_reference="SYNTHETIC_ERROR_TEST",
    ).to_text_block()


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=PORT,
        stateless_http=True,
        # Public hosted MCP endpoint behind Render/Notion: keep bearer auth on,
        # but do not allow an environment-level FastMCP origin guard to reject
        # Notion's browser/service Origin before token verification runs.
        host_origin_protection=False,
    )
