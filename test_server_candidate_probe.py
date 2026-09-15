import importlib
import os
import unittest


class TestServerCandidateProbe(unittest.TestCase):
    def test_default_mode_requires_database_url(self):
        # Default runtime should be PRODUCTION when unset.
        os.environ.pop("RUNTIME_MODE", None)
        os.environ.pop("DATABASE_URL", None)
        os.environ["MCP_AUTH_TOKEN"] = "test"
        os.environ["OPENAI_API_KEY"] = "test"

        import server
        importlib.reload(server)

        with self.assertRaises(RuntimeError):
            server.create_mcp_app()

    def test_explicit_production_requires_database_url(self):
        os.environ["RUNTIME_MODE"] = "production"
        os.environ.pop("DATABASE_URL", None)
        os.environ["MCP_AUTH_TOKEN"] = "test"
        os.environ["OPENAI_API_KEY"] = "test"

        import server
        importlib.reload(server)

        with self.assertRaises(RuntimeError):
            server.create_mcp_app()

    def test_candidate_mode_allows_startup_with_dummy_env(self):
        # Ensure env is set BEFORE importing server so module-level config is correct.
        os.environ["RUNTIME_MODE"] = "staging_candidate"
        os.environ.pop("DATABASE_URL", None)
        os.environ["MCP_AUTH_TOKEN"] = "test"
        os.environ["OPENAI_API_KEY"] = "test"
        os.environ["CODEX_MODEL"] = "gpt-5.3-codex"
        os.environ["GEMINI_MODEL"] = "google/gemini-3.1-pro-preview"

        import server
        importlib.reload(server)

        mcp = server.create_mcp_app()
        self.assertTrue(hasattr(server, "create_mcp_app"))
        self.assertIsNotNone(mcp)


if __name__ == "__main__":
    unittest.main()
