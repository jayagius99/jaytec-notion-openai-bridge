import unittest

from durable_tasks import (
    contains_secret_material,
    retry_delay_seconds,
    should_cache_orchestration_result,
)


class TestDurableTaskHelpers(unittest.TestCase):
    def test_secret_keys_are_rejected_before_persistence(self):
        self.assertTrue(contains_secret_material({"api_key": "abc"}))
        self.assertTrue(contains_secret_material({"nested": {"token": "abc"}}))

    def test_secret_values_are_rejected_before_persistence(self):
        self.assertTrue(
            contains_secret_material({"text": "Authorization: Bearer abcdefghijklmnop"})
        )

    def test_normal_packet_content_is_not_secret(self):
        self.assertFalse(
            contains_secret_material(
                {
                    "task_id": "JAYTEC-1",
                    "request": "research timeout handling",
                    "constraints": ["no production writes"],
                }
            )
        )

    def test_transient_results_are_not_cached(self):
        self.assertFalse(should_cache_orchestration_result({"overall_status": "TIMEOUT"}))
        self.assertFalse(should_cache_orchestration_result({"overall_status": "RATE_LIMITED"}))

    def test_terminal_results_are_cached(self):
        self.assertTrue(should_cache_orchestration_result({"overall_status": "SUCCESS"}))
        self.assertTrue(should_cache_orchestration_result({"overall_status": "FAILED_CLOSED"}))

    def test_retry_delay_is_bounded(self):
        self.assertEqual(retry_delay_seconds(1), 5)
        self.assertEqual(retry_delay_seconds(2), 10)
        self.assertEqual(retry_delay_seconds(3), 20)
        self.assertEqual(retry_delay_seconds(100), 60)


if __name__ == "__main__":
    unittest.main()
