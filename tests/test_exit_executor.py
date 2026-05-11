import unittest
from unittest.mock import MagicMock

from agents.application.exit_executor import ExitExecutor


class TestExitExecutor(unittest.TestCase):
    def test_sell_fak_matched_closes(self):
        pm = MagicMock()
        pm.sell_shares.return_value = {"status": "matched", "orderID": "x"}
        ex = ExitExecutor(pm, sell_slippage=0.02)
        result = ex.sell_fak(token_id="tok", shares=10, mid=0.50)
        self.assertTrue(result.closed)
        self.assertEqual(result.status, "matched")
        call = pm.sell_shares.call_args
        self.assertAlmostEqual(call.kwargs["limit_price"], 0.49)
        self.assertIn("order_type", call.kwargs)

    def test_sell_fak_live_does_not_close(self):
        pm = MagicMock()
        pm.sell_shares.return_value = {"status": "live", "orderID": "x"}
        ex = ExitExecutor(pm)
        result = ex.sell_fak(token_id="tok", shares=10, mid=0.50)
        self.assertFalse(result.closed)
        self.assertEqual(result.status, "live")

    def test_sell_fak_exception_does_not_close(self):
        pm = MagicMock()
        pm.sell_shares.side_effect = RuntimeError("boom")
        ex = ExitExecutor(pm)
        result = ex.sell_fak(token_id="tok", shares=10, mid=0.50)
        self.assertFalse(result.closed)
        self.assertEqual(result.status, "exception")
        self.assertIn("boom", result.error)


if __name__ == "__main__":
    unittest.main()
