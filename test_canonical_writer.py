import json
import os
import unittest

import psycopg2

from canonical_writer import (
    CANONICAL_WRITER_ID,
    CanonicalWriterAuthorityError,
    PostgresCanonicalWriterQueue,
)


DATABASE_URL = os.environ.get("DATABASE_URL", "")


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL required for canonical writer integration proof")
class TestCanonicalWriterIntegration(unittest.TestCase):
    def _seed_candidate(self, suffix, *, canonical=True):
        job_id = f"ci-canonical-job-{suffix}"
        handoff_id = f"ci-canonical-handoff-{suffix}"
        review_id = f"ci-canonical-review-{suffix}"
        repository = "jayagius99/jaytec-notion-openai-bridge"
        head = (suffix[0].lower() if suffix[0].lower() in "abcdef" else "a") * 40
        base = "b" * 40
        result = {
            "whole_packet_status": "SUCCESS",
            "partial_side_effect_status": "VERIFIED_COMPLETE",
            "unresolved_items": [],
            "watch_verification": {
                "repository": repository,
                "branch": f"watch/worker-1-ci-{suffix}",
                "head_sha": head,
                "base_branch": "main",
                "base_sha": base,
                "pr_number": 900 + len(suffix),
                "file_digests": {"ci.txt": "d" * 64},
            },
        }
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO jaytec_jobs(
                      job_id,project_id,task_id,assignment_type,objective,status,
                      priority,source_shared_state_version,concurrency_class,
                      mutation_scope,read_scope,dependencies,resource_scope,
                      fabric_state,checkpoint_ref,health
                    ) VALUES (
                      %s,'CI',%s,'FIVE_SEAT_FABRIC','canonical-writer-ci',
                      'SUCCEEDED',10,1,'B','[]','[]','[]','{}',
                      'SUCCEEDED',%s,'HEALTHY'
                    )
                    """,
                    (job_id, "task-" + suffix, handoff_id),
                )
                cur.execute(
                    """
                    INSERT INTO jaytec_fabric_envelopes(
                      job_id,envelope_hash,idempotency_key,worker_kind,
                      required_capabilities,approval_required,authority_class,
                      cost_policy,evidence_standard,stop_conditions,
                      result_destination,payload
                    ) VALUES (
                      %s,%s,%s,'GITHUB_BRANCH_PR','["jaytec.github.branch_pr"]',
                      TRUE,'EXTERNAL_SIDE_EFFECT',
                      '{"mode":"ZERO_SPEND","allow_paid":false,"max_cost_usd":0,"provider_mode":"NO_PROVIDER"}',
                      '{"watch_review_required":true}','{}',%s::jsonb,'{}'
                    )
                    """,
                    (
                        job_id,
                        "env-" + suffix,
                        "env-idem-" + suffix,
                        json.dumps(
                            {
                                "type": (
                                    "CANONICAL_WRITE_CANDIDATE"
                                    if canonical
                                    else "WATCH_ATTESTED_GITHUB_PR"
                                ),
                                "task_id": "task-" + suffix,
                            }
                        ),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO jaytec_worker_handoffs(
                      handoff_id,job_id,seat_id,worker_id,ownership_epoch,
                      job_fence_token,seat_epoch,seat_fence_token,
                      task_packet_hash,handoff_digest,payload
                    ) VALUES (
                      %s,%s,'WORKER-SEAT-1','ci-worker',1,1,1,1,
                      NULL,%s,%s::jsonb
                    )
                    """,
                    (
                        handoff_id,
                        job_id,
                        ("handoff-" + suffix).ljust(64, "0")[:64],
                        json.dumps(
                            {
                                "result": result,
                                "partial_side_effect_status": "VERIFIED_COMPLETE",
                                "unresolved_items": [],
                            }
                        ),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO jaytec_watch_reviews(
                      review_id,job_id,handoff_id,controller_id,controller_owner,
                      leader_epoch,fence_token,decision,reason,evidence
                    ) VALUES (
                      %s,%s,%s,'WATCH','five-seat-watch:ci',1,1,'ACCEPT',
                      'ci accepted','{"independent_watch_review":true}'
                    )
                    """,
                    (review_id, job_id, handoff_id),
                )
        return {
            "job_id": job_id,
            "handoff_id": handoff_id,
            "review_id": review_id,
            "repository": repository,
            "head": head,
            "base": base,
            "result": result,
        }

    def test_single_writer_queue_fencing_and_ledger(self):
        queue = PostgresCanonicalWriterQueue(DATABASE_URL)
        ready = queue.verify_ready()
        self.assertTrue(ready["ready"])

        first = self._seed_candidate("a1", canonical=True)
        candidate_digest = __import__("hashlib").sha256(
            json.dumps(
                first["result"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        item = queue.enqueue_from_watch(
            job_id=first["job_id"],
            handoff_id=first["handoff_id"],
            review_id=first["review_id"],
            repository=first["repository"],
            pull_request_number=902,
            candidate_head_sha=first["head"],
            expected_base_sha=first["base"],
            candidate_digest=candidate_digest,
            source_shared_state_version=1,
            idempotency_key="ci-canonical-write-a1",
            requested_by="five-seat-watch:ci",
            priority=10,
        )
        replay = queue.enqueue_from_watch(
            job_id=first["job_id"],
            handoff_id=first["handoff_id"],
            review_id=first["review_id"],
            repository=first["repository"],
            pull_request_number=902,
            candidate_head_sha=first["head"],
            expected_base_sha=first["base"],
            candidate_digest=candidate_digest,
            source_shared_state_version=1,
            idempotency_key="ci-canonical-write-a1",
            requested_by="five-seat-watch:ci",
            priority=10,
        )
        self.assertEqual(item["write_id"], replay["write_id"])
        self.assertEqual(item["state"], "PENDING_CHATGPT_APPROVAL")

        with self.assertRaises(CanonicalWriterAuthorityError):
            queue.approve(
                item["write_id"],
                approved_by="WORKER-SEAT-1",
                approval_ref="forbidden",
                expected_candidate_digest=candidate_digest,
                expected_base_sha=first["base"],
                expected_source_shared_state_version=1,
            )

        approved = queue.approve(
            item["write_id"],
            approved_by="CHATGPT",
            approval_ref="ci-owner-control",
            expected_candidate_digest=candidate_digest,
            expected_base_sha=first["base"],
            expected_source_shared_state_version=1,
        )
        self.assertEqual(approved["state"], "APPROVED")

        with self.assertRaises(CanonicalWriterAuthorityError):
            queue.claim_next(owner="WATCH", lease_seconds=120)

        token = queue.claim_next(
            owner=CANONICAL_WRITER_ID,
            lease_seconds=120,
        )
        self.assertIsNotNone(token)
        ticket = queue.active_ticket(token)
        self.assertEqual(ticket["write_id"], item["write_id"])
        renewed = queue.heartbeat(token, lease_seconds=120)
        self.assertEqual(renewed.writer_epoch, token.writer_epoch)
        self.assertEqual(renewed.fence_token, token.fence_token)

        with self.assertRaises(ValueError):
            queue.complete(
                renewed,
                outcome="VERIFIED_COMPLETE",
                canonical_commit_sha="e" * 40,
                verification_evidence={"healthy": True},
            )

        completed = queue.complete(
            renewed,
            outcome="VERIFIED_COMPLETE",
            canonical_commit_sha="e" * 40,
            verification_evidence={
                "healthy": True,
                "candidate_head_verified": True,
                "expected_base_verified": True,
                "canonical_ref_verified": True,
            },
        )
        self.assertEqual(completed["state"], "VERIFIED_COMPLETE")
        self.assertTrue(queue.verify_ledger_chain()["valid"])

        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                with self.assertRaises(psycopg2.Error):
                    cur.execute(
                        """
                        UPDATE jaytec_canonical_write_ledger
                        SET actor='tampered'
                        WHERE write_id=%s
                        """,
                        (item["write_id"],),
                    )
                conn.rollback()

        second = self._seed_candidate("a2", canonical=True)
        third = self._seed_candidate("a3", canonical=False)
        reconciled = queue.reconcile_watch_accepts(limit=100)
        queued_ids = set(reconciled["enqueued"])
        rows = queue.list_queue(limit=100)
        by_job = {row["job_id"]: row for row in rows}
        self.assertIn(second["job_id"], by_job)
        self.assertIn(by_job[second["job_id"]]["write_id"], queued_ids)
        self.assertNotIn(third["job_id"], by_job)

        second_item = by_job[second["job_id"]]
        queue.approve(
            second_item["write_id"],
            approved_by="ROOT_OWNER",
            approval_ref="ci-root-owner",
            expected_candidate_digest=second_item["candidate_digest"],
            expected_base_sha=second_item["expected_base_sha"],
            expected_source_shared_state_version=1,
        )
        token2 = queue.claim_next(owner=CANONICAL_WRITER_ID, lease_seconds=30)
        self.assertIsNotNone(token2)
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jaytec_canonical_writer_state
                    SET lease_expires_at=now()-interval '1 second'
                    WHERE writer_id='CANONICAL'
                    """
                )
        contained = queue.contain_expired_inflight()
        self.assertEqual(contained, [second_item["write_id"]])
        state = {
            row["write_id"]: row["state"]
            for row in queue.list_queue(limit=100)
        }
        self.assertEqual(state[second_item["write_id"]], "QUARANTINED")
        self.assertTrue(queue.verify_ledger_chain()["valid"])


