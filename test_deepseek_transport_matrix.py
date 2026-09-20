from pathlib import Path


ROOT = Path(__file__).parent
SOURCE = ROOT / "staging_server.py"


def _probe_source() -> str:
    text = SOURCE.read_text(encoding="utf-8")
    start = text.index("def _run_deepseek_transport_matrix_probe()")
    end = text.index('\n\nif __name__ == "__main__":', start)
    return text[start:end]


def test_transport_matrix_keeps_exact_reviewer_model():
    body = _probe_source()
    assert "model=DEEPSEEK_REVIEWER_MODEL" in body
    assert '"model": DEEPSEEK_REVIEWER_MODEL' in body
    assert "allow_fallbacks" in body


def test_transport_matrix_is_bounded_to_four_safe_cases():
    body = _probe_source()
    assert body.count('"name": "json_') == 4
    assert "max_tokens=128" in body
    assert "timeout=30" in body


def test_transport_matrix_never_logs_prompt_response_or_key_material():
    body = _probe_source()
    assert "OPENROUTER_API_KEY" not in body
    assert '"content": content' not in body
    assert '"response":' not in body
    assert '"prompt":' not in body
    assert '"api_key":' not in body


def test_transport_matrix_only_reports_error_class_and_model_identity():
    body = _probe_source()
    assert '"error_class"' in body
    assert '"returned_model"' in body
    assert "type(exc).__name__" in body
