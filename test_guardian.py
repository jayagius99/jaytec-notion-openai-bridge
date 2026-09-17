import unittest

from guardian import (
    GuardianFinding,
    _stable_finding_id,
    capability_mismatch_from_event,
    duplicate_findings,
    repair_allowed,
)


def job(
    job_id,
    *,
    status="QUEUED",
    task_id="T1",
    assignment_type="BUILD",
    priority=100,
    mutation=None,
    resources=None,
):
    return {
        "job_id": job_id,
        "project_id": "P1",
        "task_id": task_id,
        "assignment_type": assignment_type,
        "status": status,
        "priority": priority,
        "created_at": f"2026-09-17T00:00:{priority % 60:02d}Z",
        "concurrency_class": "B",
        "mutation_scope": mutation or ["github/repo"],
        "read_scope": [],
        "resource_scope": resources or {},
        "dependencies": [],
    }


class TestGuardianHelpers(unittest.TestCase):
    def test_stable_finding_id_is_deterministic(self):
        first = _stable_finding_id("EXPIRED_JOB_LEASE", job_id="job-1")
        second = _stable_finding_id("EXPIRED_JOB_LEASE", job_id="job-1")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("guardian:"))

    def test_capability_mismatch_explicit_event(self):
        self.assertTrue(
            capability_mismatch_from_event(
                {"event_type": "SPECIALIST_EXECUTION_UNAVAILABLE", "payload": {}}
            )
        )

    def test_reasoning_without_execution_is_mismatch(self):
        self.assertTrue(
            capability_mismatch_from_event(
                {
                    "event_type": "SPECIALIST_CAPABILITY_REPORTED",
                    "payload": {
                        "reasoning_available": True,
                        "execution_available": False,
                        "writable_execution": False,
                    },
                }
            )
        )

    def test_full_reasoning_and_writable_execution_is_not_mismatch(self):
        self.assertFalse(
            capability_mismatch_from_event(
                {
                    "event_type": "SPECIALIST_CAPABILITY_REPORTED",
                    "payload": {
                        "reasoning_available": True,
                        "execution_available": True,
                        "writable_execution": True,
                    },
                }
            )
        )

    def test_only_mechanically_safe_findings_are_auto_repairable(self):
        for finding_type in (
            "EXPIRED_JOB_LEASE",
            "STALE_EXECUTION_ROOM",
            "DUPLICATE_ASSIGNMENT",
        ):
            self.assertTrue(
                repair_allowed(
                    GuardianFinding(
                        finding_id="x",
                        severity="ERROR",
                        finding_type=finding_type,
                        symptom="x",
                    )
                )
            )
        for finding_type in (
            "UNCERTAIN_PARTIAL_OPERATION",
            "BRIDGE_PROVIDER_FAILURE",
            "CHECKPOINT_FAILURE",
            "SPECIALIST_CAPABILITY_MISMATCH",
        ):
            self.assertFalse(
                repair_allowed(
                    GuardianFinding(
                        finding_id="x",
                        severity="ERROR",
                        finding_type=finding_type,
                        symptom="x",
                    )
                )
            )


class TestDuplicateFindings(unittest.TestCase):
    def test_overlapping_same_assignment_generates_finding(self):
        jobs = [
            job("keep", status="RUNNING", priority=10, mutation=["github/repo/src"]),
            job("block", status="QUEUED", priority=20, mutation=["github/repo/src/file.py"]),
        ]
        findings = duplicate_findings(jobs)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].finding_type, "DUPLICATE_ASSIGNMENT")
        self.assertEqual(findings[0].job_id, "block")
        self.assertEqual(findings[0].evidence["keep_job_id"], "keep")

    def test_disjoint_same_task_is_not_treated_as_duplicate(self):
        jobs = [
            job("left", priority=10, mutation=["github/repo-a"]),
            job("right", priority=20, mutation=["notion/page-b"]),
        ]
        self.assertEqual(duplicate_findings(jobs), [])

    def test_different_assignment_type_is_not_duplicate(self):
        jobs = [
            job("build", assignment_type="BUILD", mutation=["github/repo"]),
            job("review", assignment_type="REVIEW", mutation=["github/repo"]),
        ]
        self.assertEqual(duplicate_findings(jobs), [])


if __name__ == "__main__":
    unittest.main()
