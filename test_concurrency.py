import unittest

from concurrency import (
    ConcurrencyConfigurationError,
    concurrency_decision,
    duplicate_assignment,
    scopes_overlap,
    select_runnable_jobs,
)


def job(
    job_id,
    klass,
    *,
    project_id="P",
    task_id=None,
    assignment_type="GENERIC",
    mutation=None,
    read=None,
    resources=None,
    dependencies=None,
    priority=100,
):
    return {
        "job_id": job_id,
        "project_id": project_id,
        "task_id": task_id or job_id,
        "assignment_type": assignment_type,
        "concurrency_class": klass,
        "mutation_scope": mutation or [],
        "read_scope": read or [],
        "resource_scope": resources or {},
        "dependencies": dependencies or [],
        "priority": priority,
    }


class TestConcurrencyScopes(unittest.TestCase):
    def test_exact_scope_overlaps(self):
        self.assertTrue(scopes_overlap(["repo/a"], ["repo/a"]))

    def test_parent_scope_overlaps_child(self):
        self.assertTrue(scopes_overlap(["repo/src"], ["repo/src/server.py"]))

    def test_disjoint_scope_does_not_overlap(self):
        self.assertFalse(scopes_overlap(["repo/src"], ["notion/page/123"]))


class TestConcurrencyDecision(unittest.TestCase):
    def test_read_only_can_run_with_disjoint_mutator(self):
        candidate = job("read", "A", read=["notion/research"])
        active = job("build", "B", mutation=["github/repo-a"])
        decision = concurrency_decision(candidate, [active])
        self.assertTrue(decision.allowed)

    def test_reader_is_blocked_while_same_scope_is_mutated(self):
        candidate = job("read", "A", read=["github/repo-a/src"])
        active = job("build", "B", mutation=["github/repo-a/src/server.py"])
        decision = concurrency_decision(candidate, [active])
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.conflicts, ("build",))

    def test_disjoint_scoped_mutations_can_run_together(self):
        left = job("left", "B", mutation=["github/repo-a"])
        right = job("right", "B", mutation=["notion/page-b"])
        self.assertTrue(concurrency_decision(right, [left]).allowed)

    def test_overlapping_scoped_mutations_are_blocked(self):
        left = job("left", "B", mutation=["github/repo-a/src"])
        right = job("right", "B", mutation=["github/repo-a/src/file.py"])
        self.assertFalse(concurrency_decision(right, [left]).allowed)

    def test_shared_state_mutations_serialize(self):
        left = job("left", "C", mutation=["notion/shared-house"])
        right = job("right", "C", mutation=["notion/other-page"])
        decision = concurrency_decision(right, [left])
        self.assertFalse(decision.allowed)

    def test_external_side_effects_serialize_against_shared_state(self):
        active = job("state", "C", mutation=["notion/shared-house"])
        candidate = job("deploy", "D", resources={"render_service": "prod"})
        decision = concurrency_decision(candidate, [active])
        self.assertFalse(decision.allowed)

    def test_global_exclusive_requires_empty_runtime(self):
        candidate = job("global", "E", resources={"system": "jaytec"})
        active = job("read", "A", read=["docs"])
        decision = concurrency_decision(candidate, [active])
        self.assertFalse(decision.allowed)

    def test_mutating_job_without_scope_fails_closed(self):
        candidate = job("bad", "B")
        decision = concurrency_decision(candidate, [])
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "MUTATING_CLASS_REQUIRES_DECLARED_SCOPE")

    def test_class_a_with_mutation_fails_closed(self):
        candidate = job("bad", "A", mutation=["repo"])
        decision = concurrency_decision(candidate, [])
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "CLASS_A_MUST_BE_READ_ONLY")

    def test_unknown_class_raises_configuration_error(self):
        candidate = job("bad", "Z", mutation=["repo"])
        with self.assertRaises(ConcurrencyConfigurationError):
            concurrency_decision(candidate, [])

    def test_dependencies_must_be_complete(self):
        candidate = job("next", "A", read=["docs"], dependencies=["first"])
        blocked = concurrency_decision(candidate, [], completed_job_ids=[])
        allowed = concurrency_decision(candidate, [], completed_job_ids=["first"])
        self.assertFalse(blocked.allowed)
        self.assertTrue(allowed.allowed)

    def test_unresolved_side_effect_job_blocks_candidate(self):
        active = job("uncertain", "B", mutation=["github/repo-a"])
        candidate = job("other", "B", mutation=["notion/page-b"])
        decision = concurrency_decision(
            candidate,
            [active],
            unresolved_side_effect_job_ids=["uncertain"],
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.conflicts, ("uncertain",))


class TestBatchSelection(unittest.TestCase):
    def test_batch_selects_disjoint_jobs(self):
        queued = [
            job("a", "B", mutation=["github/a"], priority=10),
            job("b", "B", mutation=["notion/b"], priority=20),
        ]
        selected = select_runnable_jobs(queued, [], max_parallel=4)
        self.assertEqual([item["job_id"] for item in selected], ["a", "b"])

    def test_batch_does_not_select_two_conflicting_jobs(self):
        queued = [
            job("a", "B", mutation=["github/repo/src"], priority=10),
            job("b", "B", mutation=["github/repo/src/file.py"], priority=20),
        ]
        selected = select_runnable_jobs(queued, [], max_parallel=4)
        self.assertEqual([item["job_id"] for item in selected], ["a"])

    def test_max_parallel_counts_existing_active_jobs(self):
        active = [job("running", "A", read=["docs"])]
        queued = [
            job("a", "A", read=["x"], priority=10),
            job("b", "A", read=["y"], priority=20),
        ]
        selected = select_runnable_jobs(queued, active, max_parallel=2)
        self.assertEqual([item["job_id"] for item in selected], ["a"])

    def test_duplicate_assignment_is_skipped(self):
        active = [
            job(
                "active",
                "B",
                task_id="T1",
                assignment_type="BUILD",
                mutation=["github/repo"],
            )
        ]
        candidate = job(
            "duplicate",
            "B",
            task_id="T1",
            assignment_type="BUILD",
            mutation=["github/repo/src"],
        )
        self.assertEqual(duplicate_assignment(candidate, active), "active")
        selected = select_runnable_jobs([candidate], active, max_parallel=4)
        self.assertEqual(selected, [])


if __name__ == "__main__":
    unittest.main()
