import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class JaytecWatchCommandPolicyTests(unittest.TestCase):
    def test_watch_commands_and_safety_boundaries_are_registered(self):
        text = (ROOT / "JAYTEC_COMMAND_POLICY.md").read_text(encoding="utf-8")
        for phrase in (
            "## JAYTEC:WATCH",
            "JAYTEC:WATCH STATUS",
            "JAYTEC:WATCH STOP",
            "nominal cadence: every five minutes",
            "observation-only",
            "UNKNOWN",
            "WATCH_TARGET_UNSUPPORTED",
        ):
            self.assertIn(phrase, text)

    def test_stop_is_explicitly_non_destructive_to_watched_assignment(self):
        text = (ROOT / "JAYTEC_COMMAND_POLICY.md").read_text(encoding="utf-8")
        self.assertIn(
            "MUST NOT\nstop, pause, restart, resume, rerun, cancel, merge, edit, deploy",
            text,
        )


if __name__ == "__main__":
    unittest.main()
