import unittest
from pathlib import Path

from dispatch_authority_boundary import (
    BLOCK_REASON,
    evaluate_dispatch_boundary,
    production_dispatch_authority_integrated,
)


ROOT = Path(__file__).resolve().parent


class DispatchAuthorityBoundaryTests(unittest.TestCase):
    def test_production_dispatch_is_fail_closed_until_integrated(self):
        self.assertFalse(production_dispatch_authority_integrated())
        decision = evaluate_dispatch_boundary(runtime_mode="production")
        self.assertFalse(decision.allowed)
        self.assertEqual(BLOCK_REASON, decision.reason)

    def test_nonproduction_boundary_does_not_claim_v2_authority(self):
        decision = evaluate_dispatch_boundary(runtime_mode="staging_candidate")
        self.assertTrue(decision.allowed)

    def test_production_gate_has_no_environment_override(self):
        source = (ROOT / "dispatch_authority_boundary.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)
        self.assertIn("PRODUCTION_DISPATCH_AUTHORITY_INTEGRATED = False", source)

    def test_all_production_packet_tools_share_central_packet_guard(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        start = source.index("    def _packet_json(packet_json: str) -> str:")
        end = source.index("    # ---------------- Legacy compatibility surface", start)
        guarded = source[start:end]
        self.assertLess(
            guarded.index("evaluate_dispatch_boundary"),
            guarded.index("_execute_task_packet_json"),
        )
        self.assertIn(
            "_legacy_collaborate_command(task, _status_json, _packet_json)",
            source,
        )
        self.assertIn("return _packet_json(packet_json)", source)

    def test_staging_live_packet_dispatch_is_one_shot_guarded(self):
        source = (ROOT / "staging_server.py").read_text(encoding="utf-8")
        start = source.index("@mcp.tool\ndef execute_task_packet")
        end = source.index("\ndef _startup_probe_authorized", start)
        block = source[start:end]
        self.assertIn('"task_packet_dispatch"', block)
        self.assertIn("sha256_json(packet)", block)
        self.assertLess(
            block.index("_startup_probe_authorized"),
            block.index("execute_task_packet_core"),
        )
        self.assertIn(
            "ONE_SHOT_TASK_PACKET_DISPATCH_AUTH_REQUIRED",
            block,
        )


if __name__ == "__main__":
    unittest.main()
