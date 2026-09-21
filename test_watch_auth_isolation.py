from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER = (ROOT / "staging_server.py").read_text(encoding="utf-8")


def test_github_oidc_is_not_installed_as_global_mcp_auth():
    assert "MultiAuth" not in SERVER
    assert "auth=static_auth" in SERVER
    assert 'middleware=[AuthMiddleware(auth=require_scopes("jaytec:mcp"))]' in SERVER


def test_watch_route_manually_verifies_bearer_oidc():
    assert '@mcp.custom_route("/jaytec/watch-cycle", methods=["POST"])' in SERVER
    assert 'request.headers.get("authorization")' in SERVER
    assert "watch_oidc_auth.verify_token(raw_token)" in SERVER
    assert '"jaytec:watch-cycle"' in SERVER


def test_watch_route_does_not_reuse_mcp_static_token_path():
    route = SERVER.split('@mcp.custom_route("/jaytec/watch-cycle", methods=["POST"])', 1)[1]
    route = route.split("def _protocol_portal", 1)[0]
    assert "MCP_AUTH_TOKEN" not in route
    assert "static_auth" not in route


def test_watch_status_route_is_oidc_only_and_read_only():
    assert '@mcp.custom_route("/jaytec/watch-status", methods=["POST"])' in SERVER
    route = SERVER.split('@mcp.custom_route("/jaytec/watch-status", methods=["POST"])', 1)[1]
    route = route.split("def _protocol_portal", 1)[0]
    assert 'watch_oidc_auth.verify_token(raw_token)' in route
    assert '"jaytec:watch-cycle"' in route
    assert 'read_autorecovery_assignment_status(' in route
    assert '"read_only"] = True' in route
    assert '"lease_acquired"] = False' in route
    assert '"worker_invoked"] = False' in route
    assert "execute_watch_cycle(" not in route
    assert "acquire_recovery_lease" not in route
    assert "MCP_AUTH_TOKEN" not in route
    assert "static_auth" not in route
    assert '"specialist_fabric"' in route
    assert '"default_watch_trio": ["sol", "deepseek", "nemo"]' in route
    assert '"manus_direct_provider_access": False' in route
    assert "chat.completions.create" not in route
    assert "responses.create" not in route
    assert '"runtime_deployment"' in route
    assert 'os.environ.get("RENDER_GIT_COMMIT"' in route
    assert 'os.environ.get("RENDER_GIT_BRANCH"' in route
    assert 'os.environ.get("RENDER_GIT_REPO_SLUG"' in route
    for forbidden in (
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "MANUS_API_KEY",
        "DATABASE_URL",
    ):
        assert forbidden not in route


def test_watch_status_only_peeks_existing_invocation_record():
    route = SERVER.split('@mcp.custom_route("/jaytec/watch-status", methods=["POST"])', 1)[1]
    route = route.split("def _protocol_portal", 1)[0]
    assert "REGISTRY.peek_result(recovery_key)" in route
    assert '"last_invocation_record"' in route
    assert "_manus_runtime()" not in route
    assert "execute_watch_cycle(" not in route


def test_watch_status_recovery_budget_diagnostic_is_read_only():
    route = SERVER.split('@mcp.custom_route("/jaytec/watch-status", methods=["POST"])', 1)[1]
    route = route.split("def _protocol_portal", 1)[0]
    assert "recovery_preflight_budget(" in route
    assert "PostgresAssignmentStore(DATABASE_URL).get(task_id)" in route
    assert '"provider_invoked"] = False' in route
    assert '"lease_acquired"] = False' in route
    assert "acquire_recovery_lease" not in route
    assert "start_task(" not in route
    assert "start_task_idempotent(" not in route


def test_watch_status_terminal_inspection_is_read_only():
    route = SERVER.split('@mcp.custom_route("/jaytec/watch-status", methods=["POST"])', 1)[1]
    route = route.split("def _protocol_portal", 1)[0]
    assert "task_status_readonly(worker_id)" in route
    assert '"current_worker_result"' in route
    assert "stop_task(" not in route
    assert "execute_watch_cycle(" not in route
    assert "acquire_recovery_lease" not in route


def test_proving_grounds_watch_route_is_oidc_only_and_no_arbitrary_execution_path():
    assert '@mcp.custom_route("/jaytec/proving-grounds", methods=["POST"])' in SERVER
    route = SERVER.split('@mcp.custom_route("/jaytec/proving-grounds", methods=["POST"])', 1)[1]
    route = route.split('@mcp.custom_route("/jaytec/watch-cycle"', 1)[0]
    assert 'watch_oidc_auth.verify_token(raw_token)' in route
    assert '"jaytec:proving-grounds"' in route
    assert 'proving_grounds_run_registered_suite(' in route
    assert 'FORGE-GENESIS-ACTIVATION-001' in route
    assert "subprocess" not in route
    assert "MCP_AUTH_TOKEN" not in route
    assert "static_auth" not in route


def test_watch_controller_advice_route_is_oidc_only_and_sol_advisory():
    assert '@mcp.custom_route("/jaytec/watch-controller-advice", methods=["POST"])' in SERVER
    route = SERVER.split('@mcp.custom_route("/jaytec/watch-controller-advice", methods=["POST"])', 1)[1]
    route = route.split('@mcp.custom_route("/jaytec/watch-status"', 1)[0]
    assert 'watch_oidc_auth.verify_token(raw_token)' in route
    assert '"jaytec:watch-controller-advice"' in route
    assert 'watch_controller_advise(' in route
    assert 'engineering_dispatch=ENGINEERING_DISPATCH' in route
    assert 'REGISTRY.lookup(cache_key, normalized["request_sha256"])' in route
    assert 'REGISTRY.store(' in route
    assert "MCP_AUTH_TOKEN" not in route
    assert "static_auth" not in route
    assert "execute_watch_cycle(" not in route
