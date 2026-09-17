import unittest


class TestDockerPackaging(unittest.TestCase):
    def test_dockerfile_copies_full_runtime(self):
        with open("Dockerfile", "r", encoding="utf-8") as handle:
            dockerfile = handle.read()
        self.assertIn("COPY . ./", dockerfile)
        self.assertIn('CMD ["python", "compat_server.py"]', dockerfile)
        self.assertIn("reliable_server", dockerfile)

    def test_dockerignore_blocks_sensitive_and_dev_files(self):
        with open(".dockerignore", "r", encoding="utf-8") as handle:
            dockerignore = handle.read()
        for required in [".git", ".env", ".env.*", ".github", "__pycache__"]:
            self.assertIn(required, dockerignore)


if __name__ == "__main__":
    unittest.main()
