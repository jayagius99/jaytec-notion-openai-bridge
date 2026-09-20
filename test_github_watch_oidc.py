import unittest

from github_watch_oidc import (
    WATCH_OWNER,
    WATCH_OWNER_ID,
    WATCH_REF,
    WATCH_REPOSITORY,
    WATCH_REPOSITORY_ID,
    WATCH_SUBJECT,
    WATCH_WORKFLOW,
    WATCH_WORKFLOW_REF,
    WATCH_OIDC_SCOPES,
    validate_watch_claims,
)


def claims(**overrides):
    value = {
        "sub": WATCH_SUBJECT,
        "repository": WATCH_REPOSITORY,
        "repository_id": WATCH_REPOSITORY_ID,
        "repository_owner": WATCH_OWNER,
        "repository_owner_id": WATCH_OWNER_ID,
        "repository_visibility": "private",
        "ref": WATCH_REF,
        "workflow": WATCH_WORKFLOW,
        "workflow_ref": WATCH_WORKFLOW_REF,
        "workflow_sha": "a" * 40,
        "sha": "b" * 40,
        "event_name": "schedule",
        "runner_environment": "github-hosted",
        "run_id": "123",
        "run_number": "7",
        "run_attempt": "1",
    }
    value.update(overrides)
    return value


class GitHubWatchOIDCTests(unittest.TestCase):
    def test_exact_watch_identity_is_accepted(self):
        self.assertEqual(
            validate_watch_claims(claims()),
            (True, "WATCH_OIDC_IDENTITY_VERIFIED"),
        )

    def test_repository_and_repository_id_are_both_pinned(self):
        for key, value in (
            ("repository", "attacker/repo"),
            ("repository_id", "1"),
        ):
            with self.subTest(key=key):
                ok, reason = validate_watch_claims(claims(**{key: value}))
                self.assertFalse(ok)
                self.assertIn("OIDC_CLAIM_MISMATCH", reason)

    def test_only_main_watch_workflow_is_accepted(self):
        for key, value in (
            ("ref", "refs/heads/evil"),
            ("workflow", "Other Workflow"),
            ("workflow_ref", "evil/repo/.github/workflows/x.yml@refs/heads/main"),
        ):
            with self.subTest(key=key):
                self.assertFalse(validate_watch_claims(claims(**{key: value}))[0])

    def test_legacy_mutable_subject_is_rejected_for_this_new_repository(self):
        legacy = f"repo:{WATCH_REPOSITORY}:ref:{WATCH_REF}"
        ok, reason = validate_watch_claims(claims(sub=legacy))
        self.assertFalse(ok)
        self.assertEqual(reason, "OIDC_SUBJECT_INVALID")

    def test_owner_identity_is_immutably_pinned(self):
        for key, value in (
            ("repository_owner", "renamed-owner"),
            ("repository_owner_id", "1"),
        ):
            with self.subTest(key=key):
                ok, reason = validate_watch_claims(claims(**{key: value}))
                self.assertFalse(ok)
                self.assertIn("OIDC_CLAIM_MISMATCH", reason)

    def test_watch_identity_receives_only_watch_and_proving_grounds_scopes(self):
        self.assertEqual(
            set(WATCH_OIDC_SCOPES),
            {"jaytec:watch-cycle", "jaytec:proving-grounds"},
        )

    def test_pull_request_identity_cannot_drive_recovery(self):
        ok, reason = validate_watch_claims(claims(event_name="pull_request"))
        self.assertFalse(ok)
        self.assertEqual(reason, "OIDC_EVENT_NOT_ALLOWED")

    def test_non_github_hosted_runner_is_rejected(self):
        self.assertFalse(
            validate_watch_claims(claims(runner_environment="self-hosted"))[0]
        )

    def test_required_run_identity_claims_cannot_be_blank(self):
        for key in ("run_id", "run_number", "run_attempt", "workflow_sha", "sha"):
            with self.subTest(key=key):
                ok, reason = validate_watch_claims(claims(**{key: ""}))
                self.assertFalse(ok)
                self.assertEqual(reason, "OIDC_CLAIM_MISSING:" + key)


if __name__ == "__main__":
    unittest.main()
