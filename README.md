# JAYTEC Notion AI ↔ OpenAI MCP Bridge

This package creates a real remote MCP server that a Notion Agent can call when it wants help from an OpenAI model.

## What it does

It exposes four MCP tools:

- `ask_openai(question, context)` — independent answer / second opinion
- `review_notion_answer(question, notion_answer, context)` — audits Notion AI's draft
- `collaborate(task, notion_analysis, context)` — two-agent engineering collaboration
- `bridge_status()` — connection test

Default OpenAI model: `gpt-5.6-sol`.

## Security design

There are **two different secrets**:

1. `OPENAI_API_KEY` — your OpenAI API secret. Keep it only on the server host.
2. `MCP_AUTH_TOKEN` — a different random bearer token that Notion uses to authenticate to your bridge.

Never put the OpenAI API key in Notion pages, prompts, GitHub, or this ZIP.

## Local test

Python 3.11+ recommended.

```bash
pip install -r requirements.txt
export OPENAI_API_KEY="..."
export MCP_AUTH_TOKEN="..."
python server.py
```

On Windows PowerShell:

```powershell
$env:OPENAI_API_KEY="..."
$env:MCP_AUTH_TOKEN="..."
python server.py
```

The MCP endpoint is:

```text
http://localhost:8000/mcp
```

Notion cannot reach localhost. For Notion, deploy it to a public HTTPS host.

## Deployment

A Dockerfile and `render.yaml` are included. Any host that can run a Python/Docker web service and supply HTTPS is suitable.

Required environment secrets:
- `OPENAI_API_KEY`
- `MCP_AUTH_TOKEN`

Optional:
- `OPENAI_MODEL=gpt-5.6-sol`

After deployment, connect Notion to:

```text
https://YOUR-SERVICE-HOST/mcp
```

Configure header/bearer authentication with the value of `MCP_AUTH_TOKEN`.

## API key

Create the OpenAI API key yourself in the OpenAI API platform. The bridge cannot create, recover, or safely embed your account key.

## Important limitation

This calls an OpenAI API model. It does **not** attach Notion to an existing ChatGPT conversation, ChatGPT account memory, or this exact live chat session. If Notion wants the OpenAI peer to know project context, it should pass that context in the MCP call.


## JAYTEC:READ

The runtime now exposes a dedicated jaytec_read(url) MCP tool. It routes the
task through the canonical Gemini adapter with OpenRouter web_fetch enabled and
domain-restricted to the requested source. The workflow is fail-closed and has
no Notion, Codex, or other-agent fallback.

See JAYTEC_COMMAND_POLICY.md for the explicit Notion authorization gate and
READ route contract.
