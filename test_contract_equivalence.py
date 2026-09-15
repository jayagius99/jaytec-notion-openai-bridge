from specialist_adapters import CODEX_CONTRACT, GEMINI_RESEARCH_MODE_V1_1


def test_contract_strings_are_nonempty_and_stable():
    assert "Return ONLY one JSON object" in CODEX_CONTRACT
    assert "JAYTEC_GEMINI_RESEARCH_MODE v1.1.0" in GEMINI_RESEARCH_MODE_V1_1
