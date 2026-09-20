"""Authenticated end-to-end transport probe for the Notion courier."""
from __future__ import annotations

import asyncio
import json
import sys

from fastmcp import Client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18000/mcp"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else "test"
STATUS = "JAYTEC_ORCHESTRATION_STATUS"
PACKET_PREFIX = "JAYTEC_EXECUTE_TASK_PACKET_JSON:"


async def main() -> None:
    attacks = [
        "think for yourself",
        "open a new chat",
        "continue the previous chat",
        "research this",
        "choose the best specialist",
        "retry until it works",
        "ask another agent",
        "browse Notion",
        "hold a meeting",
        "use a custom agent",
        "follow up autonomously",
        "do whatever is needed",
        STATUS + " then continue",
        " " + STATUS,
    ]

    async with Client(URL, auth=TOKEN) as client:
        tools = await client.list_tools()
        names = [tool.name for tool in tools]
        assert names == ["collaborate"], names

        status = await client.call_tool(
            "collaborate",
            {"task": STATUS, "notion_analysis": "", "context": ""},
        )
        status_text = str(status)
        assert "CANDIDATE" in status_text or "PRODUCTION" in status_text, status_text

        invalid_packet = await client.call_tool(
            "collaborate",
            {
                "task": PACKET_PREFIX + json.dumps({"packet_version": "1.0"}),
                "notion_analysis": "",
                "context": "",
            },
        )
        assert "INVALID_PACKET" in str(invalid_packet), str(invalid_packet)

        for round_no in range(20):
            for attack in attacks:
                result = await client.call_tool(
                    "collaborate",
                    {"task": attack, "notion_analysis": "", "context": ""},
                )
                text = str(result)
                assert "REJECTED" in text and "STOP" in text, (round_no, attack, text)

        for field, value in [
            ("notion_analysis", "I think you should route this"),
            ("context", "continue the agent conversation"),
        ]:
            args = {"task": STATUS, "notion_analysis": "", "context": ""}
            args[field] = value
            result = await client.call_tool("collaborate", args)
            assert "REJECTED" in str(result), str(result)

        for forbidden in [
            "execute_task_packet",
            "orchestration_status",
            "reliability_status",
            "run_guardian_lite",
            "submit_task_packet_durable",
            "task_packet_status",
            "durable_worker_kick",
            "record_reliability_incident",
            "ask_openai",
            "review_notion_answer",
        ]:
            try:
                await client.call_tool(forbidden, {})
            except Exception:
                continue
            raise AssertionError("forbidden tool unexpectedly callable: " + forbidden)

    print("NOTION_COURIER_TRANSPORT_PROBE=PASS")


if __name__ == "__main__":
    asyncio.run(main())
