import importlib
import os
import unittest


class TestProductionReady(unittest.TestCase):
    def _reload(self):
        import server
        importlib.reload(server)
        return server

    def test_production_ready_true_only_when_all_prereqs_met(self):
        os.environ["RUNTIME_MODE"] = "production"
        os.environ["DATABASE_URL"] = "postgres://user:pass@localhost:5432/db"
        os.environ["MCP_AUTH_TOKEN"] = "test"
        os.environ["MCP_AUTH_SUBJECT"] = "jaytec-control-plane"
        os.environ["MCP_AUTH_CLIENT_ID"] = "jaytec-control-plane"
        os.environ["OPENAI_API_KEY"] = "test"
        os.environ["OPENROUTER_API_KEY"] = "test"
        os.environ["CODEX_MODEL"] = "gpt-5.6-sol"
        os.environ["GEMINI_MODEL"] = "google/gemini-3.1-pro-preview"

        server = self._reload()

        self.assertTrue(
            server.compute_production_ready(
                runtime_mode=server.RUNTIME_MODE,
                idempotency_store="postgres",
                codex_model=server.CODEX_MODEL,
                gemini_model=server.GEMINI_MODEL,
                mcp_auth_token_present=True,
                mcp_auth_token_strong=True,
                openai_api_key_present=True,
                openrouter_api_key_present=True,
                mcp_auth_subject="jaytec-control-plane",
                mcp_auth_client_id="jaytec-control-plane",
            )
        )



    def test_production_ready_false_when_mcp_token_is_weak(self):
        server = self._reload()
        self.assertFalse(
            server.compute_production_ready(
                runtime_mode="production",
                idempotency_store="postgres",
                codex_model="gpt-5.6-sol",
                gemini_model="google/gemini-3.1-pro-preview",
                mcp_auth_token_present=True,
                mcp_auth_token_strong=False,
                openai_api_key_present=True,
                openrouter_api_key_present=True,
                mcp_auth_subject="jaytec-control-plane",
                mcp_auth_client_id="jaytec-control-plane",
            )
        )

    def test_production_ready_false_when_legacy_direct_provider_tools_enabled(self):
        os.environ["RUNTIME_MODE"] = "production"
        os.environ["DATABASE_URL"] = "postgres://user:pass@localhost:5432/db"
        os.environ["MCP_AUTH_TOKEN"] = "test"
        os.environ["MCP_AUTH_SUBJECT"] = "jaytec-control-plane"
        os.environ["MCP_AUTH_CLIENT_ID"] = "jaytec-control-plane"
        os.environ["OPENAI_API_KEY"] = "test"
        os.environ["OPENROUTER_API_KEY"] = "test"
        os.environ["CODEX_MODEL"] = "gpt-5.6-sol"
        os.environ["GEMINI_MODEL"] = "google/gemini-3.1-pro-preview"

        server = self._reload()

        self.assertFalse(
            server.compute_production_ready(
                runtime_mode="production",
                idempotency_store="postgres",
                codex_model=server.CODEX_MODEL,
                gemini_model=server.GEMINI_MODEL,
                mcp_auth_token_present=True,
                mcp_auth_token_strong=True,
                openai_api_key_present=True,
                openrouter_api_key_present=True,
                legacy_direct_tools_enabled=True,
            )
        )


    def test_production_ready_rejects_notion_agent_as_execution_principal(self):
        server = self._reload()
        self.assertFalse(
            server.compute_production_ready(
                runtime_mode="production",
                idempotency_store="postgres",
                codex_model="gpt-5.6-sol",
                gemini_model="google/gemini-3.1-pro-preview",
                mcp_auth_token_present=True,
                mcp_auth_token_strong=True,
                openai_api_key_present=True,
                openrouter_api_key_present=True,
                mcp_auth_subject="notion-agent",
                mcp_auth_client_id="jaytec-notion-openai-bridge",
            )
        )

    def test_candidate_mode_never_reports_production_ready(self):
        os.environ["RUNTIME_MODE"] = "staging_candidate"
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("MCP_AUTH_SUBJECT", None)
        os.environ.pop("MCP_AUTH_CLIENT_ID", None)
        os.environ["MCP_AUTH_TOKEN"] = "test"
        os.environ["MCP_AUTH_SUBJECT"] = "jaytec-control-plane"
        os.environ["MCP_AUTH_CLIENT_ID"] = "jaytec-control-plane"
        os.environ["OPENAI_API_KEY"] = "test"
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ["CODEX_MODEL"] = "gpt-5.6-sol"
        os.environ["GEMINI_MODEL"] = "google/gemini-3.1-pro-preview"

        server = self._reload()

        self.assertFalse(
            server.compute_production_ready(
                runtime_mode=server.RUNTIME_MODE,
                idempotency_store="process_memory",
                codex_model=server.CODEX_MODEL,
                gemini_model=server.GEMINI_MODEL,
                mcp_auth_token_present=True,
                mcp_auth_token_strong=True,
                openai_api_key_present=True,
                openrouter_api_key_present=False,
            )
        )

    def test_production_missing_openrouter_does_not_claim_ready(self):
        os.environ["RUNTIME_MODE"] = "production"
        os.environ["DATABASE_URL"] = "postgres://user:pass@localhost:5432/db"
        os.environ["MCP_AUTH_TOKEN"] = "test"
        os.environ["MCP_AUTH_SUBJECT"] = "jaytec-control-plane"
        os.environ["MCP_AUTH_CLIENT_ID"] = "jaytec-control-plane"
        os.environ["OPENAI_API_KEY"] = "test"
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ["CODEX_MODEL"] = "gpt-5.6-sol"
        os.environ["GEMINI_MODEL"] = "google/gemini-3.1-pro-preview"

        server = self._reload()

        self.assertFalse(
            server.compute_production_ready(
                runtime_mode=server.RUNTIME_MODE,
                idempotency_store="postgres",
                codex_model=server.CODEX_MODEL,
                gemini_model=server.GEMINI_MODEL,
                mcp_auth_token_present=True,
                mcp_auth_token_strong=True,
                openai_api_key_present=True,
                openrouter_api_key_present=bool(server.OPENROUTER_API_KEY),
            )
        )


if __name__ == "__main__":
    unittest.main()
