import unittest
from pathlib import Path


class TestFiveSeatCourierIntegration(unittest.TestCase):
    def test_courier_keeps_one_tool_boundary_and_hosts_fabric_internally(self):
        source = Path(__file__).with_name("notion_courier_server.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('mcp = FastMCP("JAYTEC Notion Courier"', source)
        self.assertIn('names != ["collaborate"]', source)
        self.assertIn("legacy_server.five_seat_fabric_enabled()", source)
        self.assertIn("FiveSeatFabricService(", source)
        self.assertIn("self.fabric_service.verify_ready()", source)
        self.assertIn("self.fabric_service.start()", source)
        self.assertIn("FIVE_SEAT_FABRIC_STARTUP_REPORT", source)
        self.assertIn("JAYTEC_FIVE_SEAT_STARTUP_REPORT_V1", source)
        self.assertIn('"worker_threads_alive"', source)
        self.assertIn('"watch_leader_matches_instance"', source)
        self.assertIn("seat_ids == expected_seats", source)

    def test_enabled_courier_routes_packets_to_shared_ingress_not_direct_execution(self):
        source = Path(__file__).with_name("notion_courier_server.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("def _execute_direct", source)
        self.assertIn('if not getattr(self, "fabric_enabled", False):', source)
        self.assertIn("return self._execute_direct(packet_json)", source)
        self.assertIn("submit_low_risk_task_packet(", source)
        self.assertIn('"execution_mode": "WATCH_CONTROLLED_FIVE_SEAT"', source)
        self.assertIn('"result_delivery": "WATCH_ATTESTED_DURABLE_OUTBOX"', source)

    def test_fabric_status_exposes_watch_without_widening_courier_catalog(self):
        source = Path(__file__).with_name("notion_courier_server.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"five_seat_fabric_enabled": True', source)
        self.assertIn('"watch_summary": report.get("summary")', source)
        self.assertIn('"jay_action_required": report.get("jay_action_required")', source)
        self.assertIn('"recent_owner_notifications"', source)


if __name__ == "__main__":
    unittest.main()
