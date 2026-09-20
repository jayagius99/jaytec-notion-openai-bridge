from pathlib import Path


ROOT = Path(__file__).parent
SOURCE = ROOT / "staging_server.py"


def _probe_source() -> str:
    text = SOURCE.read_text(encoding="utf-8")
    start = text.index("def _run_deepseek_route_visibility_probe()")
    end = text.index('\n\nif __name__ == "__main__":', start)
    return text[start:end]


def test_visibility_probe_is_explicitly_gated():
    body = _probe_source()
    assert "JAYTEC_DEEPSEEK_ROUTE_VISIBILITY_ENABLED" in body
    assert '== "1"' in body


def test_visibility_probe_logs_only_safe_metadata():
    body = _probe_source()
    for forbidden in [
        "OPENROUTER_API_KEY",
        '"prompt"',
        '"response"',
        '"api_key"',
        '"account"',
        '"headers"',
    ]:
        assert forbidden not in body
    for required in [
        '"base_host"',
        '"model_visible"',
        '"models_count"',
        '"visible_deepseek_models"',
        '"error_class"',
    ]:
        assert required in body


def test_visibility_probe_keeps_exact_model_identity():
    body = _probe_source()
    assert "DEEPSEEK_REVIEWER_MODEL" in body
    assert "OPENROUTER_CLIENT.models.list()" in body


def test_visibility_probe_is_started_as_daemon_only():
    text = SOURCE.read_text(encoding="utf-8")
    assert 'target=_run_deepseek_route_visibility_probe' in text
    assert 'name="jaytec-deepseek-route-visibility"' in text


def test_visibility_probe_only_exposes_deepseek_vendor_ids():
    body = _probe_source()
    assert 'model_id.startswith("deepseek/")' in body
    assert "sorted(" in body
    assert ")[:50]" in body
