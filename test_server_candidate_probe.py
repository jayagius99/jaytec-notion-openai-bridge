import os
import unittest


class TestServerCandidateProbe(unittest.TestCase):
    def test_production_mode_requires_database_url(self):
        import importlib

        import server

        os.environ["RUNTIME_MODE"] = "production"
        os.environ.pop("DATABASE_URL", None)
        os.environ["MCP_AUTH_TOKEN"] = "test"
        os.environ["OPENAI_API_KEY"] = "test"

        # Reload to pick up env
        importlib.reload(server)

        with self.assertRaises(RuntimeError):
            server.create_mcp_app()

    def test_candidate_mode_allows_import_and_exposes_entrypoints(self):
        import server

        # create_mcp_app should register tools, but requires env to instantiate
        os.environ["RUNTIME_MODE"] = "staging_candidate"
        os.environ["MCP_AUTH_TOKEN"] = "test"
        os.environ["OPENAI_API_KEY"] = "test"
        os.environ["CODEX_MODEL"] = "gpt-5.3-codex"

        mcp = server.create_mcp_app()
        # FastMCP exposes tools internally; we just assert functions exist on module.
        self.assertTrue(hasattr(server, "create_mcp_app"))
        self.assertIsNotNone(mcp)


if __name__ == "__main__":
    unittest.main()
