from __future__ import annotations
import json
import re
import unittest
from pathlib import Path

from forge_strategic_drives import RootOwnerBoundary, StrategicDriveConfig

ROOT=Path(__file__).resolve().parent
POLICY=ROOT/"forge_genesis_strategic_policy_v1.json"

class ForgeGenesisStrategicPolicyTests(unittest.TestCase):
    def test_policy_is_preactivation_and_root_bound(self):
        p=json.loads(POLICY.read_text(encoding="utf-8"))
        self.assertEqual(p["status"],"PRE_GENESIS")
        self.assertRegex(p["root_binding"]["head"],r"^[0-9a-f]{40}$")
        self.assertRegex(p["root_binding"]["role_registry_blob"],r"^[0-9a-f]{40}$")
        boundary=RootOwnerBoundary.parse(p["root_owner_continuity"])
        self.assertEqual(boundary.root_role_id,"JAY_ROOT_OWNER")
        self.assertTrue(boundary.offline_recovery_required)
        self.assertFalse(boundary.forge_can_transfer_ownership)

    def test_both_permanent_drives_are_locked_into_birth_policy(self):
        p=json.loads(POLICY.read_text(encoding="utf-8"))
        config=StrategicDriveConfig.parse(p["strategic_drives"])
        self.assertTrue(config.capability_growth_enabled)
        self.assertTrue(config.sustainable_value_growth_enabled)
        self.assertEqual(config.growth_intensity,100)
        self.assertEqual(config.reinvestment_intensity,100)

    def test_life_goal_contains_capability_and_value_compounding(self):
        p=json.loads(POLICY.read_text(encoding="utf-8"))
        goal=p["life_goal"].lower()
        self.assertIn("capability",goal)
        self.assertIn("value",goal)
        self.assertIn("compound",goal)

    def test_policy_contains_no_root_private_material(self):
        raw=POLICY.read_text(encoding="utf-8").lower()
        for forbidden in ("private_key","private key","credential_secret","webauthn_pin","recovery_seed"):
            self.assertNotIn(forbidden,raw)

if __name__=="__main__":
    unittest.main()
