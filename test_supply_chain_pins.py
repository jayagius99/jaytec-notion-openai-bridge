import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent

EXPECTED_DOCKER_DIGEST = "sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
EXPECTED_CHECKOUT_SHA = "fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09"
EXPECTED_SETUP_PYTHON_SHA = "ece7cb06caefa5fff74198d8649806c4678c61a1"


class SupplyChainPinTests(unittest.TestCase):
    def test_top_level_requirements_are_exactly_pinned(self):
        lines = [
            line.strip()
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            lines,
            [
                "fastmcp==3.4.7",
                "openai==3.16.2",
                "psycopg2-binary==2.9.13",
            ],
        )

    def test_lock_file_has_only_exact_versions(self):
        lines = [
            line.strip()
            for line in (ROOT / "requirements.lock.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertGreater(len(lines), 20)
        for line in lines:
            with self.subTest(line=line):
                self.assertRegex(line, r"^[A-Za-z0-9_.-]+==[A-Za-z0-9_.+-]+$")
                self.assertEqual(line.count("=="), 1)
                name, version = line.split("==", 1)
                self.assertTrue(name)
                self.assertTrue(version)
                self.assertFalse(any(op in name or op in version for op in ("<", ">", "!", "~", "=")))

    def test_lock_contains_exact_top_level_dependencies(self):
        lock = set(
            line.strip()
            for line in (ROOT / "requirements.lock.txt").read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        for item in (
            "fastmcp==3.4.7",
            "openai==3.16.2",
            "psycopg2-binary==2.9.13",
        ):
            self.assertIn(item, lock)

    def test_docker_base_and_install_are_pinned(self):
        source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(f"FROM python:3.12-slim@{EXPECTED_DOCKER_DIGEST}", source)
        self.assertIn("pip install --no-cache-dir -r requirements.lock.txt", source)
        self.assertNotIn("FROM python:3.12-slim\n", source)

    def test_github_actions_are_immutable(self):
        source = (
            ROOT / ".github" / "workflows" / "orchestration-staging.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(f"actions/checkout@{EXPECTED_CHECKOUT_SHA}", source)
        self.assertIn(f"actions/setup-python@{EXPECTED_SETUP_PYTHON_SHA}", source)
        self.assertNotRegex(source, r"actions/checkout@v\\d+")
        self.assertNotRegex(source, r"actions/setup-python@v\\d+")

    def test_security_files_are_ci_trigger_paths(self):
        source = (
            ROOT / ".github" / "workflows" / "orchestration-staging.yml"
        ).read_text(encoding="utf-8")
        for path in (
            "requirements.lock.txt",
            "provider_endpoints.py",
            "http_security.py",
            "auth_security.py",
            "test_provider_endpoints.py",
            "test_http_security.py",
            "test_auth_security.py",
            "test_supply_chain_pins.py",
        ):
            with self.subTest(path=path):
                self.assertIn(f"- '{path}'", source)

    def test_ci_installs_locked_graph(self):
        source = (
            ROOT / ".github" / "workflows" / "orchestration-staging.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("python -m pip install -r requirements.lock.txt", source)
        self.assertNotIn("pip install -r requirements.txt", source)
        self.assertNotIn("pip install --upgrade pip", source)


if __name__ == "__main__":
    unittest.main()
