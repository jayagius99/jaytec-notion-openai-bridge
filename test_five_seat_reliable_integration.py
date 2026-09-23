import os
import unittest
from pathlib import Path

import server
import reliable_server


class TestFiveSeatReliableServerIntegration(unittest.TestCase):
    def test_cutover_flag_is_default_off_and_dynamic(self):
        previous = os.environ.get("FIVE_SEAT_FABRIC_ENABLED")
        try:
            os.environ.pop("FIVE_SEAT_FABRIC_ENABLED", None)
            self.assertFalse(server.five_seat_fabric_enabled())
            os.environ["FIVE_SEAT_FABRIC_ENABLED"] = "1"
            self.assertTrue(server.five_seat_fabric_enabled())
            self.assertTrue(reliable_server._five_seat_enabled())
        finally:
            if previous is None:
                os.environ.pop("FIVE_SEAT_FABRIC_ENABLED", None)
            else:
                os.environ["FIVE_SEAT_FABRIC_ENABLED"] = previous

    def test_direct_packet_surface_fails_closed_when_fabric_enabled(self):
        source = Path(__file__).with_name("server.py").read_text(encoding="utf-8")
        self.assertIn("FIVE_SEAT_FABRIC_REQUIRES_DURABLE_SUBMIT", source)
        self.assertIn('"preferred_path": "submit_task_packet_five_seat"', source)

    def test_old_background_executor_is_suppressed_by_fabric(self):
        source = Path(__file__).with_name("reliable_server.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("if fabric_enabled:", source)
        self.assertIn("FIVE_SEAT_FABRIC_OWNS_EXECUTION", source)
        self.assertIn("legacy_durable_worker_suppressed_by_fabric", source)
        self.assertIn("submit_task_packet_five_seat", source)
        self.assertIn("five_seat_job_status", source)
        self.assertIn("watch_last_60_minutes", source)

    def test_low_risk_cutover_rejects_paid_or_side_effecting_packet_routes(self):
        source = Path(__file__).with_name("reliable_server.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('any(item not in {"codex", "gemini"} for item in plan)', source)
        self.assertIn("submit_low_risk_task_packet", source)
        policy = Path(__file__).with_name("five_seat_service.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("FIVE_SEAT_LOW_RISK_V1 requires side_effect_policy=none", policy)
        self.assertIn('"mode": "ZERO_SPEND"', policy)
        self.assertIn('"provider_mode": "FREE_ONLY"', policy)
        self.assertIn('authority_class="READ_ONLY"', policy)
        self.assertIn('concurrency_class="A"', policy)


if __name__ == "__main__":
    unittest.main()
