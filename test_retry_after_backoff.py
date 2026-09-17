import unittest

from durable_tasks_runtime import retry_delay_for_result


class TestRetryAfterBackoff(unittest.TestCase):
    def test_provider_retry_after_extends_default_backoff(self):
        result = {
            "overall_status": "PARTIAL_SUCCESS",
            "codex_result": {"status": "SUCCESS"},
            "gemini_result": {
                "status": "RATE_LIMITED",
                "unresolved_items": ["retryable_rate_limit:retry_after=45"],
            },
        }
        self.assertEqual(retry_delay_for_result(result, 1), 45)

    def test_small_retry_after_does_not_shorten_safe_backoff(self):
        result = {
            "overall_status": "RATE_LIMITED",
            "unresolved_items": ["retryable_rate_limit:retry_after=1"],
        }
        self.assertEqual(retry_delay_for_result(result, 2), 10)

    def test_retry_after_is_bounded(self):
        result = {
            "overall_status": "RATE_LIMITED",
            "unresolved_items": ["retryable_rate_limit:retry_after=999999"],
        }
        self.assertEqual(retry_delay_for_result(result, 1), 3600)


if __name__ == "__main__":
    unittest.main()
