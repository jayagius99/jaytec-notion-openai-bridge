import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class StartupProviderSafetyTests(unittest.TestCase):
    def test_render_startup_contains_no_provider_review_scripts(self):
        blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
        self.assertIn("startCommand: python staging_server.py", blueprint)
        self.assertIn(
            "buildCommand: pip install -r requirements.txt && python staging_adversarial_probe.py",
            blueprint,
        )
        self.assertNotIn(
            "startCommand: python staging_adversarial_probe.py",
            blueprint,
        )
        for forbidden in (
            "staging_independent_g1_review.py &&",
            "staging_gemini_manus_governance_review.py &&",
            "staging_manus_governance_acceptance.py &&",
            "staging_codex_429_diagnostic.py &&",
            "staging_engineering_provider_probe.py &&",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blueprint)

    def test_manus_startup_telemetry_exposes_only_nonsecret_message_budget(self):
        source = (ROOT / "staging_server.py").read_text(encoding="utf-8")
        self.assertIn('"max_message_chars": MANUS_MAX_MESSAGE_CHARS', source)
        self.assertIn(
            "from manus_adapter import MANUS_API_KEY, MANUS_MAX_MESSAGE_CHARS, ManusClient",
            source,
        )

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

    def test_watch_model_specialists_are_free_only_without_unlocking_legacy_openrouter(self):
        source = (ROOT / "staging_server.py").read_text(encoding="utf-8")
        blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
        deepseek = (ROOT / "deepseek_reviewer.py").read_text(encoding="utf-8")
        nemo = (ROOT / "nemo_specialist.py").read_text(encoding="utf-8")

        self.assertIn(
            'EXPECTED_DEEPSEEK_REVIEWER_MODEL = "deepseek/deepseek-v4-flash-0731:free"',
            deepseek,
        )
        self.assertIn(
            'EXPECTED_NEMO_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"',
            nemo,
        )
        self.assertIn('"ACTIVE_FREE_ONLY"', source)
        self.assertIn("WATCH_FREE_SPECIALISTS_CLIENT", source)
        self.assertIn(
            "- key: OPENROUTER_PROVIDER_MODE\n        value: LOCKED_RESERVE",
            blueprint,
        )
        self.assertIn("value: deepseek/deepseek-v4-flash-0731:free", blueprint)
        self.assertIn("value: nvidia/nemotron-3-ultra-550b-a55b:free", blueprint)
        self.assertEqual(blueprint.count("value: ACTIVE_FREE_ONLY"), 2)
        self.assertIn('if DEEPSEEK_PROVIDER_MODE == "ACTIVE_FREE_ONLY" and not DEEPSEEK_REVIEWER_MODEL.endswith(":free")', source)
        self.assertIn('if NEMO_PROVIDER_MODE == "ACTIVE_FREE_ONLY" and not NEMO_MODEL.endswith(":free")', source)

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


    def test_manual_provider_probes_require_one_shot_authorization_before_call(self):
        for name in (
            "staging_codex_429_diagnostic.py",
            "staging_engineering_provider_probe.py",
        ):
            with self.subTest(name=name):
                source = (ROOT / name).read_text(encoding="utf-8")
                guard = source.find("_startup_probe_authorized(")
                provider = source.find("responses.create(")
                self.assertGreaterEqual(guard, 0)
                self.assertGreater(provider, guard)
                self.assertIn("ONE_SHOT_AUTH_REQUIRED", source)

    def test_jaytec_read_requires_separate_enable_flag(self):
        source = (ROOT / "staging_server.py").read_text(encoding="utf-8")
        self.assertIn("JAYTEC_READ_BOOTSTRAP_ENABLED", source)
        self.assertIn('== "1"', source)


    def test_live_manus_certification_is_explicitly_flag_gated(self):
        source = (ROOT / "staging_adversarial_probe.py").read_text(encoding="utf-8")
        governance_flag = source.find('RUN_LIVE_MANUS_GOVERNANCE_ACCEPTANCE')
        governance_import = source.find(
            'from staging_manus_governance_acceptance import main as manus_governance_main'
        )
        usability_flag = source.find('RUN_LIVE_MANUS_USABILITY_EVAL')
        usability_import = source.find(
            'from staging_manus_usability_eval import main as manus_usability_main'
        )
        self.assertGreaterEqual(governance_flag, 0)
        self.assertGreater(governance_import, governance_flag)
        self.assertGreaterEqual(usability_flag, 0)
        self.assertGreater(usability_import, usability_flag)
        readback_flag = source.find('RUN_LIVE_MANUS_TASK_READBACK')
        readback_import = source.find(
            'from staging_manus_review_readback import main as manus_readback_main'
        )
        self.assertGreaterEqual(readback_flag, 0)
        self.assertGreater(readback_import, readback_flag)
        self.assertIn('== "1"', source)
        self.assertNotIn('return manus_readback_main()', source)


if __name__ == "__main__":
    unittest.main()
