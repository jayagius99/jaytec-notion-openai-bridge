import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from openai import OpenAI

from circuit_breaker import CircuitBreaker
from orchestration import (
    ExecutionRegistry,
    PacketValidationError,
    execute_task_packet_core,
    parse_packet_json,
)
from specialist_adapters import (
    EXPECTED_CODEX_MODEL,
    EXPECTED_GEMINI_MODEL,
    build_codex_dispatch,
    build_gemini_dispatch,
    resolve_engineering_model,
)

# --- Runtime configuration (NO secrets in code) ---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-sol").strip()

# Provider-neutral engineering model config with legacy CODEX_MODEL compatibility.
CODEX_MODEL = resolve_engineering_model()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", EXPECTED_GEMINI_MODEL).strip()

# OpenRouter route for Gemini research (optional; disabled unless configured).
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
GEMINI_TIMEOUT_S = float(os.environ.get("GEMINI_TIMEOUT_S", "90"))

MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "8000"))

# Timeouts / retries (bounded)
OPENAI_TIMEOUT_S = float(os.environ.get("OPENAI_TIMEOUT_S", "45"))
OPENAI_MAX_RETRIES = int(os.environ.get("OPENAI_MAX_RETRIES", "2"))

# Circuit breaker
CIRCUIT_FAILURE_THRESHOLD = int(os.environ.get("CIRCUIT_FAILURE_THRESHOLD", "3"))
CIRCUIT_RESET_SECONDS = int(os.environ.get("CIRCUIT_RESET_SECONDS", "60"))

# Runtime mode:
# - production is the SAFE DEFAULT and must be durable
# - staging_candidate exists only as an explicit escape hatch for CI / candidate validation
RUNTIME_MODE = os.environ.get("RUNTIME_MODE", "production").strip().lower()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

BRIDGE_ID_CODEX = "BRIDGE_CODEX_ENGINEERING"
LEGACY_ORCHESTRATION_STATUS_TASK = "JAYTEC_ORCHESTRATION_STATUS"
LEGACY_EXECUTE_TASK_PACKET_PREFIX = "JAYTEC_EXECUTE_TASK_PACKET_JSON:"


def _require_startup_prereqs() -> None:
    if not MCP_AUTH_TOKEN:
        raise RuntimeError(
            "MCP_AUTH_TOKEN is not set. Refusing to start an unauthenticated remote MCP server."
        )
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set.")

    # Fail closed: production requires durable idempotency.
    if RUNTIME_MODE == "production":
        if not DATABASE_URL:
            raise RuntimeError(
                "RUNTIME_MODE=production requires DATABASE_URL for durable idempotency; refusing to start with process memory."
            )



def compute_production_ready(
    *,
    runtime_mode: str,
    idempotency_store: str,
    codex_model: str,
    gemini_model: str,
    mcp_auth_token_present: bool,
    openai_api_key_present: bool,
    openrouter_api_key_present: bool,
) -> bool:
    """Compute whether this bridge instance is truly production-ready.

    This is intentionally fail-closed: any missing prerequisite should return False.

    Required conditions:
    - RUNTIME_MODE == 'production'
    - durable idempotency store is 'postgres'
    - exact specialist model identities match the required locks
    - MCP auth + provider startup prerequisites are satisfied
    - Gemini production adapter is actually configured (OPENROUTER_API_KEY present)

    NOTE: Startup may still be allowed in some partially-configured states; this flag
    is strictly about readiness, not liveness.
    """
    if runtime_mode != "production":
        return False
    if idempotency_store != "postgres":
        return False
    if codex_model != EXPECTED_CODEX_MODEL:
        return False
    if gemini_model != EXPECTED_GEMINI_MODEL:
        return False
    if not mcp_auth_token_present:
        return False
    if not openai_api_key_present:
        return False
    if not openrouter_api_key_present:
        return False
    return True



def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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



def _classify_error(exc: Exception) -> Dict[str, str]:
    msg = str(exc)
    lower = msg.lower()
    if "401" in lower or "unauthorized" in lower or "api key" in lower:
        return {
            "error_class": "AUTH_ERROR",
            "retryable": "NO",
            "suggested_action": "Check OPENAI_API_KEY and provider account access.",
        }
    if "429" in lower or ("rate" in lower and "limit" in lower):
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



