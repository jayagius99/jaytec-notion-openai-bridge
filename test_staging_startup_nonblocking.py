import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class NonBlockingStartupTests(unittest.TestCase):
    def test_jaytec_read_bootstrap_is_backgrounded_before_http_bind(self):
        source = (ROOT / "staging_server.py").read_text(encoding="utf-8")
        main = source.split('if __name__ == "__main__":', 1)[1]
        self.assertIn('target=_run_jaytec_read_bootstrap_probe', main)
        self.assertIn('name="jaytec-read-bootstrap-oneshot"', main)
        self.assertIn('daemon=True', main)
        self.assertNotIn('\n    _run_jaytec_read_bootstrap_probe()\n', main)
        self.assertLess(
            main.index('target=_run_jaytec_read_bootstrap_probe'),
            main.index('mcp.run('),
        )


if __name__ == "__main__":
    unittest.main()
