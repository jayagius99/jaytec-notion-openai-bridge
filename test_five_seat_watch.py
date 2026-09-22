import unittest
from pathlib import Path
from five_seat_watch import WATCH_DECISIONS, evidence_hash

class TestFiveSeatWatchContract(unittest.TestCase):
    def test_decisions_are_exact(self):
        self.assertEqual(WATCH_DECISIONS, frozenset({'ACCEPT','REWORK','BLOCK','ESCALATE'}))
    def test_evidence_hash_is_stable(self):
        self.assertEqual(evidence_hash({'b':2,'a':1}), evidence_hash({'a':1,'b':2}))
    def test_schema_has_singleton_watch_not_sixth_seat(self):
        s=Path(__file__).with_name('five_seat_schema.sql').read_text(encoding='utf-8')
        self.assertIn('jaytec_watch_leader',s)
        self.assertIn('CHECK (singleton_id=1)',s)
        self.assertNotIn("'WORKER-SEAT-6'",s)
    def test_handoff_is_immutable_candidate_evidence(self):
        s=Path(__file__).with_name('five_seat_schema.sql').read_text(encoding='utf-8')
        self.assertIn('jaytec_worker_handoffs',s)
        self.assertIn('UNIQUE(job_id,job_ownership_epoch,job_fence_token)',s)
        runtime=Path(__file__).with_name('five_seat_watch.py').read_text(encoding='utf-8')
        self.assertNotIn('UPDATE jaytec_worker_handoffs',runtime)
    def test_no_self_approval_and_fenced_review(self):
        r=Path(__file__).with_name('five_seat_watch.py').read_text(encoding='utf-8')
        self.assertIn('self_approval_forbidden',r)
        self.assertIn('leader_epoch',r)
        self.assertIn('fence_token',r)
        self.assertIn("fabric_state='HANDOFF_PENDING_REVIEW'",r)
    def test_worker_retires_before_review(self):
        r=Path(__file__).with_name('five_seat_watch.py').read_text(encoding='utf-8')
        self.assertIn("SET state='FREE'",r)
        self.assertIn("fabric_state='HANDOFF_PENDING_REVIEW'",r)
        self.assertIn('jaytec_watch_reviews',r)
    def test_unresolved_side_effects_block_handoff(self):
        r=Path(__file__).with_name('five_seat_watch.py').read_text(encoding='utf-8')
        self.assertIn('UNRESOLVED_OPERATION_STATUSES',r)
        self.assertIn('unresolved_operations:',r)

if __name__=='__main__': unittest.main()
