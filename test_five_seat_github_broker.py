import base64
import json
import unittest

from five_seat_adapters import PermanentAdapterError, UncertainSideEffectError
from five_seat_github_broker import (
    GitHubBranchPrBroker,
    GitHubBrokerConfig,
)


BASE_SHA = "a" * 40
FILE_SHA = "b" * 40
COMMIT_SHA = "c" * 40


class FakeGitHub:
    def __init__(self):
        self.base_head = BASE_SHA
        self.branch = None
        self.branch_head = None
        self.files = {}
        self.pr = None
        self.calls = []
        self.fail_next_put = False

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        assert headers["Authorization"] == "Bearer test-token"

        if "/git/ref/heads/" in url and method == "GET":
            if url.endswith("/git/ref/heads/main"):
                return 200, {"object": {"sha": self.base_head}}
            if self.branch is None:
                return 404, {"message": "not found"}
            return 200, {"object": {"sha": self.branch_head}}

        if url.endswith("/git/refs") and method == "POST":
            self.branch = body["ref"].split("refs/heads/", 1)[1]
            self.branch_head = body["sha"]
            return 201, {"object": {"sha": self.branch_head}}

        if "/compare/" in url and method == "GET":
            return 200, {
                "merge_base_commit": {"sha": BASE_SHA},
                "files": [
                    {"filename": path, "status": "modified"}
                    for path in sorted(self.files)
                ],
            }

        if "/contents/" in url and method == "GET":
            path = url.split("/contents/", 1)[1].split("?", 1)[0]
            from urllib.parse import unquote
            path = unquote(path)
            if path not in self.files:
                return 404, {"message": "not found"}
            row = self.files[path]
            return 200, {
                "sha": row["sha"],
                "content": base64.b64encode(row["content"].encode()).decode(),
            }

        if "/contents/" in url and method == "PUT":
            if self.fail_next_put:
                self.fail_next_put = False
                return 500, {"message": "boom"}
            path = url.split("/contents/", 1)[1]
            from urllib.parse import unquote
            path = unquote(path)
            text = base64.b64decode(body["content"]).decode()
            sha = FILE_SHA
            self.files[path] = {"sha": sha, "content": text}
            self.branch_head = COMMIT_SHA
            return 200, {
                "content": {"sha": sha},
                "commit": {"sha": COMMIT_SHA},
            }

        if "/pulls?" in url and method == "GET":
            return 200, [self.pr] if self.pr else []

        if url.endswith("/pulls") and method == "POST":
            self.pr = {
                "number": 7,
                "html_url": "https://github.example/pr/7",
                "state": "open",
                "merged": False,
                "head": {"ref": body["head"], "sha": self.branch_head},
                "base": {"ref": body["base"]},
            }
            return 201, self.pr

        if url.endswith("/pulls/7") and method == "GET":
            return 200, dict(self.pr)

        raise AssertionError(f"unexpected transport call: {method} {url}")


def payload(*, mutation_scope=None, path="app/demo.py", expected_sha=""):
    return {
        "repository": "jayagius99/demo",
        "base_branch": "main",
        "base_sha": BASE_SHA,
        "branch_slug": "demo-safe-change",
        "files": [
            {
                "path": path,
                "content": "print('safe')\n",
                "expected_sha": expected_sha,
            }
        ],
        "pull_request": {
            "title": "Safe bounded change",
            "body": "Worker candidate; WATCH must independently verify.",
        },
        "_fabric_context": {
            "job_id": "fabric-job-1",
            "job_fence_token": 12,
            "seat_id": "WORKER-SEAT-2",
            "seat_fence_token": 44,
            "mutation_scope": mutation_scope
            or [
                "github:jayagius99/demo:path:app/demo.py",
                "github:jayagius99/demo:pr",
            ],
            "resource_scope": {
                "github_repository": "jayagius99/demo",
                "base_branch": "main",
                "base_sha": BASE_SHA,
            },
        },
    }


