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
