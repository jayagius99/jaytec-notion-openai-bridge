def test_dockerfile_copies_full_runtime():
    # Regression guard for C6:
    # Dockerfile must include the orchestration/idempotency/circuit-breaker runtime
    # modules required by the production entrypoint (server.py).
    dockerfile = open("Dockerfile", "r", encoding="utf-8").read()

    assert "COPY . ./" in dockerfile
    assert "CMD [\"python\", \"server.py\"]" in dockerfile
