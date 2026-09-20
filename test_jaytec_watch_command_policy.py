import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class JaytecWatchCommandPolicyTests(unittest.TestCase):
    def test_watch_is_registered_as_durable_autorecovery_supervisor(self):
        text = (ROOT / "JAYTEC_COMMAND_POLICY.md").read_text(encoding="utf-8")
        for phrase in (
            "## JAYTEC:WATCH + AUTORECOVERY",
            "The assignment survives the worker",
            "JAYTEC:WATCH STATUS",
            "JAYTEC:WATCH STOP",
            "approximately every five minutes",
            "recovery lease",
            "fencing token",
            "Resume — do not recreate completed work",
            "JAYTEC_CALLABLE",
            "RECOVERY_EXHAUSTED",
            "autorecovery_supervisor.py",
        ):
            self.assertIn(phrase, text)

    def test_owner_and_operator_pauses_are_never_autoresumed(self):
        text = (ROOT / "JAYTEC_COMMAND_POLICY.md").read_text(encoding="utf-8")
        self.assertIn("PAUSED_BY_OWNER", text)
        self.assertIn("PAUSED_BY_OPERATOR", text)
        self.assertIn(
            'must\nnever reinterpret "pause safely" as a stale worker and restart it',
            text,
        )

    def test_only_documented_recoverable_stop_reasons_may_autoresume(self):
        text = (ROOT / "JAYTEC_COMMAND_POLICY.md").read_text(encoding="utf-8")
        for reason in (
            "STALLED_RECOVERABLE",
            "WORKER_LOST",
            "TIMEOUT",
            "TRANSIENT_PROVIDER_FAILURE",
        ):
            self.assertIn(reason, text)
        self.assertIn("Automatic recovery is allowed only for:", text)

    def test_ui_chat_is_manual_resume_only(self):
        text = (ROOT / "JAYTEC_COMMAND_POLICY.md").read_text(encoding="utf-8")
        self.assertIn("Normal ChatGPT app conversations are not assumed externally callable", text)
        self.assertIn("one manual resume action is required", text)
        self.assertIn("do not try to click, poke, message, or impersonate the chat externally", text)

    def test_watch_stop_preserves_assignment_state(self):
        text = (ROOT / "JAYTEC_COMMAND_POLICY.md").read_text(encoding="utf-8")
        self.assertIn(
            "It MUST NOT infer cancellation, delete canonical task state, destroy\nthe checkpoint, or mark the objective complete",
            text,
        )

    def test_github_observer_remains_non_authoritative_and_read_only(self):
        text = (ROOT / "JAYTEC_COMMAND_POLICY.md").read_text(encoding="utf-8")
        self.assertIn("remains a\nread-only observability surface", text)
        self.assertIn("is not the canonical recovery authority", text)
        self.assertIn("GitHub inactivity alone can never prove a worker died", text)


if __name__ == "__main__":
    unittest.main()