def _orchestration_status_json(
    *,
    runtime_mode: str,
    codex_model: str,
    gemini_model: str,
    codex_circuit: Any,
    gemini_circuit: Any,
    idempotency_store: str,
    production_ready: bool,
) -> str:
    return json.dumps(
        {
            "status": "PRODUCTION" if runtime_mode == "production" else "CANDIDATE",
            "operation": "execute_task_packet",
            "codex_model": codex_model,
            "gemini_model": gemini_model,
            "codex_circuit": codex_circuit,
            "gemini_circuit": gemini_circuit,
            "idempotency_store": idempotency_store,
            "runtime_mode": runtime_mode,
            "production_ready": production_ready,
        },
        sort_keys=True,
    )



def _execute_task_packet_json(
    packet_json: str,
    *,
    registry: ExecutionRegistry,
    idempotency_store: str,
    codex_dispatch: Any,
    gemini_dispatch: Any,
) -> str:
    packet, parse_errors = parse_packet_json(packet_json)
    if packet is None:
        return json.dumps(
            {
                "execution_id": "invalid",
                "task_id": "",
                "subtask_id": "",
                "overall_status": "INVALID_PACKET",
                "unresolved_items": list(parse_errors),
                "return_schema_version": "1.0",
            },
            sort_keys=True,
        )

    def _lookup(key: str, digest: str, now=None):
        try:
            return registry.lookup(key, digest, now=now)
        except ValueError as exc:
            if str(exc) == "CONFLICTING_DUPLICATE":
                raise PacketValidationError("CONFLICTING_DUPLICATE")
            raise

    def _store(key: str, digest: str, result, now=None):
        try:
            return registry.store(key, digest, result, now=now)
        except ValueError as exc:
            if str(exc) == "CONFLICTING_DUPLICATE":
                raise PacketValidationError("CONFLICTING_DUPLICATE")
            raise

    if idempotency_store == "postgres":
        class _Wrapper(ExecutionRegistry):
            def lookup(self, key, packet_hash, *, now=None):
                return _lookup(key, packet_hash, now=now)

            def store(self, key, packet_hash, result, *, now=None):
                return _store(key, packet_hash, result, now=now)

        registry_adapter = _Wrapper()
    else:
        registry_adapter = registry

    result = execute_task_packet_core(
        packet,
        {"codex": codex_dispatch, "gemini": gemini_dispatch},
        registry_adapter,
    )
    return json.dumps(result, ensure_ascii=False, sort_keys=True)



def _legacy_collaborate_command(
    task: str,
    status_fn: Any,
    packet_fn: Any,
) -> Optional[str]:
    if task == LEGACY_ORCHESTRATION_STATUS_TASK:
        return status_fn()
    if task.startswith(LEGACY_EXECUTE_TASK_PACKET_PREFIX):
        return packet_fn(task[len(LEGACY_EXECUTE_TASK_PACKET_PREFIX) :])
    return None



def _build_collaborate_prompt(
    project_context: str,
    context: str,
    task: str,
    notion_analysis: str,
) -> str:
    return f"""PROJECT CONTEXT:\n{project_context}\n\nCONTEXT FROM NOTION:\n{context}\n\nTASK:\n{task}\n\nNOTION AI CURRENT ANALYSIS:\n{notion_analysis}\n\nWork as the second engineering agent.\nChallenge mistakes instead of automatically agreeing.\nReturn:\n- what appears correct,\n- what needs correction or verification,\n- the strongest improved solution,\n- any concrete next checks or tests.\n"""


project_context_path = Path(__file__).with_name("PROJECT_CONTEXT.md")
PROJECT_CONTEXT = (
    project_context_path.read_text(encoding="utf-8")
    if project_context_path.exists()
    else ""
)

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



def _call_openai(client: OpenAI, model: str, task: str) -> str:
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
            if attempt >= OPENAI_MAX_RETRIES:
                break
            time.sleep(0.75 * (attempt + 1))
    raise last_exc or RuntimeError("Unknown OpenAI error")



