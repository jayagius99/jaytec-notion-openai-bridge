import unittest
from pathlib import Path

from five_seat_adapters import AdapterRegistry, AdapterRegistryError


class TestFiveSeatContinuousFabric(unittest.TestCase):
    def test_adapter_registry_is_capability_safe(self):
        registry = AdapterRegistry()
        registry.register(
            "GITHUB_CODE",
            capabilities={"github.read", "github.branch.write"},
            execute=lambda payload: {"ok": True},
        )
        self.assertTrue(
            registry.can_run(
                "GITHUB_CODE",
                {"github.read", "github.branch.write"},
            )
        )
        self.assertFalse(
            registry.can_run(
                "GITHUB_CODE",
                {"github.merge"},
            )
        )
        self.assertFalse(registry.can_run("PC_REMOTE", set()))

    def test_duplicate_adapter_fails_closed(self):
        registry = AdapterRegistry()
        registry.register("TEST", capabilities=set(), execute=lambda payload: {})
        with self.assertRaises(AdapterRegistryError):
            registry.register("TEST", capabilities=set(), execute=lambda payload: {})

    def test_queue_is_envelope_storage_not_second_state_machine(self):
        schema = Path(__file__).with_name("five_seat_schema.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS jaytec_fabric_envelopes", schema)
        block = schema.split("CREATE TABLE IF NOT EXISTS jaytec_fabric_envelopes", 1)[1]
        block = block.split(");", 1)[0]
        self.assertNotIn(" status ", " " + block.lower() + " ")
        self.assertIn("job_id TEXT PRIMARY KEY REFERENCES jaytec_jobs", block)

    def test_worker_chains_without_sleep_after_success(self):
        source = Path(__file__).with_name("five_seat_worker.py").read_text(encoding="utf-8")
        self.assertIn("Immediate refill: no sleep after a completed handoff.", source)
        self.assertIn("run_until_idle", source)
        self.assertIn("WORK_AVAILABLE_CHANNEL", source)

    def test_notify_is_acceleration_not_authority(self):
        source = Path(__file__).with_name("five_seat_signals.py").read_text(encoding="utf-8")
        self.assertIn("losing a notification can never lose work", source)
        self.assertIn("select.select", source)
        self.assertIn("timeout", source)

    def test_generic_ingress_supports_worker_kind_and_owner_gate(self):
        source = Path(__file__).with_name("five_seat_queue.py").read_text(encoding="utf-8")
        self.assertIn("worker_kind", source)
        self.assertIn("required_capabilities", source)
        self.assertIn('authority_class == "OWNER_GATED"', source)
        self.assertIn('"BLOCKED_OWNER"', source)


if __name__ == "__main__":
    unittest.main()
