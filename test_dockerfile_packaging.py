import unittest


class TestDockerPackaging(unittest.TestCase):
    def test_dockerfile_copies_full_runtime(self):
        dockerfile = open("Dockerfile", "r", encoding="utf-8").read()
        self.assertIn("COPY . ./", dockerfile)
        self.assertIn('CMD ["python", "compat_server.py"]', dockerfile)

    def test_dockerignore_blocks_sensitive_and_dev_files(self):
        dockerignore = open(".dockerignore", "r", encoding="utf-8").read()
        for required in [".git", ".env", ".env.*", ".github", "__pycache__"]:
            self.assertIn(required, dockerignore)


if __name__ == "__main__":
    unittest.main()