class TestCanonicalWriterStaticContract(unittest.TestCase):
    def test_no_network_shell_merge_or_deploy_client_in_writer_module(self):
        source = open("canonical_writer.py", encoding="utf-8").read()
        forbidden = (
            "import urllib",
            "import requests",
            "import subprocess",
            "os.system",
            "/merge",
            "/deployments",
            "workflow_dispatch",
        )
        for value in forbidden:
            self.assertNotIn(value, source)

    def test_schema_enforces_single_inflight_and_append_only_ledger(self):
        schema = open("five_seat_schema.sql", encoding="utf-8").read()
        self.assertIn("jaytec_canonical_write_one_inflight_idx", schema)
        self.assertIn("WHERE state='IN_FLIGHT'", schema)
        self.assertIn("jaytec_canonical_ledger_no_update", schema)
        self.assertIn("jaytec_canonical_ledger_no_delete", schema)

    def test_github_canonical_intent_is_explicit(self):
        source = open("five_seat_github_broker.py", encoding="utf-8").read()
        self.assertIn("canonical_write_intent: bool = False", source)
        self.assertIn('"CANONICAL_WRITE_CANDIDATE"', source)
        self.assertIn('"WATCH_ATTESTED_GITHUB_PR"', source)


if __name__ == "__main__":
    unittest.main()
