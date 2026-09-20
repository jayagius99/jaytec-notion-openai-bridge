import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class StartupProviderSafetyTests(unittest.TestCase):
    def test_render_startup_contains_no_provider_review_scripts(self):
        blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
        self.assertIn(
            "startCommand: python staging_adversarial_probe.py && python staging_server.py",
            blueprint,
        )
        for forbidden in (
            "staging_independent_g1_review.py &&",
            "staging_gemini_manus_governance_review.py &&",
            "staging_manus_governance_acceptance.py &&",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blueprint)

    def test_persistent_staging_blueprint_has_no_manus_credentials(self):
        blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
        for forbidden in (
            "MANUS_API_KEY",
            "JAYTEC_MANUS_REVIEW_TASK_ID",
            "JAYTEC_MANUS_PROJECT_ID",
            "RUN_LIVE_MANUS_GOVERNANCE_ACCEPTANCE",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blueprint)

    def test_all_server_start_provider_probes_default_disabled(self):
        blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
        for flag in (
            "JAYTEC_READ_BOOTSTRAP_ENABLED",
            "JAYTEC_G1_INDEPENDENT_REVIEW_SERVER_ONESHOT",
            "JAYTEC_GOD_PROJECT_REVIEW_ENABLED",
            "JAYTEC_DEEPSEEK_SECURITY_REVIEW_ENABLED",
            "JAYTEC_DEEPSEEK_TRANSPORT_MATRIX_ENABLED",
            "JAYTEC_DEEPSEEK_ROUTE_VISIBILITY_ENABLED",
        ):
            with self.subTest(flag=flag):
                self.assertIn(f"- key: {flag}", blueprint)
        self.assertGreaterEqual(blueprint.count('value: "0"'), 6)

    def test_every_server_start_provider_probe_uses_one_shot_guard(self):
        source = (ROOT / "staging_server.py").read_text(encoding="utf-8")
        for probe in (
            '"jaytec_read"',
            '"independent_g1_review"',
            '"god_project_review"',
            '"deepseek_security_review"',
            '"deepseek_transport_matrix"',
            '"deepseek_route_visibility"',
        ):
            with self.subTest(probe=probe):
                self.assertIn(
                    f"_startup_probe_authorized({probe}",
                    source.replace("\n        ", ""),
                )

    def test_jaytec_read_requires_separate_enable_flag(self):
        source = (ROOT / "staging_server.py").read_text(encoding="utf-8")
        self.assertIn("JAYTEC_READ_BOOTSTRAP_ENABLED", source)
        self.assertIn('== "1"', source)


if __name__ == "__main__":
    unittest.main()
