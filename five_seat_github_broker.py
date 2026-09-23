from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

from five_seat_adapters import PermanentAdapterError, UncertainSideEffectError
from five_seat_queue import PostgresFabricQueue


GITHUB_WORKER_KIND = "GITHUB_BRANCH_PR"
GITHUB_WORK_CAPABILITY = "jaytec.github.branch_pr"
MAX_FILES = 10
MAX_FILE_BYTES = 50_000
_BRANCH_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9._/@+-][A-Za-z0-9._/@+ -]{0,299}$")
_BLOCKED_PATH_RE = re.compile(
    r"(^|/)(?:\.env(?:\.|$)|.*(?:secret|credential|private[_-]?key).*|"
    r"id_rsa(?:\.|$)|.*\.(?:pem|key|p12|pfx))",
    re.I,
)
_BLOCKED_PREFIXES = (
    ".github/workflows/",
    ".github/actions/",
)


class GitHubBrokerError(RuntimeError):
    pass


class GitHubBrokerPolicyError(GitHubBrokerError):
    pass


@dataclass(frozen=True)
class GitHubBrokerConfig:
    token: str
    allowed_repositories: frozenset[str]

    @classmethod
    def build(
        cls,
        token: str,
        allowed_repositories: Iterable[str],
    ) -> "GitHubBrokerConfig":
        clean_token = str(token or "").strip()
        repos = frozenset(
            str(item or "").strip()
            for item in allowed_repositories
            if str(item or "").strip()
        )
        if not clean_token:
            raise GitHubBrokerPolicyError("github_broker_token_missing")
        if not repos:
            raise GitHubBrokerPolicyError("github_broker_repository_allowlist_empty")
        if any("/" not in repo or repo.startswith("/") or repo.endswith("/") for repo in repos):
            raise GitHubBrokerPolicyError("github_broker_repository_invalid")
        return cls(clean_token, repos)


Transport = Callable[[str, str, Mapping[str, str], Optional[Mapping[str, Any]]], tuple[int, Any]]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_path(value: Any) -> str:
    path = str(value or "").strip()
    if (
        not path
        or not _PATH_RE.fullmatch(path)
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or _BLOCKED_PATH_RE.search(path)
        or any(path.startswith(prefix) for prefix in _BLOCKED_PREFIXES)
    ):
        raise GitHubBrokerPolicyError("github_broker_path_invalid:" + path[:160])
    return path


def _safe_slug(value: Any) -> str:
    slug = str(value or "").strip().lower()
    if not _BRANCH_SLUG_RE.fullmatch(slug):
        raise GitHubBrokerPolicyError("github_broker_branch_slug_invalid")
    return slug


