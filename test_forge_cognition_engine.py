from __future__ import annotations
import unittest
from forge_cognition import ReasoningTier
from forge_cognition_engine import CognitionStepResult

class ForgeCognitionEngineContractTests(unittest.TestCase):
    def test_step_result_is_small_and_explicit(self):
        r=CognitionStepResult("COMMITTED","EXECUTE_NEXT",ReasoningTier.FAST.value,12,7,"ok")
        self.assertEqual(r.status,"COMMITTED")
        self.assertEqual(r.committed_event_id,7)
        self.assertEqual(r.tier,"FAST")

if __name__=="__main__":
    unittest.main()
