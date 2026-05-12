import unittest

from agents.application.execution_safety import exitable_size_check


class TestExecutionSafety(unittest.TestCase):
    def test_blocks_tiny_entry_even_if_price_is_valid(self):
        result = exitable_size_check(amount_usdc=1.0, entry_price=0.50)
        self.assertFalse(result.ok)
        self.assertIn("entry_not_exitable", result.reason)

    def test_allows_entry_above_floor(self):
        result = exitable_size_check(amount_usdc=5.0, entry_price=0.50)
        self.assertTrue(result.ok)
        self.assertGreater(result.worst_exit_notional_usdc, 1.0)

    def test_rejects_invalid_price(self):
        self.assertFalse(
            exitable_size_check(amount_usdc=5.0, entry_price=0.0).ok
        )


if __name__ == "__main__":
    unittest.main()