def create_mcp_app() -> FastMCP:
    """Create the authenticated MCP server.

    Separated from module import so CI can compile/import server.py without secrets.
    """
    _require_startup_prereqs()

    auth = StaticTokenVerifier(
        tokens={
            MCP_AUTH_TOKEN: {
                "sub": "notion-agent",
                "client_id": "jaytec-notion-openai-bridge",
            }
        }
    )
    mcp = FastMCP("JAYTEC OpenAI Engineering Bridge", auth=auth)

    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    openrouter_client = (
        OpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)
        if OPENROUTER_API_KEY
        else None
    )

    # Idempotency registry selection
    if DATABASE_URL:
        from idempotency_postgres import PostgresExecutionRegistry

        registry: ExecutionRegistry = PostgresExecutionRegistry(
            database_url=DATABASE_URL,
            ttl_seconds=ExecutionRegistry().ttl_seconds,
        )
        registry.ensure_schema()
        idempotency_store = "postgres"
    else:
        registry = ExecutionRegistry()
        idempotency_store = "process_memory"

    production_ready = compute_production_ready(
        runtime_mode=RUNTIME_MODE,
        idempotency_store=idempotency_store,
        codex_model=CODEX_MODEL,
        gemini_model=GEMINI_MODEL,
        mcp_auth_token_present=bool(MCP_AUTH_TOKEN),
        openai_api_key_present=bool(OPENAI_API_KEY),
        openrouter_api_key_present=bool(OPENROUTER_API_KEY),
    )

    codex_circuit = CircuitBreaker(
        failure_threshold=CIRCUIT_FAILURE_THRESHOLD,
        reset_after_seconds=CIRCUIT_RESET_SECONDS,
    )
    gemini_circuit = CircuitBreaker(
        failure_threshold=CIRCUIT_FAILURE_THRESHOLD,
        reset_after_seconds=CIRCUIT_RESET_SECONDS,
    )

    codex_dispatch = build_codex_dispatch(
        openai_client=openai_client,
        codex_model=CODEX_MODEL,
        circuit=codex_circuit,
    )

    if openrouter_client is not None:
        gemini_dispatch = build_gemini_dispatch(
            openrouter_client=openrouter_client,
            gemini_model=GEMINI_MODEL,
            gemini_timeout_s=GEMINI_TIMEOUT_S,
            circuit=gemini_circuit,
        )
    else:
        gemini_dispatch = gemini_circuit.guard(
            lambda _packet: (_ for _ in ()).throw(RuntimeError("OPENROUTER_API_KEY is not configured on this bridge"))
        )

    def _status_json() -> str:
        return _orchestration_status_json(
            runtime_mode=RUNTIME_MODE,
            codex_model=CODEX_MODEL,
            gemini_model=GEMINI_MODEL,
            codex_circuit=codex_circuit.snapshot(),
            gemini_circuit=gemini_circuit.snapshot(),
            idempotency_store=idempotency_store,
            production_ready=production_ready,
        )

    def _packet_json(packet_json: str) -> str:
        return _execute_task_packet_json(
            packet_json,
            registry=registry,
            idempotency_store=idempotency_store,
            codex_dispatch=codex_dispatch,
            gemini_dispatch=gemini_dispatch,
        )

    # ---------------- Legacy tools (preserved) ----------------

    @mcp.tool
    def ask_openai(question: str, context: str = "") -> str:
        prompt = f"""PROJECT CONTEXT:\n{PROJECT_CONTEXT}\n\nCONTEXT FROM NOTION:\n{context}\n\nQUESTION:\n{question}\n\nProduce a self-contained answer. Clearly mark uncertainty where appropriate."""
        return _call_openai(openai_client, OPENAI_MODEL, prompt)

    @mcp.tool
    def review_notion_answer(question: str, notion_answer: str, context: str = "") -> str:
        prompt = f"""PROJECT CONTEXT:\n{PROJECT_CONTEXT}\n\nCONTEXT FROM NOTION:\n{context}\n\nORIGINAL USER QUESTION:\n{question}\n\nNOTION AI DRAFT:\n{notion_answer}\n\nAct as an independent senior reviewer.\n1. Check factual and technical correctness.\n2. Find unsupported assumptions, omissions, contradictions, and unsafe shortcuts.\n3. Preserve correct content.\n4. Produce a corrected final answer Notion AI can use.\n"""
        return _call_openai(openai_client, OPENAI_MODEL, prompt)

    @mcp.tool
    def collaborate(task: str, notion_analysis: str = "", context: str = "") -> str:
        legacy_result = _legacy_collaborate_command(task, _status_json, _packet_json)
        if legacy_result is not None:
            return legacy_result
        prompt = _build_collaborate_prompt(PROJECT_CONTEXT, context, task, notion_analysis)
        return _call_openai(openai_client, OPENAI_MODEL, prompt)

    @mcp.tool
    def bridge_status() -> str:
        return f"JAYTEC Notion/OpenAI bridge is online. OpenAI model: {OPENAI_MODEL}"

    # ---------------- Unified orchestration surface ----------------

    @mcp.tool
    def orchestration_status() -> str:
        return _status_json()

    @mcp.tool
    def execute_task_packet(packet_json: str) -> str:
        return _packet_json(packet_json)

    return mcp



def main() -> None:
    mcp = create_mcp_app()
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=PORT,
        stateless_http=True,
        host_origin_protection=False,
    )


if __name__ == "__main__":
    main()
