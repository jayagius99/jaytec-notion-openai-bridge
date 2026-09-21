import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent

class NonBlockingStartupTests(unittest.TestCase):
    def test_render_blueprint_keeps_adversarial_validation_off_cold_start(self):
        blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
        self.assertIn(
            "buildCommand: pip install -r requirements.txt && python staging_adversarial_probe.py",
            blueprint,
        )
        self.assertIn("startCommand: python staging_server.py", blueprint)
        self.assertNotIn(
            "startCommand: python staging_adversarial_probe.py",
            blueprint,
        )
        self.assertIn("autoDeployTrigger: checksPass", blueprint)
        self.assertNotIn("autoDeploy: true", blueprint)

    def test_read_bootstrap_cannot_block_http_bind(self):
        source = (ROOT / "staging_server.py").read_text(encoding="utf-8")
        main = source.split('if __name__ == "__main__":', 1)[1]
        self.assertNotIn('\n    _run_jaytec_read_bootstrap_probe()\n', main)
        self.assertIn('target=_run_jaytec_read_bootstrap_probe', main)
        self.assertIn('name="jaytec-read-bootstrap-oneshot"', main)
        self.assertIn('daemon=True', main)
        self.assertLess(
            main.index('target=_run_jaytec_read_bootstrap_probe'),
            main.index('mcp.run('),
        )

if __name__ == "__main__":
    unittest.main()
