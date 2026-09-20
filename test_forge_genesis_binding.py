from __future__ import annotations
import json
import unittest
from pathlib import Path

from forge_genesis_binding import GenesisBindingError, load_policy, verify_genesis_bindings

ROOT=Path(__file__).resolve().parent

class GenesisBindingTests(unittest.TestCase):
    def test_current_policy_binding_passes_exact_values(self):
        p=load_policy()
        b=p["root_binding"]
        result=verify_genesis_bindings(
            p,
            current_root_head=b["head"],
            current_root_registry_blob=b["role_registry_blob"],
        )
        self.assertEqual(result["status"],"PASS")
        self.assertFalse(result["forge_activated"])

    def test_root_head_drift_fails_closed(self):
        p=load_policy()
        with self.assertRaisesRegex(GenesisBindingError,"ROOT_HEAD_DRIFT"):
            verify_genesis_bindings(
                p,
                current_root_head="0"*40,
                current_root_registry_blob=p["root_binding"]["role_registry_blob"],
            )

    def test_root_registry_drift_fails_closed(self):
        p=load_policy()
        with self.assertRaisesRegex(GenesisBindingError,"ROOT_REGISTRY_DRIFT"):
            verify_genesis_bindings(
                p,
                current_root_head=p["root_binding"]["head"],
                current_root_registry_blob="f"*40,
            )

if __name__=="__main__":
    unittest.main()
