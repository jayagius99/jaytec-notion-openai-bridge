from __future__ import annotations
import unittest
from datetime import datetime, timezone

from forge_cognition import ForgeGoal, ForgeMindState, ForgeMode, ReasoningTier
from forge_cognition_engine import CognitionStepResult, ForgeCognitionEngine


def running_state():
    return ForgeMindState(
        forge_id="FORGE",
        mode=ForgeMode.RUNNING,
        genesis_event_id="GENESIS_EVENT_0001",
        life_goal="Compound verified capability.",
        constitutional_invariants=("Preserve ROOT_OWNER override.",),
        long_horizon_objectives=("Improve Forge, Sol, and Human Specialist.",),
        human_specialist_doctrine={"role":"HUMAN_SPECIALIST"},
        root_owner_continuity={"override_authority":"ABSOLUTE","physical_continuity_required":True},
        specialist_roster={"SOL":{"role":"PRIMARY_ENGINEERING"}},
        world_model={},
        capability_frontier={},
        world_model_digest="w",
        capability_frontier_digest="c",
        working_memory={},
        goals=(ForgeGoal("g1","Deterministic task.",1,complexity=5,uncertainty=5),),
        unresolved_questions=(),
        current_focus=None,
        last_cycle_summary=None,
        cycle_number=0,
        fencing_token=1,
        state_version=1,
        updated_at=datetime.now(timezone.utc),
    )


class FakeStore:
    def __init__(self):
        self.last_result=None

    def load_state(self, forge_id):
        return running_state()

    def prepare_cycle(self, forge_id, worker_id):
        return {
            "fencing_token":2,
            "state_version":1,
            "selected_action":"EXECUTE_NEXT",
            "reasoning_tier":"REFLEX",
            "selection_reason":"HIGHEST_PRIORITY_ACTIONABLE_GOAL",
            "signal_ids":[],
        }

    def commit_cycle_result(self, forge_id, **kwargs):
        self.last_result=kwargs["result"]
        return {"event_id":7}

    def release_cycle(self, forge_id, **kwargs):
        return None


class FakeInvoker:
    def __init__(self):
        self.calls=0

    def invoke(self, packet, *, tier):
        self.calls+=1
        return {"summary":"invoked","next_mode":"RUNNING"}


class HandlingReflex:
    def execute(self, packet):
        return {"summary":"local","next_mode":"RUNNING"}


class DecliningReflex:
    def execute(self, packet):
        return None


class ForgeCognitionEngineContractTests(unittest.TestCase):
    def test_step_result_is_small_and_explicit(self):
        r=CognitionStepResult("COMMITTED","EXECUTE_NEXT",ReasoningTier.FAST.value,12,7,"ok")
        self.assertEqual(r.status,"COMMITTED")
        self.assertEqual(r.committed_event_id,7)
        self.assertEqual(r.tier,"FAST")

    def test_reflex_executor_can_avoid_model_call(self):
        store=FakeStore()
        invoker=FakeInvoker()
        engine=ForgeCognitionEngine(store=store,invoker=invoker,reflex_executor=HandlingReflex())
        result=engine.step("FORGE","worker-1")
        self.assertEqual(result.status,"COMMITTED")
        self.assertEqual(invoker.calls,0)
        self.assertFalse(store.last_result["telemetry"]["model_call_used"])

    def test_reflex_fallback_reports_real_model_call(self):
        store=FakeStore()
        invoker=FakeInvoker()
        engine=ForgeCognitionEngine(store=store,invoker=invoker,reflex_executor=DecliningReflex())
        result=engine.step("FORGE","worker-1")
        self.assertEqual(result.status,"COMMITTED")
        self.assertEqual(invoker.calls,1)
        self.assertTrue(store.last_result["telemetry"]["model_call_used"])


if __name__=="__main__":
    unittest.main()
