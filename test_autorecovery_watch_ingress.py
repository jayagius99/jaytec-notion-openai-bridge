import unittest
from unittest.mock import patch

from autorecovery_watch_ingress import (
    FORGE_TASK_ID,
    WatchIngressError,
    _observed_refs,
)


class WatchIngressPolicyTests(unittest.TestCase):
    def test_only_canonical_forge_task_is_allowed_by_constant(self):
        self.assertEqual(FORGE_TASK_ID, "FORGE-GENESIS-ACTIVATION-001")

    def test_observed_refs_are_bounded_and_nonempty(self):
        with self.assertRaisesRegex(WatchIngressError, "REQUIRED"):
            _observed_refs({})
        refs = _observed_refs({"main": "a" * 40})
        self.assertEqual(refs["main"], "a" * 40)

    def test_ref_count_is_bounded(self):
        with self.assertRaisesRegex(WatchIngressError, "TOO_MANY"):
            _observed_refs({f"r-{i}": "a" * 40 for i in range(129)})


if __name__ == "__main__":
    unittest.main()
