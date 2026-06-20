import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests

from samsung_auto_trader.account import AccountService
from samsung_auto_trader.trader import SamsungTrader
from samsung_auto_trader.api_client import KISClient
from samsung_auto_trader import config as sat_config


class DummyClient(KISClient):
    def __init__(self, balance_payload=None, price_payload=None, orders_payload=None):
        self.balance_payload = balance_payload or {}
        self.price_payload = price_payload or {}
        self.orders_payload = orders_payload or {}
        self.auth = self
        self.token_source = "test"

    def get_balance(self):
        return self.balance_payload

    def get_price(self, symbol: str):
        return self.price_payload

    def get_recent_daily_orders(self, days: int = 1):
        return self.orders_payload

    def authenticate(self) -> str:
        return "test-token"


class DummyClientAllFailure(KISClient):
    """Dummy client that raises HTTPError for all operations."""
    def __init__(self):
        self.auth = self
        self.token_source = "test"

    def get_balance(self):
        raise requests.HTTPError("500 Server Error: Internal Server Error for inquire-balance")

    def get_price(self, symbol: str):
        raise requests.HTTPError("500 Server Error: Internal Server Error for inquire-price")

    def get_recent_daily_orders(self, days: int = 1):
        raise requests.HTTPError("500 Server Error: Internal Server Error for inquire-daily-ccld")

    def authenticate(self) -> str:
        return "test-token"


class TestAccountService(unittest.TestCase):
    def test_account_snapshot_uses_output1_holdings_and_output2_cash(self):
        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "5"}],
            "output2": {"ord_psbl_cash": "100000", "dnca_tot_amt": "50000", "prvs_rcdl_excc_amt": "20000"},
        }
        account_service = AccountService(DummyClient(balance_payload=payload))
        snapshot = account_service.get_account_snapshot()
        self.assertEqual(snapshot["holdings"], payload["output1"])
        self.assertEqual(snapshot["available_cash"], 100000)

    def test_account_snapshot_falls_back_to_dnca_tot_amt_and_prvs_rcdl_excc_amt(self):
        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "5"}],
            "output2": {"ord_psbl_cash": "0", "dnca_tot_amt": "50000", "prvs_rcdl_excc_amt": "20000"},
        }
        account_service = AccountService(DummyClient(balance_payload=payload))
        snapshot = account_service.get_account_snapshot()
        self.assertEqual(snapshot["available_cash"], 50000)


class TestSamsungTrader(unittest.TestCase):
    def test_quantity_is_capped_and_at_least_one(self):
        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "0"}],
            "output2": {"ord_psbl_cash": "90000"},
        }
        price_payload = {"output": {"stck_prpr": "90000"}}
        dummy_client = DummyClient(balance_payload=payload, price_payload=price_payload)
        trader = SamsungTrader(
            dry_run=True,
            paper_trading=True,
            quantity=100,
            client=dummy_client,
        )
        quantity = trader._determine_quantity(90000, 90000)
        self.assertEqual(quantity, 1)
        self.assertLessEqual(quantity, 10)

    def test_sell_order_prevented_without_holdings(self):
        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "0"}],
            "output2": {"ord_psbl_cash": "100000"},
        }
        price_payload = {"output": {"stck_prpr": "100000"}}
        dummy_client = DummyClient(balance_payload=payload, price_payload=price_payload)
        trader = SamsungTrader(
            dry_run=True,
            paper_trading=True,
            quantity=1,
            client=dummy_client,
        )
        holding = trader.account_service.find_holding(payload["output1"], "005930")
        self.assertEqual(trader._holding_quantity(holding), 0)

    def test_trading_window_timezone(self):
        trader = SamsungTrader(dry_run=True, paper_trading=True, quantity=1)
        now = trader._now()
        start_time = datetime.strptime("09:10", "%H:%M").time()
        end_time = datetime.strptime("15:30", "%H:%M").time()
        self.assertEqual(trader._is_within_trading_window(), start_time <= now.time() <= end_time)

    def test_trading_window_timezone(self):
        trader = SamsungTrader(dry_run=True, paper_trading=True, quantity=1)
        now = datetime.now(ZoneInfo("Asia/Seoul"))
        self.assertEqual(trader._is_within_trading_window(), trader._now().time() >= datetime.strptime("09:10", "%H:%M").time() and trader._now().time() <= datetime.strptime("15:30", "%H:%M").time())

    def test_inspect_report_continues_when_orders_unavailable(self):
        """Verify inspect/report mode continues gracefully when all APIs fail."""
        dummy_client = DummyClientAllFailure()
        trader = SamsungTrader(
            dry_run=True,
            paper_trading=True,
            quantity=1,
            inspect=True,
            report=True,
            show_orders=True,
            client=dummy_client,
        )
        # This should not raise an exception despite all APIs being unavailable
        trader._run_inspect()
        # Verify report was still generated with defaults
        from pathlib import Path
        report_path = Path("outputs/execution_report.md")
        self.assertTrue(report_path.exists())
        report_content = report_path.read_text()
        # Report should contain "최근 주문내역 조회 불가" since orders list is empty
        self.assertIn("최근 주문내역 조회 불가", report_content)
        # Report should show available_cash as 0 due to snapshot failure
        self.assertIn("Available cash: 0", report_content)
        # recent_orders.csv must exist even when orders unavailable
        orders_csv = Path("outputs/recent_orders.csv")
        self.assertTrue(orders_csv.exists())

    def test_no_sensitive_logs_and_csv_on_error(self):
        """Ensure logs do not contain CANO/account/appkey/appsecret/token/authorization or query strings."""
        # inject sensitive-looking values into config
        sat_config.gh_account = "CANO12345"
        sat_config.gh_appkey = "APPKEY_SECRET"
        sat_config.gh_appsecret = "APPSECRET_SECRET"
        dummy_client = DummyClientAllFailure()
        trader = SamsungTrader(
            dry_run=True,
            paper_trading=True,
            quantity=1,
            inspect=True,
            report=True,
            show_orders=True,
            client=dummy_client,
        )
        import logging
        from io import StringIO
        from samsung_auto_trader.logger import logger

        stream = StringIO()
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        try:
            trader._run_inspect()
        finally:
            logger.removeHandler(handler)

        logs = stream.getvalue()
        # Assert sensitive substrings are NOT present
        self.assertNotIn("CANO=", logs)
        self.assertNotIn("CANO12345", logs)
        self.assertNotIn("APPKEY_SECRET", logs)
        self.assertNotIn("APPSECRET_SECRET", logs)
        self.assertNotIn("http://", logs)
        self.assertNotIn("https://", logs)
        self.assertNotIn("authorization", logs)


if __name__ == "__main__":
    unittest.main()