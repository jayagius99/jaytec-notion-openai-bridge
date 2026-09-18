import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "staging_independent_g1_review.py"


def _load():
    spec = importlib.util.spec_from_file_location("g1_independent_review_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_independent_review_packet_is_bounded_and_secret_safe():
    module = _load()
    assert module.EXPECTED_MODEL == "nvidia/nemotron-3-ultra-550b-a55b:free"
    ok = module._validate_packet({"proofs": {"P01": "PASS"}, "evidence": ["hash-only"]})
    assert ok["proofs"]["P01"] == "PASS"
    with pytest.raises(ValueError, match="FORBIDDEN_KEY"):
        module._validate_packet({"api_key": "never"})
    with pytest.raises(ValueError, match="TOO_LARGE"):
        module._validate_packet({"evidence": "x" * (module.MAX_PACKET_CHARS + 1)})


def test_independent_review_is_disabled_by_default_and_has_no_tools():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'JAYTEC_G1_INDEPENDENT_REVIEW_ENABLED", "0"' in text
    assert "allow_fallbacks" in text and "False" in text
    assert "max_retries=0" in text
    assert "tools=" not in text
    assert "OPENAI_API_KEY" not in text
