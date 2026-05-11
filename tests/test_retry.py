import unittest
from unittest.mock import patch

from agents.application.retry import is_rate_limit_error, run_with_retries


class RetryTests(unittest.TestCase):
    def test_rate_limit_errors_are_detected(self):
        error = Exception(
            "You've hit your session rate limit. Please upgrade your plan or wait 2 hours."
        )

        self.assertTrue(is_rate_limit_error(error))

    @patch("agents.application.retry.time.sleep", return_value=None)
    def test_non_rate_limit_errors_retry_until_success(self, _sleep):
        attempts = {"count": 0}

        def flaky_operation():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise Exception("temporary failure")
            return "ok"

        result = run_with_retries(
            flaky_operation,
            operation_name="flaky_operation",
            max_attempts=3,
            retry_delay_seconds=0,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(attempts["count"], 3)

    def test_rate_limit_errors_fail_without_retrying(self):
        attempts = {"count": 0}

        def rate_limited_operation():
            attempts["count"] += 1
            raise Exception(
                "You've hit your session rate limit. Please upgrade your plan or wait 2 hours."
            )

        with self.assertRaises(RuntimeError) as context:
            run_with_retries(
                rate_limited_operation,
                operation_name="rate_limited_operation",
                max_attempts=3,
                retry_delay_seconds=0,
            )

        self.assertIn("rate-limit error", str(context.exception))
        self.assertEqual(attempts["count"], 1)


if __name__ == "__main__":
    unittest.main()
