def test_dockerfile_copies_full_runtime():
    dockerfile = open("Dockerfile", "r", encoding="utf-8").read()

    assert "COPY . ./" in dockerfile
    assert "CMD [\"python\", \"server.py\"]" in dockerfile


def test_dockerignore_blocks_sensitive_and_dev_files():
    dockerignore = open(".dockerignore", "r", encoding="utf-8").read()

    # Must not ship git history or env files into container image.
    for required in [".git", ".env", ".env.*", ".github", "__pycache__"]:
        assert required in dockerignore