def _sha(value: Any, *, name: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA_RE.fullmatch(text):
        raise GitHubBrokerPolicyError(name + "_invalid")
    return text


def _content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class GitHubBranchPrBroker:
    """Capability-scoped GitHub broker for worker branches and PRs only.

    The credential remains inside this broker. Worker payloads contain no token.
    Merge, deploy, workflow edits, credential paths, deletion and direct default-
    branch writes are intentionally absent from the operation surface.
    """

    def __init__(
        self,
        config: GitHubBrokerConfig,
        *,
        transport: Optional[Transport] = None,
    ):
        self.config = config
        self._transport = transport or self._urllib_transport

    def _urllib_transport(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[Mapping[str, Any]],
    ) -> tuple[int, Any]:
        encoded = None
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=encoded,
            method=method,
            headers=dict(headers),
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read(512_001)
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            raw = exc.read(512_001)
            status = int(exc.code)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GitHubBrokerError("github_broker_network_error:" + type(exc).__name__) from exc
        if len(raw) > 512_000:
            raise GitHubBrokerError("github_broker_response_too_large")
        try:
            value = json.loads(raw.decode("utf-8")) if raw else None
        except Exception as exc:
            raise GitHubBrokerError("github_broker_response_json_invalid") from exc
        return status, value

    def _request(
        self,
        method: str,
        url: str,
        body: Optional[Mapping[str, Any]] = None,
    ) -> tuple[int, Any]:
        headers = {
            "Authorization": "Bearer " + self.config.token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "JAYTEC-Five-Seat-GitHub-Broker/1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        return self._transport(method, url, headers, body)

    @staticmethod
    def _context(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        context = payload.get("_fabric_context")
        if not isinstance(context, Mapping):
            raise GitHubBrokerPolicyError("fabric_context_missing")
        required = {
            "job_id",
            "job_fence_token",
            "seat_id",
            "seat_fence_token",
            "mutation_scope",
            "resource_scope",
        }
        if any(key not in context for key in required):
            raise GitHubBrokerPolicyError("fabric_context_incomplete")
        return context

    def _policy(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[str, str, str, str, list[dict[str, Any]], Mapping[str, Any]]:
        context = self._context(payload)
        repository = str(payload.get("repository") or "").strip()
        if repository not in self.config.allowed_repositories:
            raise GitHubBrokerPolicyError("repository_not_allowlisted:" + repository)

        base_branch = str(payload.get("base_branch") or "").strip()
        if not base_branch or len(base_branch) > 120:
            raise GitHubBrokerPolicyError("base_branch_invalid")
        base_sha = _sha(payload.get("base_sha"), name="base_sha")
        slug = _safe_slug(payload.get("branch_slug"))
        fence = int(context.get("job_fence_token") or 0)
        if fence < 1:
            raise GitHubBrokerPolicyError("job_fence_token_invalid")
        branch = f"watch/worker-{fence}-{slug}"

        files_raw = payload.get("files")
        if not isinstance(files_raw, list) or not (1 <= len(files_raw) <= MAX_FILES):
            raise GitHubBrokerPolicyError("files_count_invalid")
        files: list[dict[str, Any]] = []
        seen: set[str] = set()
        mutation_scope = {
            str(item) for item in list(context.get("mutation_scope") or [])
        }
        required_scope = {f"github:{repository}:pr"}
        for row in files_raw:
            if not isinstance(row, Mapping):
                raise GitHubBrokerPolicyError("file_spec_invalid")
            path = _safe_path(row.get("path"))
            if path in seen:
                raise GitHubBrokerPolicyError("duplicate_file_path:" + path)
            seen.add(path)
            content = str(row.get("content") or "")
            if not content or len(content.encode("utf-8")) > MAX_FILE_BYTES:
                raise GitHubBrokerPolicyError("file_content_invalid:" + path)
            expected_sha = str(row.get("expected_sha") or "").strip().lower()
            if expected_sha and not _SHA_RE.fullmatch(expected_sha):
                raise GitHubBrokerPolicyError("expected_sha_invalid:" + path)
            required_scope.add(f"github:{repository}:path:{path}")
            files.append(
                {
                    "path": path,
                    "content": content,
                    "expected_sha": expected_sha,
                    "content_sha256": _content_digest(content),
                }
            )
        if not required_scope.issubset(mutation_scope):
            missing = sorted(required_scope - mutation_scope)
            raise GitHubBrokerPolicyError(
                "mutation_scope_missing:" + ",".join(missing)
            )

        resource_scope = context.get("resource_scope")
        if not isinstance(resource_scope, Mapping):
            raise GitHubBrokerPolicyError("resource_scope_invalid")
        if str(resource_scope.get("github_repository") or "") != repository:
            raise GitHubBrokerPolicyError("resource_repository_mismatch")
        if str(resource_scope.get("base_sha") or "").lower() != base_sha:
            raise GitHubBrokerPolicyError("resource_base_sha_mismatch")

        pr = payload.get("pull_request")
        if not isinstance(pr, Mapping):
            raise GitHubBrokerPolicyError("pull_request_missing")
        title = str(pr.get("title") or "").strip()[:240]
        body = str(pr.get("body") or "").strip()[:4000]
        if not title:
            raise GitHubBrokerPolicyError("pull_request_title_missing")

        return repository, base_branch, base_sha, branch, files, {
            "title": title,
            "body": body,
        }

    def _ref_head(self, root: str, branch: str) -> str:
        status, value = self._request(
            "GET",
            f"{root}/git/ref/heads/{urllib.parse.quote(branch, safe='')}",
        )
        if status != 200 or not isinstance(value, Mapping):
            raise GitHubBrokerError("git_ref_read_failed:" + branch)
        obj = value.get("object") if isinstance(value.get("object"), Mapping) else {}
        return _sha(obj.get("sha"), name="git_ref_head")

    def _verify_diff_confined(
        self,
        root: str,
        *,
        base_sha: str,
        branch: str,
        allowed_paths: set[str],
    ) -> Mapping[str, Any]:
        status, compare = self._request(
            "GET",
            f"{root}/compare/{base_sha}...{urllib.parse.quote(branch, safe='')}",
        )
        if status != 200 or not isinstance(compare, Mapping):
            raise GitHubBrokerError("github_compare_failed")
        merge_base = (
            compare.get("merge_base_commit")
            if isinstance(compare.get("merge_base_commit"), Mapping)
            else {}
        )
        if str(merge_base.get("sha") or "") != base_sha:
            raise GitHubBrokerPolicyError("worker_branch_not_descended_from_attested_base")
        changed: set[str] = set()
        for row in list(compare.get("files") or []):
            if not isinstance(row, Mapping):
                raise GitHubBrokerError("github_compare_file_invalid")
            path = _safe_path(row.get("filename"))
            status_name = str(row.get("status") or "")
            if status_name not in {"added", "modified"}:
                raise GitHubBrokerPolicyError(
                    "worker_branch_change_type_forbidden:" + status_name
                )
            changed.add(path)
        if not changed.issubset(allowed_paths):
            raise GitHubBrokerPolicyError(
                "worker_branch_scope_escape:" + ",".join(sorted(changed - allowed_paths))
            )
        return {
            "merge_base_sha": base_sha,
            "changed_paths": sorted(changed),
        }

    def execute(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            (
                repository,
                base_branch,
                base_sha,
                branch,
                files,
                pr,
            ) = self._policy(payload)
        except GitHubBrokerPolicyError as exc:
            raise PermanentAdapterError(str(exc)) from exc

        root = f"https://api.github.com/repos/{repository}"
        owner = repository.split("/", 1)[0]
        allowed_paths = {row["path"] for row in files}
        operations: list[dict[str, Any]] = []
        side_effect_present = False

        try:
            # Base freshness is checked before any mutation. A stale base turns
            # into a new planning/rebase job rather than an implicit mutation.
            observed_base_sha = self._ref_head(root, base_branch)
            if observed_base_sha != base_sha:
                raise GitHubBrokerPolicyError("base_branch_head_moved")

            encoded_branch = urllib.parse.quote(branch, safe="")
            status, existing = self._request(
                "GET", f"{root}/git/ref/heads/{encoded_branch}"
            )
            if status == 404:
                status, created = self._request(
                    "POST",
                    f"{root}/git/refs",
                    {"ref": "refs/heads/" + branch, "sha": base_sha},
                )
                if status not in {200, 201} or not isinstance(created, Mapping):
                    raise GitHubBrokerError("branch_create_failed:" + str(status))
                side_effect_present = True
                branch_head = str(
                    ((created.get("object") or {}) if isinstance(created.get("object"), Mapping) else {}).get("sha")
                    or base_sha
                )
                operations.append(
                    {
                        "operation": "create_branch",
                        "branch": branch,
                        "base_sha": base_sha,
                        "head_sha": branch_head,
                        "idempotent_replay": False,
                    }
                )
            elif status == 200 and isinstance(existing, Mapping):
                side_effect_present = True
                obj = existing.get("object") if isinstance(existing.get("object"), Mapping) else {}
                branch_head = str(obj.get("sha") or "")
                self._verify_diff_confined(
                    root,
                    base_sha=base_sha,
                    branch=branch,
                    allowed_paths=allowed_paths,
                )
                operations.append(
                    {
                        "operation": "create_branch",
                        "branch": branch,
                        "base_sha": base_sha,
                        "head_sha": branch_head,
                        "idempotent_replay": True,
                    }
                )
            else:
                raise GitHubBrokerError("branch_lookup_failed:" + str(status))

            for spec in files:
                path = spec["path"]
                encoded_path = urllib.parse.quote(path, safe="/")
                status, current = self._request(
                    "GET", f"{root}/contents/{encoded_path}?ref={urllib.parse.quote(branch, safe='')}"
                )
                current_sha = ""
                current_text = None
                if status == 200 and isinstance(current, Mapping):
                    current_sha = str(current.get("sha") or "")
                    encoded = str(current.get("content") or "").replace("\n", "")
                    try:
                        current_text = base64.b64decode(encoded).decode("utf-8")
                    except Exception as exc:
                        raise GitHubBrokerError("existing_file_not_utf8:" + path) from exc
                    if current_text == spec["content"]:
                        operations.append(
                            {
                                "operation": "write_file",
                                "path": path,
                                "branch": branch,
                                "sha": current_sha,
                                "content_sha256": spec["content_sha256"],
                                "idempotent_replay": True,
                            }
                        )
                        continue
                    if not spec["expected_sha"]:
                        raise GitHubBrokerPolicyError("expected_sha_required:" + path)
                    if current_sha != spec["expected_sha"]:
                        raise GitHubBrokerPolicyError("expected_sha_mismatch:" + path)
                elif status == 404:
                    if spec["expected_sha"]:
                        raise GitHubBrokerPolicyError("expected_sha_not_found:" + path)
                else:
                    raise GitHubBrokerError("file_lookup_failed:" + path + ":" + str(status))

                request = {
                    "message": f"JAYTEC worker {str(self._context(payload).get('job_id'))[:80]}: {path}"[:200],
                    "content": base64.b64encode(spec["content"].encode("utf-8")).decode("ascii"),
                    "branch": branch,
                }
                if current_sha:
                    request["sha"] = current_sha
                status, written = self._request(
                    "PUT", f"{root}/contents/{encoded_path}", request
                )
                if status not in {200, 201} or not isinstance(written, Mapping):
                    raise GitHubBrokerError("file_write_failed:" + path + ":" + str(status))
                side_effect_present = True
                content_row = written.get("content") if isinstance(written.get("content"), Mapping) else {}
                commit_row = written.get("commit") if isinstance(written.get("commit"), Mapping) else {}
                branch_head = str(commit_row.get("sha") or branch_head)
                operations.append(
                    {
                        "operation": "write_file",
                        "path": path,
                        "branch": branch,
                        "sha": str(content_row.get("sha") or ""),
                        "commit_sha": branch_head,
                        "content_sha256": spec["content_sha256"],
                        "idempotent_replay": False,
                    }
                )

            query = (
                f"{root}/pulls?state=open&head="
                + urllib.parse.quote(owner + ":" + branch, safe="")
                + "&base="
                + urllib.parse.quote(base_branch, safe="")
                + "&per_page=10"
            )
            status, existing_prs = self._request("GET", query)
            if status != 200 or not isinstance(existing_prs, list):
                raise GitHubBrokerError("pull_request_lookup_failed:" + str(status))
            if existing_prs:
                row = existing_prs[0] if isinstance(existing_prs[0], Mapping) else {}
                pr_number = int(row.get("number") or 0)
                pr_url = str(row.get("html_url") or "")
                operations.append(
                    {
                        "operation": "create_pr",
                        "number": pr_number,
                        "branch": branch,
                        "base": base_branch,
                        "idempotent_replay": True,
                    }
                )
            else:
                status, created_pr = self._request(
                    "POST",
                    f"{root}/pulls",
                    {
                        "title": pr["title"],
                        "head": branch,
                        "base": base_branch,
                        "body": pr["body"],
                    },
                )
                if status not in {200, 201} or not isinstance(created_pr, Mapping):
                    raise GitHubBrokerError("pull_request_create_failed:" + str(status))
                side_effect_present = True
                pr_number = int(created_pr.get("number") or 0)
                pr_url = str(created_pr.get("html_url") or "")
                operations.append(
                    {
                        "operation": "create_pr",
                        "number": pr_number,
                        "branch": branch,
                        "base": base_branch,
                        "idempotent_replay": False,
                    }
                )

            final_diff = self._verify_diff_confined(
                root,
                base_sha=base_sha,
                branch=branch,
                allowed_paths=allowed_paths,
            )

            result = {
                "operations": operations,
                "artifacts": [
                    {
                        "type": "github_pull_request",
                        "repository": repository,
                        "number": pr_number,
                        "url": pr_url,
                        "branch": branch,
                        "head_sha": branch_head,
                        "base_branch": base_branch,
                        "base_sha": base_sha,
                    }
                ],
                "tests": [],
                "evidence": [
                    {
                        "type": "github_broker_execution",
                        "repository": repository,
                        "branch": branch,
                        "head_sha": branch_head,
                        "pr_number": pr_number,
                        "file_digests": {
                            row["path"]: row["content_sha256"] for row in files
                        },
                        "diff": final_diff,
                    }
                ],
                "provider_identity": "JAYTEC_GITHUB_BROKER",
                "unresolved_items": [],
                "partial_side_effect_status": "VERIFIED_COMPLETE",
                "proposed_next_action": "WATCH_VERIFY_GITHUB",
                "worker_completion_classification": "CANDIDATE_COMPLETE",
                "whole_packet_status": "SUCCESS",
                "watch_verification": {
                    "repository": repository,
                    "branch": branch,
                    "head_sha": branch_head,
                    "base_branch": base_branch,
                    "base_sha": base_sha,
                    "pr_number": pr_number,
                    "file_digests": {
                        row["path"]: row["content_sha256"] for row in files
                    },
                },
            }
            return result
        except GitHubBrokerPolicyError as exc:
            if side_effect_present:
                raise UncertainSideEffectError(
                    "github_broker_policy_failed_with_external_state:"
                    + str(exc)[:500]
                ) from exc
            raise PermanentAdapterError(str(exc)) from exc
        except Exception as exc:
            if side_effect_present:
                raise UncertainSideEffectError(
                    "github_broker_uncertain_after_side_effect:"
                    + type(exc).__name__
                    + ":"
                    + str(exc)[:500]
                ) from exc
            raise PermanentAdapterError(
                "github_broker_failed_before_side_effect:"
                + type(exc).__name__
                + ":"
                + str(exc)[:500]
            ) from exc

    def verify_result(self, result: Mapping[str, Any]) -> Mapping[str, Any]:
        expected = result.get("watch_verification")
        if not isinstance(expected, Mapping):
            raise GitHubBrokerPolicyError("watch_verification_missing")
        repository = str(expected.get("repository") or "")
        if repository not in self.config.allowed_repositories:
            raise GitHubBrokerPolicyError("watch_repository_not_allowlisted")
        branch = str(expected.get("branch") or "")
        if not branch.startswith("watch/worker-"):
            raise GitHubBrokerPolicyError("watch_branch_invalid")
        expected_head = _sha(expected.get("head_sha"), name="watch_head_sha")
        base_branch = str(expected.get("base_branch") or "")
        pr_number = int(expected.get("pr_number") or 0)
        file_digests = expected.get("file_digests")
        if not base_branch or pr_number < 1 or not isinstance(file_digests, Mapping):
            raise GitHubBrokerPolicyError("watch_verification_contract_invalid")

        root = f"https://api.github.com/repos/{repository}"
        allowed_paths = {_safe_path(path) for path in file_digests}
        expected_base_sha = _sha(expected.get("base_sha"), name="watch_base_sha")
        # The target branch may legitimately advance after worker completion.
        # WATCH verifies ancestry against the exact attested base SHA instead of
        # requiring the moving base ref to remain frozen.
        diff = self._verify_diff_confined(
            root,
            base_sha=expected_base_sha,
            branch=branch,
            allowed_paths=allowed_paths,
        )

        status, ref = self._request(
            "GET", f"{root}/git/ref/heads/{urllib.parse.quote(branch, safe='')}"
        )
        obj = ref.get("object") if status == 200 and isinstance(ref, Mapping) and isinstance(ref.get("object"), Mapping) else {}
        observed_head = str(obj.get("sha") or "")
        if observed_head != expected_head:
            raise GitHubBrokerError("watch_branch_head_mismatch")

        status, pr = self._request("GET", f"{root}/pulls/{pr_number}")
        if status != 200 or not isinstance(pr, Mapping):
            raise GitHubBrokerError("watch_pull_request_read_failed")
        head = pr.get("head") if isinstance(pr.get("head"), Mapping) else {}
        base = pr.get("base") if isinstance(pr.get("base"), Mapping) else {}
        if (
            str(head.get("ref") or "") != branch
            or str(head.get("sha") or "") != expected_head
            or str(base.get("ref") or "") != base_branch
            or bool(pr.get("merged"))
            or str(pr.get("state") or "").lower() != "open"
        ):
            raise GitHubBrokerError("watch_pull_request_state_mismatch")

        verified_files: dict[str, str] = {}
        for raw_path, expected_digest in file_digests.items():
            path = _safe_path(raw_path)
            status, row = self._request(
                "GET",
                f"{root}/contents/{urllib.parse.quote(path, safe='/')}?ref={urllib.parse.quote(branch, safe='')}",
            )
            if status != 200 or not isinstance(row, Mapping):
                raise GitHubBrokerError("watch_file_read_failed:" + path)
            encoded = str(row.get("content") or "").replace("\n", "")
            try:
                text = base64.b64decode(encoded).decode("utf-8")
            except Exception as exc:
                raise GitHubBrokerError("watch_file_not_utf8:" + path) from exc
            digest = _content_digest(text)
            if digest != str(expected_digest):
                raise GitHubBrokerError("watch_file_digest_mismatch:" + path)
            verified_files[path] = digest

        return {
            "github_readback_verified": True,
            "repository": repository,
            "branch": branch,
            "head_sha": expected_head,
            "pr_number": pr_number,
            "base_branch": base_branch,
            "base_sha": expected_base_sha,
            "diff": diff,
            "files": verified_files,
        }


def submit_github_branch_pr_job(
    queue: PostgresFabricQueue,
    *,
    task_id: str,
    objective: str,
    idempotency_key: str,
    source_shared_state_version: int,
    repository: str,
    base_branch: str,
    base_sha: str,
    branch_slug: str,
    files: list[Mapping[str, Any]],
    pull_request: Mapping[str, Any],
    priority: int = 100,
    subtask_id: Optional[str] = None,
) -> Mapping[str, Any]:
    repository = str(repository or "").strip()
    base_branch = str(base_branch or "").strip()
    base_sha = _sha(base_sha, name="base_sha")
    branch_slug = _safe_slug(branch_slug)
    normalized_files: list[dict[str, Any]] = []
    mutations = {f"github:{repository}:pr"}
    for row in files:
        path = _safe_path(row.get("path"))
        content = str(row.get("content") or "")
        expected_sha = str(row.get("expected_sha") or "").strip().lower()
        if expected_sha and not _SHA_RE.fullmatch(expected_sha):
            raise ValueError("expected_sha_invalid:" + path)
        if not content or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("file_content_invalid:" + path)
        normalized_files.append(
            {"path": path, "content": content, "expected_sha": expected_sha}
        )
        mutations.add(f"github:{repository}:path:{path}")
    if not normalized_files or len(normalized_files) > MAX_FILES:
        raise ValueError("files_count_invalid")

    return queue.submit(
        task_id=task_id,
        subtask_id=subtask_id,
        objective=objective,
        worker_kind=GITHUB_WORKER_KIND,
        idempotency_key="five-seat-github:" + str(idempotency_key),
        source_shared_state_version=int(source_shared_state_version),
        authority_class="EXTERNAL_SIDE_EFFECT",
        concurrency_class="B",
        priority=int(priority),
        max_attempts=1,
        max_reworks=2,
        required_capabilities={GITHUB_WORK_CAPABILITY},
        read_scope={f"github:{repository}:base:{base_sha}"},
        mutation_scope=mutations,
        resource_scope={
            "github_repository": repository,
            "base_branch": base_branch,
            "base_sha": base_sha,
        },
        dependencies=set(),
        collision_key=None,
        cost_policy={
            "mode": "ZERO_SPEND",
            "allow_paid": False,
            "max_cost_usd": 0,
            "provider_mode": "NO_PROVIDER",
        },
        evidence_standard={
            "watch_review_required": True,
            "github_readback_required": True,
            "branch_only": True,
            "merge_forbidden": True,
        },
        stop_conditions={
            "merge": "FAIL_CLOSED",
            "deploy": "FAIL_CLOSED",
            "credential_access": "FAIL_CLOSED",
            "scope_expansion": "FAIL_CLOSED",
        },
        result_destination={
            "type": "WATCH_ATTESTED_GITHUB_PR",
            "task_id": task_id,
        },
        payload={
            "repository": repository,
            "base_branch": base_branch,
            "base_sha": base_sha,
            "branch_slug": branch_slug,
            "files": normalized_files,
            "pull_request": {
                "title": str(pull_request.get("title") or "")[:240],
                "body": str(pull_request.get("body") or "")[:4000],
            },
        },
    )
