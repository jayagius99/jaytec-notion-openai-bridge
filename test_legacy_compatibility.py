import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import server
from orchestration import ExecutionRegistry


class TestLegacyCompatibility(unittest.TestCase):
    def test_ordinary_collaborate_path_and_prompt_remain_unchanged(self):
        self.assertIsNone(
            server._legacy_collaborate_command(
                "ordinary task",
                lambda: "status",
                lambda packet_json: packet_json,
            )
        )
        self.assertEqual(
            "PROJECT CONTEXT:\nproject\n\nCONTEXT FROM NOTION:\ncontext\n\nTASK:\ntask\n\nNOTION AI CURRENT ANALYSIS:\nanalysis\n\nWork as the second engineering agent.\nChallenge mistakes instead of automatically agreeing.\nReturn:\n- what appears correct,\n- what needs correction or verification,\n- the strongest improved solution,\n- any concrete next checks or tests.\n",
            server._build_collaborate_prompt("project", "context", "task", "analysis"),
        )

    def test_legacy_status_delegates_to_status_result(self):
        calls = []
        result = server._legacy_collaborate_command(
            server.LEGACY_ORCHESTRATION_STATUS_TASK,
            lambda: calls.append("status") or "status-json",
            lambda packet_json: calls.append(("packet", packet_json)) or "packet-json",
        )
        self.assertEqual("status-json", result)
        self.assertEqual(["status"], calls)

    def test_legacy_packet_delegates_exact_suffix(self):
        calls = []
        result = server._legacy_collaborate_command(
            server.LEGACY_EXECUTE_TASK_PACKET_PREFIX + '{"packet":true}',
            lambda: calls.append("status") or "status-json",
            lambda packet_json: calls.append(("packet", packet_json)) or "packet-json",
        )
        self.assertEqual("packet-json", result)
        self.assertEqual([("packet", '{"packet":true}')], calls)

    def test_legacy_packet_malformed_json_fails_closed(self):
        result = server._execute_task_packet_json(
            "{",
            registry=ExecutionRegistry(),
            idempotency_store="process_memory",
            codex_dispatch=lambda _: None,
            gemini_dispatch=lambda _: None,
        )
        parsed = json.loads(result)
        self.assertEqual("INVALID_PACKET", parsed["overall_status"])
        self.assertEqual("invalid", parsed["execution_id"])
        self.assertTrue(parsed["unresolved_items"])

    def test_legacy_packet_preserves_replay_and_conflict_semantics(self):
        now = datetime.now(timezone.utc)
        packet = {
            "packet_version": "1.0",
            "task_id": "JAYTEC-2026-0001",
            "subtask_id": "JAYTEC-2026-0001-C9-COMPAT",
            "request": "harmless compatibility test",
            "intent": "test",
            "workflow_id": "WORKFLOW_ARCHITECTURE_DECISION",
            "risk_level": "low",
            "specialist_plan": ["codex"],
            "allowed_operations": ["test"],
            "expected_output": "result",
            "validation_requirements": ["replay"],
            "side_effect_policy": "staging_only",
            "idempotency_key": "legacy-compat-001",
            "deadline": (now + timedelta(minutes=10)).isoformat(),
            "max_fanout": 1,
            "max_retries": 0,
            "return_schema_version": "1.0",
        }
        calls = {"count": 0}

        def codex(_):
            calls["count"] += 1
            return {
                "status": "SUCCESS",
                "model": "gpt-5.3-codex",
                "findings": [],
                "evidence": [],
            }

        registry = ExecutionRegistry()
        first = json.loads(
            server._execute_task_packet_json(
                json.dumps(packet),
                registry=registry,
                idempotency_store="process_memory",
                codex_dispatch=codex,
                gemini_dispatch=lambda _: None,
            )
        )
        replay = json.loads(
            server._execute_task_packet_json(
                json.dumps(packet),
                registry=registry,
                idempotency_store="process_memory",
                codex_dispatch=codex,
                gemini_dispatch=lambda _: None,
            )
        )
        self.assertEqual(first["execution_id"], replay["execution_id"])
        self.assertTrue(replay["usage_summary"]["idempotent_replay"])
        self.assertEqual(1, calls["count"])

        conflicting = copy.deepcopy(packet)
        conflicting["request"] = "different harmless request"
        conflict = json.loads(
            server._execute_task_packet_json(
                json.dumps(conflicting),
                registry=registry,
                idempotency_store="process_memory",
                codex_dispatch=codex,
                gemini_dispatch=lambda _: None,
            )
        )
        self.assertEqual("FAILED_CLOSED", conflict["overall_status"])
        self.assertIn("CONFLICTING_DUPLICATE", conflict["unresolved_items"])

    def test_auth_and_model_prerequisites_remain_fail_closed(self):
        with patch.object(server, "MCP_AUTH_TOKEN", ""), patch.object(
            server, "OPENAI_API_KEY", "configured"
        ):
            with self.assertRaisesRegex(RuntimeError, "MCP_AUTH_TOKEN"):
                server._require_startup_prereqs()

        with patch.object(server, "MCP_AUTH_TOKEN", "configured"), patch.object(
            server, "OPENAI_API_KEY", "configured"
        ), patch.object(server, "RUNTIME_MODE", "production"), patch.object(
            server, "DATABASE_URL", ""
        ):
            with self.assertRaisesRegex(RuntimeError, "DATABASE_URL"):
                server._require_startup_prereqs()


if __name__ == "__main__":
    unittest.main()
