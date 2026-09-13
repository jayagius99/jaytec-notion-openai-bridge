import os
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from openai import OpenAI

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-sol").strip()
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "8000"))

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

def call_openai(task: str) -> str:
    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=BASE_INSTRUCTIONS,
        input=task,
    )
    return response.output_text


@mcp.tool
def ask_openai(question: str, context: str = "") -> str:
    """Ask the OpenAI engineering peer for an independent answer.

    Use when Notion AI wants a second opinion, deeper reasoning, or technical help.
    Pass relevant Notion/page/project information in `context`.
    """
    prompt = f"""PROJECT CONTEXT:
{PROJECT_CONTEXT}

CONTEXT FROM NOTION:
{context}

QUESTION:
{question}

Produce a self-contained answer. Clearly mark uncertainty where appropriate."""
    return call_openai(prompt)


@mcp.tool
def review_notion_answer(
    question: str,
    notion_answer: str,
    context: str = "",
) -> str:
    """Have OpenAI independently audit a draft answer produced by Notion AI.

    Returns corrections, missing considerations, and a revised recommended answer.
    """
    prompt = f"""PROJECT CONTEXT:
{PROJECT_CONTEXT}

CONTEXT FROM NOTION:
{context}

ORIGINAL USER QUESTION:
{question}

NOTION AI DRAFT:
{notion_answer}

Act as an independent senior reviewer.
1. Check factual and technical correctness.
2. Find unsupported assumptions, omissions, contradictions, and unsafe shortcuts.
3. Preserve correct content.
4. Produce a corrected final answer Notion AI can use.
"""
    return call_openai(prompt)


@mcp.tool
def collaborate(
    task: str,
    notion_analysis: str = "",
    context: str = "",
) -> str:
    """Collaborate with Notion AI on a task.

    Notion should send its current reasoning/analysis plus relevant context.
    OpenAI responds as a peer engineer with improvements and a proposed next result.
    """
    prompt = f"""PROJECT CONTEXT:
{PROJECT_CONTEXT}

CONTEXT FROM NOTION:
{context}

TASK:
{task}

NOTION AI CURRENT ANALYSIS:
{notion_analysis}

Work as the second engineering agent.
Challenge mistakes instead of automatically agreeing.
Return:
- what appears correct,
- what needs correction or verification,
- the strongest improved solution,
- any concrete next checks or tests.
"""
    return call_openai(prompt)


@mcp.tool
def bridge_status() -> str:
    """Return basic bridge status without exposing secrets."""
    return f"JAYTEC Notion/OpenAI bridge is online. OpenAI model: {OPENAI_MODEL}"


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=PORT,
        stateless_http=True,
    )
