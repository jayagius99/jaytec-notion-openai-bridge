import json
import unittest
from pathlib import Path

import server
from participant_contracts import render_actor_contract
from relationship_policy import Actor


ROOT = Path(__file__).resolve().parent


class ControlPlaneIdentityReadinessTests(unittest.TestCase):
    def test_main_mcp_identity_is_jaytec_not_notion(self):
        self.assertEqual(server.MCP_AUTH_SUBJECT, "jaytec-control-plane-client")
        self.assertEqual(server.MCP_AUTH_CLIENT_ID, "jaytec-control-plane-bridge")
        self.assertNotIn("notion", server.MCP_AUTH_SUBJECT.casefold())
        self.assertNotIn("notion", server.MCP_AUTH_CLIENT_ID.casefold())

    def test_bridge_prerequisites_never_claim_system_activation(self):
        payload = json.loads(
            server._orchestration_status_json(
                runtime_mode="production",
                codex_model="gpt-5.6-sol",
                gemini_model="google/gemini-3.1-pro-preview",
                codex_circuit={},
                gemini_circuit={},
                idempotency_store="postgres",
                bridge_prerequisites_ready=True,
            )
        )
        self.assertTrue(payload["bridge_prerequisites_ready"])
        self.assertFalse(payload["production_ready"])
        self.assertFalse(payload["system_activation_ready"])
        self.assertEqual(payload["readiness_scope"], "BRIDGE_PROCESS_ONLY")
        self.assertEqual(
            payload["root_owner_activation_authority"],
            "EXTERNAL_OWNER_ONLY",
        )

    def test_compatibility_helper_is_bridge_local_only(self):
        self.assertTrue(
            server.compute_production_ready(
                runtime_mode="production",
                idempotency_store="postgres",
                codex_model="gpt-5.6-sol",
                gemini_model="google/gemini-3.1-pro-preview",
                mcp_auth_token_present=True,
                openai_api_key_present=True,
                openrouter_api_key_present=True,
            )
        )
        status = json.loads(
            server._orchestration_status_json(
                runtime_mode="production",
                codex_model="gpt-5.6-sol",
                gemini_model="google/gemini-3.1-pro-preview",
                codex_circuit={},
                gemini_circuit={},
                idempotency_store="postgres",
                bridge_prerequisites_ready=True,
            )
        )
        self.assertFalse(status["production_ready"])
        self.assertFalse(status["system_activation_ready"])

    def test_notion_contract_forbids_main_control_plane_credential(self):
        contract = render_actor_contract(Actor.NOTION)
        self.assertIn(
            "Do not possess or use the main JAYTEC control-plane MCP credential",
            contract,
        )

    def test_server_source_does_not_label_main_token_notion_agent(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertNotIn('"sub": "notion-agent"', source)
        self.assertNotIn('"client_id": "jaytec-notion-openai-bridge"', source)


if __name__ == "__main__":
    unittest.main()
