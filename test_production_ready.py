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
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ["OPENROUTER_API_KEY"] = "test"
        os.environ["CODEX_MODEL"] = "nvidia/nemotron-3-ultra-550b-a55b:free"
        os.environ["DEEPSEEK_REVIEWER_MODEL"] = "deepseek/deepseek-v4-flash-0731:free"
        os.environ["GEMINI_MODEL"] = "google/gemini-3.1-pro-preview"

        server = self._reload()

        self.assertTrue(
            server.compute_production_ready(
                runtime_mode=server.RUNTIME_MODE,
                idempotency_store="postgres",
                codex_model=server.CODEX_MODEL,
                gemini_model=server.GEMINI_MODEL,
                reviewer_model=server.REVIEWER_MODEL,
                mcp_auth_token_present=True,
                openai_api_key_present=False,
                openrouter_api_key_present=True,
            )
        )

    def test_paid_gemini_is_not_required_for_ordinary_production_readiness(self):
        server = self._reload()
        self.assertTrue(
            server.compute_production_ready(
                runtime_mode="production",
                idempotency_store="postgres",
                codex_model="nvidia/nemotron-3-ultra-550b-a55b:free",
                reviewer_model="deepseek/deepseek-v4-flash-0731:free",
                gemini_model="not-configured-paid-reserve",
                mcp_auth_token_present=True,
                openai_api_key_present=False,
                openrouter_api_key_present=True,
            )
        )

    def test_wrong_deepseek_reviewer_model_fails_readiness(self):
        server = self._reload()
        self.assertFalse(
            server.compute_production_ready(
                runtime_mode="production",
                idempotency_store="postgres",
                codex_model="nvidia/nemotron-3-ultra-550b-a55b:free",
                reviewer_model="google/gemini-3.1-pro-preview",
                gemini_model="google/gemini-3.1-pro-preview",
                mcp_auth_token_present=True,
                openai_api_key_present=False,
                openrouter_api_key_present=True,
            )
        )

    def test_candidate_mode_never_reports_production_ready(self):
        os.environ["RUNTIME_MODE"] = "staging_candidate"
        os.environ.pop("DATABASE_URL", None)
        os.environ["MCP_AUTH_TOKEN"] = "test"
        os.environ["OPENAI_API_KEY"] = "test"
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ["CODEX_MODEL"] = "nvidia/nemotron-3-ultra-550b-a55b:free"
        os.environ["DEEPSEEK_REVIEWER_MODEL"] = "deepseek/deepseek-v4-flash-0731:free"
        os.environ["GEMINI_MODEL"] = "google/gemini-3.1-pro-preview"

        server = self._reload()

        self.assertFalse(
            server.compute_production_ready(
                runtime_mode=server.RUNTIME_MODE,
                idempotency_store="process_memory",
                codex_model=server.CODEX_MODEL,
                gemini_model=server.GEMINI_MODEL,
                reviewer_model=server.REVIEWER_MODEL,
                mcp_auth_token_present=True,
                openai_api_key_present=True,
                openrouter_api_key_present=False,
            )
        )

    def test_production_missing_openrouter_does_not_claim_ready(self):
        os.environ["RUNTIME_MODE"] = "production"
        os.environ["DATABASE_URL"] = "postgres://user:pass@localhost:5432/db"
        os.environ["MCP_AUTH_TOKEN"] = "test"
        os.environ["OPENAI_API_KEY"] = "test"
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ["CODEX_MODEL"] = "nvidia/nemotron-3-ultra-550b-a55b:free"
        os.environ["DEEPSEEK_REVIEWER_MODEL"] = "deepseek/deepseek-v4-flash-0731:free"
        os.environ["GEMINI_MODEL"] = "google/gemini-3.1-pro-preview"

        server = self._reload()

        self.assertFalse(
            server.compute_production_ready(
                runtime_mode=server.RUNTIME_MODE,
                idempotency_store="postgres",
                codex_model=server.CODEX_MODEL,
                gemini_model=server.GEMINI_MODEL,
                reviewer_model=server.REVIEWER_MODEL,
                mcp_auth_token_present=True,
                openai_api_key_present=True,
                openrouter_api_key_present=bool(server.OPENROUTER_API_KEY),
            )
        )


if __name__ == "__main__":
    unittest.main()