class TestFiveSeatGitHubBroker(unittest.TestCase):
    def broker(self, fake=None):
        fake = fake or FakeGitHub()
        config = GitHubBrokerConfig.build(
            "test-token",
            ["jayagius99/demo"],
        )
        return GitHubBranchPrBroker(config, transport=fake), fake

    def test_branch_file_pr_and_independent_watch_readback(self):
        broker, fake = self.broker()
        result = broker.execute(payload())

        self.assertEqual(result["whole_packet_status"], "SUCCESS")
        self.assertEqual(
            result["artifacts"][0]["branch"],
            "watch/worker-12-demo-safe-change",
        )
        self.assertEqual(result["artifacts"][0]["number"], 7)
        self.assertEqual(result["provider_identity"], "JAYTEC_GITHUB_BROKER")

        verified = broker.verify_result(result)
        self.assertTrue(verified["github_readback_verified"])
        self.assertEqual(verified["head_sha"], COMMIT_SHA)
        self.assertEqual(verified["pr_number"], 7)

        urls = [url for _method, url, _headers, _body in fake.calls]
        self.assertFalse(any(url.endswith("/merge") for url in urls))
        self.assertFalse(any("/actions/" in url for url in urls))

    def test_missing_mutation_scope_fails_before_side_effect(self):
        broker, fake = self.broker()
        with self.assertRaises(PermanentAdapterError):
            broker.execute(
                payload(
                    mutation_scope=[
                        "github:jayagius99/demo:pr",
                    ]
                )
            )
        self.assertEqual(fake.calls, [])

    def test_workflow_and_secret_paths_are_forbidden(self):
        broker, fake = self.broker()
        for forbidden in (
            ".github/workflows/evil.yml",
            ".env",
            "config/private_key.pem",
        ):
            with self.subTest(path=forbidden):
                with self.assertRaises(PermanentAdapterError):
                    broker.execute(
                        payload(
                            path=forbidden,
                            mutation_scope=[
                                f"github:jayagius99/demo:path:{forbidden}",
                                "github:jayagius99/demo:pr",
                            ],
                        )
                    )
        self.assertEqual(fake.calls, [])

    def test_existing_file_requires_expected_sha(self):
        broker, fake = self.broker()
        fake.branch = "watch/worker-12-demo-safe-change"
        fake.branch_head = BASE_SHA
        fake.files["app/demo.py"] = {
            "sha": FILE_SHA,
            "content": "old\n",
        }
        with self.assertRaises(UncertainSideEffectError):
            broker.execute(payload(expected_sha=""))
        self.assertFalse(any(method == "PUT" for method, *_rest in fake.calls))

    def test_stale_expected_sha_fails_closed(self):
        broker, fake = self.broker()
        fake.branch = "watch/worker-12-demo-safe-change"
        fake.branch_head = BASE_SHA
        fake.files["app/demo.py"] = {
            "sha": FILE_SHA,
            "content": "old\n",
        }
        with self.assertRaises(UncertainSideEffectError):
            broker.execute(payload(expected_sha="d" * 40))
        self.assertFalse(any(method == "PUT" for method, *_rest in fake.calls))

    def test_failure_after_branch_creation_is_uncertain_not_retried(self):
        broker, fake = self.broker()
        fake.fail_next_put = True
        with self.assertRaises(UncertainSideEffectError):
            broker.execute(payload())
        self.assertIsNotNone(fake.branch)
        self.assertIsNone(fake.pr)

    def test_existing_worker_branch_with_out_of_scope_diff_is_rejected(self):
        broker, fake = self.broker()
        fake.branch = "watch/worker-12-demo-safe-change"
        fake.branch_head = COMMIT_SHA
        fake.files["unexpected.py"] = {
            "sha": FILE_SHA,
            "content": "bad\n",
        }
        with self.assertRaises(UncertainSideEffectError):
            broker.execute(payload())
        self.assertFalse(any(method == "PUT" for method, *_rest in fake.calls))

    def test_base_head_move_fails_before_side_effect(self):
        broker, fake = self.broker()
        fake.base_head = "d" * 40
        with self.assertRaises(PermanentAdapterError):
            broker.execute(payload())
        self.assertIsNone(fake.branch)
        self.assertFalse(any(method in {"POST", "PUT"} for method, *_rest in fake.calls))

    def test_repository_allowlist_is_hard_boundary(self):
        broker, fake = self.broker()
        item = payload()
        item["repository"] = "jayagius99/other"
        with self.assertRaises(PermanentAdapterError):
            broker.execute(item)
        self.assertEqual(fake.calls, [])

    def test_worker_cannot_forge_fence_branch(self):
        broker, fake = self.broker()
        item = payload()
        item["branch_slug"] = "../main"
        with self.assertRaises(PermanentAdapterError):
            broker.execute(item)
        self.assertEqual(fake.calls, [])

    def test_watch_readback_rejects_tampered_head(self):
        broker, _fake = self.broker()
        result = broker.execute(payload())
        result["watch_verification"]["head_sha"] = "d" * 40
        with self.assertRaises(Exception):
            broker.verify_result(result)


class TestFs09StaticIntegration(unittest.TestCase):
    def test_worker_overwrites_runtime_context(self):
        source = open("five_seat_worker.py", encoding="utf-8").read()
        self.assertIn('adapter_payload["_fabric_context"] = {', source)
        self.assertIn('"job_fence_token": token.job_fence_token', source)
        self.assertIn('"seat_fence_token": token.seat_fence_token', source)

    def test_watch_uses_independent_github_readback(self):
        source = open("five_seat_service.py", encoding="utf-8").read()
        self.assertIn("self.github_broker.verify_result(result)", source)
        self.assertIn("github independent readback failed", source)

    def test_broker_is_explicit_opt_in(self):
        source = open("reliable_server.py", encoding="utf-8").read()
        self.assertIn('FIVE_SEAT_GITHUB_BROKER_ENABLED', source)
        self.assertIn("FIVE_SEAT_GITHUB_BROKER_ENABLED = os.environ.get(", source)
        self.assertIn('"FIVE_SEAT_GITHUB_BROKER_ENABLED", "0"', source)
        self.assertIn("if FIVE_SEAT_GITHUB_BROKER_ENABLED:", source)

    def test_github_jobs_are_durable_owner_gated_before_claim(self):
        source = open("five_seat_github_broker.py", encoding="utf-8").read()
        self.assertIn('authority_class="EXTERNAL_SIDE_EFFECT"', source)

        authority = open("five_seat_authority.py", encoding="utf-8").read()
        self.assertIn('"EXTERNAL_SIDE_EFFECT"', authority)

        queue = open("five_seat_queue.py", encoding="utf-8").read()
        self.assertIn(
            'initial_fabric_state = "BLOCKED_OWNER" if approval_required else "QUEUED"',
            queue,
        )
        self.assertIn("if not approval_required:", queue)

        runtime = open("five_seat_runtime.py", encoding="utf-8").read()
        self.assertIn("if required:", runtime)
        self.assertIn('if not row.get("approval_id"):', runtime)

    def test_no_merge_or_deploy_surface(self):
        source = open("five_seat_github_broker.py", encoding="utf-8").read()
        self.assertNotIn('"/merge"', source)
        self.assertNotIn("/deployments", source)
        self.assertNotIn("workflow_dispatch", source)


if __name__ == "__main__":
    unittest.main()
