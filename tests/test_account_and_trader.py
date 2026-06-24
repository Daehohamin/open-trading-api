import os
import unittest
from datetime import datetime
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo
import requests

from samsung_auto_trader.account import AccountService
from samsung_auto_trader.orders import OrderService
from samsung_auto_trader.price_utils import get_tick_size, normalize_order_price
from samsung_auto_trader.trader import SamsungTrader
from samsung_auto_trader.api_client import KISClient, OrderStatus
from samsung_auto_trader.config import config as app_config
from samsung_auto_trader.kis_rate_limit import reset_kis_rate_limiter


class DummyClient(KISClient):
    def __init__(self, balance_payload=None, price_payload=None, orders_payload=None, buying_power_payload=None):
        self.balance_payload = balance_payload or {}
        self.price_payload = price_payload or {}
        self.orders_payload = orders_payload or {}
        self.buying_power_payload = buying_power_payload or {"buying_power_amount": 0, "buying_power_quantity": 0}
        self.buying_power_calls: list[tuple[str, int]] = []
        self.auth = self
        self.token_source = "test"

    def get_balance(self):
        return self.balance_payload

    def get_price(self, symbol: str):
        return self.price_payload

    def get_recent_daily_orders(self, days: int = 1):
        return self.orders_payload

    def get_buying_power(self, symbol: str, order_price: int):
        self.buying_power_calls.append((symbol, order_price))
        return self.buying_power_payload

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

    def get_buying_power(self, symbol: str, order_price: int):
        raise requests.HTTPError("500 Server Error: Internal Server Error for inquire-psbl-order")

    def authenticate(self) -> str:
        return "test-token"


class TestAccountService(unittest.TestCase):
    def test_account_snapshot_parses_output1_holdings_and_output2_settlement_fields(self):
        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "5"}],
            "output2": {
                "ord_psbl_cash": "100000",
                "dnca_tot_amt": "30000000",
                "nxdy_excc_amt": "50000",
                "prvs_rcdl_excc_amt": "20000",
            },
        }
        account_service = AccountService(DummyClient(balance_payload=payload))
        snapshot = account_service.get_account_snapshot()
        self.assertEqual(snapshot["holdings"], payload["output1"])
        self.assertEqual(snapshot["deposit_total"], 30000000)
        self.assertEqual(snapshot["next_day_settlement_amount"], 50000)
        self.assertEqual(snapshot["provisional_settlement_amount"], 20000)
        self.assertNotIn("available_cash", snapshot)
        self.assertNotIn("buying_power_amount", snapshot)

    def test_account_snapshot_does_not_use_settlement_fields_as_buying_power(self):
        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "5"}],
            "output2": {"dnca_tot_amt": "30000000", "nxdy_excc_amt": "50000", "prvs_rcdl_excc_amt": "20000"},
        }
        account_service = AccountService(DummyClient(balance_payload=payload))
        snapshot = account_service.get_account_snapshot()
        self.assertEqual(snapshot["deposit_total"], 30000000)
        self.assertEqual(snapshot["next_day_settlement_amount"], 50000)
        self.assertEqual(snapshot["provisional_settlement_amount"], 20000)
        self.assertNotIn("buying_power_amount", snapshot)
        self.assertNotIn("buying_power_quantity", snapshot)


class FakeOrderService(OrderService):
    def __init__(self) -> None:
        super().__init__(client=DummyClient(), paper_trading=True)
        self.buy_calls = 0
        self.sell_calls = 0
        self.buy_orders: list[tuple[str, int, int]] = []
        self.sell_orders: list[tuple[str, int, int]] = []

    def place_buy_order(self, symbol: str, quantity: int, price: int) -> dict[str, Any]:
        self.buy_calls += 1
        self.buy_orders.append((symbol, quantity, price))
        return {"result": "buy_called"}

    def place_sell_order(self, symbol: str, quantity: int, price: int) -> dict[str, Any]:
        self.sell_calls += 1
        self.sell_orders.append((symbol, quantity, price))
        return {"result": "sell_called"}


def make_order_status(
    order_number: str,
    status: str,
    side: str = "buy",
    filled_quantity: int = 0,
    ordered_quantity: int = 1,
    remaining_quantity: int | None = None,
    average_fill_price: int = 0,
) -> OrderStatus:
    if remaining_quantity is None:
        remaining_quantity = max(0, ordered_quantity - filled_quantity)
    return OrderStatus(
        order_number=order_number,
        side=side,
        symbol="005930",
        ordered_quantity=ordered_quantity,
        filled_quantity=filled_quantity,
        remaining_quantity=remaining_quantity,
        order_price=70000,
        average_fill_price=average_fill_price,
        rejected_quantity=ordered_quantity if status == "REJECTED" else 0,
        cancelled=status == "CANCELLED",
        status=status,
    )


class AutoCycleClient(KISClient):
    def __init__(
        self,
        holdings: list[int] | None = None,
        prices: list[int] | None = None,
        buy_statuses: list[OrderStatus] | None = None,
        sell_statuses: list[OrderStatus] | None = None,
        order_statuses: dict[str, list[OrderStatus]] | None = None,
        recent_orders: dict[str, Any] | None = None,
    ) -> None:
        self.holdings = holdings or [0]
        self.prices = prices or [100000]
        self.buy_statuses = buy_statuses or []
        self.sell_statuses = sell_statuses or []
        self.order_statuses = order_statuses or {}
        self.recent_orders = recent_orders or {"output1": []}
        self.buying_power_calls: list[tuple[str, int]] = []
        self.status_calls: list[str] = []
        self.auth = self
        self.token_source = "test"

    def get_balance(self):
        if len(self.holdings) > 1:
            qty = self.holdings.pop(0)
        else:
            qty = self.holdings[0]
        return {"output1": [{"pdno": "005930", "hldg_qty": str(qty)}], "output2": {"dnca_tot_amt": "1000000"}}

    def get_price(self, symbol: str):
        if len(self.prices) > 1:
            price = self.prices.pop(0)
        else:
            price = self.prices[0]
        return {"output": {"stck_prpr": str(price)}}

    def get_recent_daily_orders(self, days: int = 1):
        return self.recent_orders

    def get_buying_power(self, symbol: str, order_price: int):
        self.buying_power_calls.append((symbol, order_price))
        return {"buying_power_amount": 1000000, "buying_power_quantity": 10}

    def get_order_status(self, order_number: str) -> OrderStatus:
        self.status_calls.append(order_number)
        if order_number in self.order_statuses:
            statuses = self.order_statuses[order_number]
            if len(statuses) > 1:
                return statuses.pop(0)
            return statuses[0]
        statuses = self.buy_statuses if order_number.startswith("B") else self.sell_statuses
        if len(statuses) > 1:
            return statuses.pop(0)
        return statuses[0]

    def authenticate(self) -> str:
        return "test-token"


class AutoCycleOrderService(FakeOrderService):
    def __init__(self) -> None:
        super().__init__()
        self.buy_order_numbers: list[str] = []
        self.sell_order_numbers: list[str] = []

    def place_buy_order(self, symbol: str, quantity: int, price: int) -> dict[str, Any]:
        super().place_buy_order(symbol, quantity, price)
        order_number = f"B{self.buy_calls:03d}"
        self.buy_order_numbers.append(order_number)
        return {"output": {"ODNO": order_number}}

    def place_sell_order(self, symbol: str, quantity: int, price: int) -> dict[str, Any]:
        super().place_sell_order(symbol, quantity, price)
        order_number = f"S{self.sell_calls:03d}"
        self.sell_order_numbers.append(order_number)
        return {"output": {"ODNO": order_number}}


class TestPriceUtils(unittest.TestCase):
    def test_tick_size_boundaries(self):
        cases = [
            (1, 1),
            (1_999, 1),
            (2_000, 5),
            (4_999, 5),
            (5_000, 10),
            (19_999, 10),
            (20_000, 50),
            (49_999, 50),
            (50_000, 100),
            (199_999, 100),
            (200_000, 500),
            (499_999, 500),
            (500_000, 1_000),
        ]
        for price, expected_tick in cases:
            with self.subTest(price=price):
                self.assertEqual(get_tick_size(price), expected_tick)

    def test_normalize_buy_rounds_down_to_tick(self):
        self.assertEqual(normalize_order_price(351_250, "buy"), 351_000)

    def test_normalize_sell_rounds_up_to_tick(self):
        self.assertEqual(normalize_order_price(355_250, "sell"), 355_500)

    def test_valid_price_remains_unchanged(self):
        self.assertEqual(normalize_order_price(353_500, "buy"), 353_500)
        self.assertEqual(normalize_order_price(353_500, "sell"), 353_500)

    def test_zero_or_negative_prices_are_rejected(self):
        for price in [0, -1]:
            with self.subTest(price=price):
                with self.assertRaises(ValueError):
                    normalize_order_price(price, "buy")


class TestSamsungTrader(unittest.TestCase):
    def _open_market_time(self) -> datetime:
        return datetime(2026, 6, 23, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))

    def _auto_trader(self, client: AutoCycleClient, order_service: AutoCycleOrderService, **kwargs: Any) -> SamsungTrader:
        return SamsungTrader(
            offset=2000,
            dry_run=False,
            paper_trading=True,
            quantity=1,
            auto_cycle=True,
            order_status_timeout=1,
            order_status_poll_interval=0,
            client=client,
            order_service=order_service,
            **kwargs,
        )

    def test_quantity_is_capped_and_at_least_one(self):
        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "0"}],
            "output2": {"ord_psbl_cash": "90000"},
        }
        price_payload = {"output": {"stck_prpr": "90000"}}
        dummy_client = DummyClient(
            balance_payload=payload,
            price_payload=price_payload,
            buying_power_payload={"buying_power_amount": 1000000, "buying_power_quantity": 3},
        )
        fake_order_service = FakeOrderService()
        trader = SamsungTrader(
            dry_run=True,
            paper_trading=True,
            quantity=100,
            client=dummy_client,
            order_service=fake_order_service,
        )
        quantity = trader._determine_quantity(1)
        self.assertEqual(quantity, 1)
        self.assertLessEqual(quantity, 10)

    def test_sell_order_prevented_without_holdings(self):
        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "0"}],
            "output2": {"ord_psbl_cash": "100000"},
        }
        price_payload = {"output": {"stck_prpr": "100000"}}
        dummy_client = DummyClient(
            balance_payload=payload,
            price_payload=price_payload,
            buying_power_payload={"buying_power_amount": 1000000, "buying_power_quantity": 3},
        )
        trader = SamsungTrader(
            dry_run=True,
            paper_trading=True,
            quantity=1,
            client=dummy_client,
        )
        holding = trader.account_service.find_holding(payload["output1"], "005930")
        self.assertIsNotNone(holding)
        assert holding is not None
        self.assertEqual(trader._holding_quantity(holding), 0)

    def test_buy_only_submits_only_buy_order(self):
        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "10"}],
            "output2": {"ord_psbl_cash": "1000000"},
        }
        price_payload = {"output": {"stck_prpr": "100000"}}
        dummy_client = DummyClient(
            balance_payload=payload,
            price_payload=price_payload,
            buying_power_payload={"buying_power_amount": 1000000, "buying_power_quantity": 3},
        )
        fake_order_service = FakeOrderService()
        trader = SamsungTrader(
            dry_run=False,
            paper_trading=True,
            quantity=1,
            buy_only=True,
            client=dummy_client,
            order_service=fake_order_service,
        )
        with patch("samsung_auto_trader.trader.time.sleep", return_value=None):
            trader._run_trade_cycle()
        self.assertEqual(fake_order_service.buy_calls, 1)
        self.assertEqual(fake_order_service.sell_calls, 0)
        self.assertEqual(dummy_client.buying_power_calls, [("005930", 98000)])

    def test_buy_only_passes_normalized_price_to_order_service(self):
        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "10"}],
            "output2": {"ord_psbl_cash": "1000000"},
        }
        price_payload = {"output": {"stck_prpr": "353250"}}
        dummy_client = DummyClient(
            balance_payload=payload,
            price_payload=price_payload,
            buying_power_payload={"buying_power_amount": 1000000, "buying_power_quantity": 1},
        )
        fake_order_service = FakeOrderService()
        trader = SamsungTrader(
            offset=2000,
            dry_run=False,
            paper_trading=True,
            quantity=1,
            buy_only=True,
            client=dummy_client,
            order_service=fake_order_service,
        )
        with patch("samsung_auto_trader.trader.time.sleep", return_value=None):
            trader._run_trade_cycle()
        self.assertEqual(fake_order_service.buy_orders, [("005930", 1, 351000)])
        self.assertEqual(fake_order_service.sell_calls, 0)
        self.assertEqual(dummy_client.buying_power_calls, [("005930", 351000)])

    def test_sell_only_submits_sell_order_when_holdings_sufficient(self):
        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "10"}],
            "output2": {"ord_psbl_cash": "100000"},
        }
        price_payload = {"output": {"stck_prpr": "100000"}}
        dummy_client = DummyClient(balance_payload=payload, price_payload=price_payload)
        fake_order_service = FakeOrderService()
        trader = SamsungTrader(
            dry_run=False,
            paper_trading=True,
            quantity=1,
            sell_only=True,
            client=dummy_client,
            order_service=fake_order_service,
        )
        with patch("samsung_auto_trader.trader.time.sleep", return_value=None):
            trader._run_trade_cycle()
        self.assertEqual(fake_order_service.sell_calls, 1)
        self.assertEqual(fake_order_service.buy_calls, 0)
        self.assertEqual(dummy_client.buying_power_calls, [])

    def test_sell_only_passes_normalized_price_to_order_service(self):
        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "10"}],
            "output2": {"ord_psbl_cash": "100000"},
        }
        price_payload = {"output": {"stck_prpr": "353250"}}
        dummy_client = DummyClient(balance_payload=payload, price_payload=price_payload)
        fake_order_service = FakeOrderService()
        trader = SamsungTrader(
            offset=2000,
            dry_run=False,
            paper_trading=True,
            quantity=1,
            sell_only=True,
            client=dummy_client,
            order_service=fake_order_service,
        )
        with patch("samsung_auto_trader.trader.time.sleep", return_value=None):
            trader._run_trade_cycle()
        self.assertEqual(fake_order_service.sell_orders, [("005930", 1, 355500)])
        self.assertEqual(fake_order_service.buy_calls, 0)
        self.assertEqual(dummy_client.buying_power_calls, [])

    def test_trade_cycle_price_normalization_tests_use_no_external_requests(self):
        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "10"}],
            "output2": {"ord_psbl_cash": "1000000"},
        }
        price_payload = {"output": {"stck_prpr": "353250"}}
        dummy_client = DummyClient(
            balance_payload=payload,
            price_payload=price_payload,
            buying_power_payload={"buying_power_amount": 1000000, "buying_power_quantity": 1},
        )
        fake_order_service = FakeOrderService()
        trader = SamsungTrader(
            offset=2000,
            dry_run=False,
            paper_trading=True,
            quantity=1,
            buy_only=True,
            client=dummy_client,
            order_service=fake_order_service,
        )
        with patch("samsung_auto_trader.trader.time.sleep", return_value=None):
            with patch("samsung_auto_trader.api_client.requests.get") as get:
                with patch("samsung_auto_trader.api_client.requests.post") as post:
                    trader._run_trade_cycle()
        get.assert_not_called()
        post.assert_not_called()
        self.assertEqual(fake_order_service.buy_orders, [("005930", 1, 351000)])
        self.assertEqual(dummy_client.buying_power_calls, [("005930", 351000)])

    def test_sell_only_submits_nothing_when_holdings_insufficient(self):
        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "0"}],
            "output2": {"ord_psbl_cash": "100000"},
        }
        price_payload = {"output": {"stck_prpr": "100000"}}
        dummy_client = DummyClient(balance_payload=payload, price_payload=price_payload)
        fake_order_service = FakeOrderService()
        trader = SamsungTrader(
            dry_run=False,
            paper_trading=True,
            quantity=1,
            sell_only=True,
            client=dummy_client,
            order_service=fake_order_service,
        )
        with patch("samsung_auto_trader.trader.time.sleep", return_value=None):
            trader._run_trade_cycle()
        self.assertEqual(fake_order_service.sell_calls, 0)
        self.assertEqual(fake_order_service.buy_calls, 0)
        self.assertEqual(dummy_client.buying_power_calls, [])

    def test_dry_run_submits_no_orders(self):
        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "10"}],
            "output2": {"ord_psbl_cash": "1000000"},
        }
        price_payload = {"output": {"stck_prpr": "100000"}}
        dummy_client = DummyClient(
            balance_payload=payload,
            price_payload=price_payload,
            buying_power_payload={"buying_power_amount": 1000000, "buying_power_quantity": 1},
        )
        fake_order_service = FakeOrderService()
        trader = SamsungTrader(
            dry_run=True,
            paper_trading=True,
            quantity=1,
            client=dummy_client,
            order_service=fake_order_service,
        )
        with patch("samsung_auto_trader.trader.time.sleep", return_value=None):
            trader._run_trade_cycle()
        self.assertEqual(fake_order_service.buy_calls, 0)
        self.assertEqual(fake_order_service.sell_calls, 0)
        self.assertEqual(dummy_client.buying_power_calls, [("005930", 98000)])

    def test_auto_cycle_buy_fills_then_sell_is_submitted(self):
        client = AutoCycleClient(
            holdings=[0, 1, 0],
            buy_statuses=[make_order_status("B001", "FILLED", filled_quantity=1, average_fill_price=98000)],
            sell_statuses=[make_order_status("S001", "FILLED", side="sell", filled_quantity=1, average_fill_price=102000)],
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            with patch("samsung_auto_trader.trader.time.sleep", return_value=None):
                completed = trader._run_auto_cycle_once()
        self.assertTrue(completed)
        self.assertEqual(order_service.buy_calls, 1)
        self.assertEqual(order_service.sell_calls, 1)
        self.assertEqual(order_service.sell_orders, [("005930", 1, 102000)])

    def test_auto_cycle_buy_offset_zero_uses_current_price(self):
        client = AutoCycleClient(
            prices=[100000, 100000],
            holdings=[0, 1, 0],
            buy_statuses=[make_order_status("B001", "FILLED", filled_quantity=1, average_fill_price=100000)],
            sell_statuses=[make_order_status("S001", "FILLED", side="sell", filled_quantity=1, average_fill_price=102000)],
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service, buy_offset=0)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            completed = trader._run_auto_cycle_once()
        self.assertTrue(completed)
        self.assertEqual(order_service.buy_orders, [("005930", 1, 100000)])
        self.assertEqual(client.buying_power_calls, [("005930", 100000)])

    def test_auto_cycle_take_profit_uses_buy_average_fill_price(self):
        client = AutoCycleClient(
            holdings=[0, 1, 0],
            buy_statuses=[make_order_status("B001", "FILLED", filled_quantity=1, average_fill_price=98000)],
            sell_statuses=[make_order_status("S001", "FILLED", side="sell", filled_quantity=1, average_fill_price=98500)],
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service, take_profit=500)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            completed = trader._run_auto_cycle_once()
        self.assertTrue(completed)
        self.assertEqual(order_service.sell_orders, [("005930", 1, 98500)])

    def test_auto_cycle_take_profit_sell_price_is_normalized_to_krx_tick(self):
        client = AutoCycleClient(
            holdings=[0, 1, 0],
            buy_statuses=[make_order_status("B001", "FILLED", filled_quantity=1, average_fill_price=98250)],
            sell_statuses=[make_order_status("S001", "FILLED", side="sell", filled_quantity=1, average_fill_price=98800)],
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service, take_profit=500)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            completed = trader._run_auto_cycle_once()
        self.assertTrue(completed)
        self.assertEqual(order_service.sell_orders, [("005930", 1, 98800)])

    def test_auto_cycle_take_profit_falls_back_to_submitted_buy_price(self):
        client = AutoCycleClient(
            holdings=[0, 1, 0],
            buy_statuses=[make_order_status("B001", "FILLED", filled_quantity=1, average_fill_price=0)],
            sell_statuses=[make_order_status("S001", "FILLED", side="sell", filled_quantity=1, average_fill_price=98500)],
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service, take_profit=500)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            completed = trader._run_auto_cycle_once()
        self.assertTrue(completed)
        self.assertEqual(order_service.buy_orders, [("005930", 1, 98000)])
        self.assertEqual(order_service.sell_orders, [("005930", 1, 98500)])

    def test_auto_cycle_offset_behavior_unchanged_without_new_options(self):
        client = AutoCycleClient(
            prices=[100000, 100000],
            holdings=[0, 1, 0],
            buy_statuses=[make_order_status("B001", "FILLED", filled_quantity=1, average_fill_price=98000)],
            sell_statuses=[make_order_status("S001", "FILLED", side="sell", filled_quantity=1, average_fill_price=102000)],
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            completed = trader._run_auto_cycle_once()
        self.assertTrue(completed)
        self.assertEqual(order_service.buy_orders, [("005930", 1, 98000)])
        self.assertEqual(order_service.sell_orders, [("005930", 1, 102000)])

    def test_auto_cycle_cycle_count_two_runs_two_successful_cycles(self):
        client = AutoCycleClient(
            holdings=[0, 1, 0, 0, 1, 0],
            order_statuses={
                "B001": [make_order_status("B001", "FILLED", filled_quantity=1, average_fill_price=98000)],
                "S001": [make_order_status("S001", "FILLED", side="sell", filled_quantity=1, average_fill_price=102000)],
                "B002": [make_order_status("B002", "FILLED", filled_quantity=1, average_fill_price=98000)],
                "S002": [make_order_status("S002", "FILLED", side="sell", filled_quantity=1, average_fill_price=102000)],
            },
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service, cycle_count=2)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            with patch("samsung_auto_trader.trader.time.sleep", return_value=None):
                trader._run_auto_cycle(once=False)
        self.assertEqual(order_service.buy_calls, 2)
        self.assertEqual(order_service.sell_calls, 2)
        self.assertEqual(order_service.buy_order_numbers, ["B001", "B002"])
        self.assertEqual(order_service.sell_order_numbers, ["S001", "S002"])

    def test_auto_cycle_first_pending_timeout_prevents_second_cycle(self):
        client = AutoCycleClient(
            holdings=[0, 0],
            order_statuses={
                "B001": [make_order_status("B001", "PENDING")],
                "B002": [make_order_status("B002", "FILLED", filled_quantity=1, average_fill_price=98000)],
            },
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service, cycle_count=2)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            with patch("samsung_auto_trader.trader.time.monotonic", side_effect=[0, 2]):
                trader._run_auto_cycle(once=False)
        self.assertEqual(order_service.buy_calls, 1)
        self.assertEqual(order_service.sell_calls, 0)

    def test_auto_cycle_waits_for_delayed_buy_holding_update_before_selling(self):
        client = AutoCycleClient(
            holdings=[0, 0, 1, 0],
            buy_statuses=[make_order_status("B001", "FILLED", filled_quantity=1, average_fill_price=98000)],
            sell_statuses=[make_order_status("S001", "FILLED", side="sell", filled_quantity=1, average_fill_price=102000)],
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            with patch("samsung_auto_trader.trader.time.sleep", return_value=None):
                completed = trader._run_auto_cycle_once()
        self.assertTrue(completed)
        self.assertEqual(order_service.buy_calls, 1)
        self.assertEqual(order_service.sell_calls, 1)
        self.assertEqual(order_service.sell_orders, [("005930", 1, 102000)])

    def test_auto_cycle_waits_for_delayed_sell_holding_update(self):
        client = AutoCycleClient(
            holdings=[3, 4, 4, 3],
            buy_statuses=[make_order_status("B001", "FILLED", filled_quantity=1, average_fill_price=98000)],
            sell_statuses=[make_order_status("S001", "FILLED", side="sell", filled_quantity=1, average_fill_price=102000)],
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            with patch("samsung_auto_trader.trader.time.sleep", return_value=None):
                completed = trader._run_auto_cycle_once()
        self.assertTrue(completed)
        self.assertEqual(order_service.buy_calls, 1)
        self.assertEqual(order_service.sell_calls, 1)
        self.assertEqual(client.holdings[0], 3)

    def test_auto_cycle_holding_confirmation_timeout_prevents_next_order(self):
        client = AutoCycleClient(
            holdings=[0, 0],
            buy_statuses=[make_order_status("B001", "FILLED", filled_quantity=1, average_fill_price=98000)],
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            with patch("samsung_auto_trader.trader.time.monotonic", side_effect=[0, 0, 2]):
                completed = trader._run_auto_cycle_once()
        self.assertFalse(completed)
        self.assertEqual(order_service.buy_calls, 1)
        self.assertEqual(order_service.sell_calls, 0)

    def test_auto_cycle_pending_buy_prevents_sell_submission(self):
        client = AutoCycleClient(
            holdings=[0, 0],
            buy_statuses=[make_order_status("B001", "PENDING")],
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            with patch("samsung_auto_trader.trader.time.monotonic", side_effect=[0, 2]):
                completed = trader._run_auto_cycle_once()
        self.assertFalse(completed)
        self.assertEqual(order_service.buy_calls, 1)
        self.assertEqual(order_service.sell_calls, 0)

    def test_auto_cycle_partially_filled_buy_prevents_sell_submission(self):
        client = AutoCycleClient(
            holdings=[0, 0],
            buy_statuses=[make_order_status("B001", "PARTIALLY_FILLED", filled_quantity=1, ordered_quantity=2)],
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            with patch("samsung_auto_trader.trader.time.monotonic", side_effect=[0, 2]):
                completed = trader._run_auto_cycle_once()
        self.assertFalse(completed)
        self.assertEqual(order_service.buy_calls, 1)
        self.assertEqual(order_service.sell_calls, 0)

    def test_auto_cycle_rejected_buy_prevents_sell_submission(self):
        client = AutoCycleClient(
            holdings=[0, 0],
            buy_statuses=[make_order_status("B001", "REJECTED")],
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            completed = trader._run_auto_cycle_once()
        self.assertFalse(completed)
        self.assertEqual(order_service.buy_calls, 1)
        self.assertEqual(order_service.sell_calls, 0)

    def test_auto_cycle_cancelled_buy_prevents_sell_submission(self):
        client = AutoCycleClient(
            holdings=[0, 0],
            buy_statuses=[make_order_status("B001", "CANCELLED")],
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            completed = trader._run_auto_cycle_once()
        self.assertFalse(completed)
        self.assertEqual(order_service.buy_calls, 1)
        self.assertEqual(order_service.sell_calls, 0)

    def test_auto_cycle_buy_timeout_prevents_sell_submission(self):
        client = AutoCycleClient(
            holdings=[0, 0],
            buy_statuses=[make_order_status("B001", "NOT_FOUND")],
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            with patch("samsung_auto_trader.trader.time.monotonic", side_effect=[0, 2]):
                completed = trader._run_auto_cycle_once()
        self.assertFalse(completed)
        self.assertEqual(order_service.buy_calls, 1)
        self.assertEqual(order_service.sell_calls, 0)

    def test_auto_cycle_existing_pending_order_blocks_duplicate_order(self):
        pending_order = {
            "odno": "EXISTING",
            "ord_qty": "1",
            "ord_unpr": "98000",
            "tot_ccld_qty": "0",
            "avg_prvs": "0",
            "rmn_qty": "1",
            "rjct_qty": "0",
            "cncl_yn": "N",
            "sll_buy_dvsn_cd_name": "매수",
            "pdno": "005930",
        }
        client = AutoCycleClient(holdings=[0], recent_orders={"output1": [pending_order]})
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            completed = trader._run_auto_cycle_once()
        self.assertFalse(completed)
        self.assertEqual(order_service.buy_calls, 0)
        self.assertEqual(order_service.sell_calls, 0)

    def test_auto_cycle_cancelled_sell_row_with_zero_remaining_does_not_block_duplicate_check(self):
        cancelled_sell_order = {
            "odno": "CANCELLED_SELL",
            "ord_qty": "1",
            "ord_unpr": "98500",
            "tot_ccld_qty": "0",
            "avg_prvs": "0",
            "rmn_qty": "0",
            "rjct_qty": "0",
            "cncl_yn": "N",
            "cncl_cfrm_qty": "1",
            "sll_buy_dvsn_cd_name": "매도",
            "pdno": "005930",
        }
        client = AutoCycleClient(holdings=[0])
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service)
        self.assertFalse(trader._is_pending_order_row(cancelled_sell_order, "005930"))
        self.assertFalse(trader._has_existing_pending_order([cancelled_sell_order], "005930"))

    def test_auto_cycle_pending_row_with_remaining_quantity_still_blocks_duplicate_check(self):
        pending_order = {
            "odno": "PENDING_BUY",
            "ord_qty": "1",
            "ord_unpr": "98000",
            "tot_ccld_qty": "0",
            "avg_prvs": "0",
            "rmn_qty": "1",
            "rjct_qty": "0",
            "cncl_yn": "N",
            "cncl_cfrm_qty": "0",
            "sll_buy_dvsn_cd_name": "매수",
            "pdno": "005930",
        }
        client = AutoCycleClient(holdings=[0])
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service)
        self.assertTrue(trader._is_pending_order_row(pending_order, "005930"))
        self.assertTrue(trader._has_existing_pending_order([pending_order], "005930"))

    def test_auto_cycle_sell_fills_and_final_holdings_equal_baseline(self):
        client = AutoCycleClient(
            holdings=[3, 4, 3],
            buy_statuses=[make_order_status("B001", "FILLED", filled_quantity=1, average_fill_price=98000)],
            sell_statuses=[make_order_status("S001", "FILLED", side="sell", filled_quantity=1, average_fill_price=102000)],
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            completed = trader._run_auto_cycle_once()
        self.assertTrue(completed)
        self.assertEqual(order_service.sell_calls, 1)
        self.assertEqual(client.holdings[0], 3)

    def test_auto_cycle_sell_timeout_stops_cycle(self):
        client = AutoCycleClient(
            holdings=[0, 1, 1],
            buy_statuses=[make_order_status("B001", "FILLED", filled_quantity=1)],
            sell_statuses=[make_order_status("S001", "PENDING", side="sell")],
        )
        order_service = AutoCycleOrderService()
        trader = self._auto_trader(client, order_service)
        with patch.object(trader, "_now", return_value=self._open_market_time()):
            with patch("samsung_auto_trader.trader.time.monotonic", side_effect=[0, 0, 2, 4]):
                completed = trader._run_auto_cycle_once()
        self.assertFalse(completed)
        self.assertEqual(order_service.buy_calls, 1)
        self.assertEqual(order_service.sell_calls, 1)

    def test_buy_quantity_is_capped_by_buying_power_quantity(self):
        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "0"}],
            "output2": {"dnca_tot_amt": "30000000"},
        }
        price_payload = {"output": {"stck_prpr": "100000"}}
        dummy_client = DummyClient(
            balance_payload=payload,
            price_payload=price_payload,
            buying_power_payload={"buying_power_amount": 300000, "buying_power_quantity": 2},
        )
        fake_order_service = FakeOrderService()
        trader = SamsungTrader(
            dry_run=False,
            paper_trading=True,
            quantity=10,
            buy_only=True,
            client=dummy_client,
            order_service=fake_order_service,
        )
        with patch("samsung_auto_trader.trader.time.sleep", return_value=None):
            trader._run_trade_cycle()
        self.assertEqual(fake_order_service.buy_orders, [("005930", 2, 98000)])

    def test_buying_power_unavailable_uses_no_balance_fallback(self):
        class BuyingPowerFailureClient(DummyClient):
            def get_buying_power(self, symbol: str, order_price: int):
                self.buying_power_calls.append((symbol, order_price))
                raise requests.HTTPError("inquire-psbl-order failed")

        payload = {
            "output1": [{"pdno": "005930", "hldg_qty": "0"}],
            "output2": {"dnca_tot_amt": "30000000", "nxdy_excc_amt": "30000000", "prvs_rcdl_excc_amt": "30000000"},
        }
        price_payload = {"output": {"stck_prpr": "100000"}}
        dummy_client = BuyingPowerFailureClient(balance_payload=payload, price_payload=price_payload)
        fake_order_service = FakeOrderService()
        trader = SamsungTrader(
            dry_run=False,
            paper_trading=True,
            quantity=1,
            buy_only=True,
            client=dummy_client,
            order_service=fake_order_service,
        )
        with patch("samsung_auto_trader.trader.time.sleep", return_value=None):
            trader._run_trade_cycle()
        self.assertEqual(dummy_client.buying_power_calls, [("005930", 98000)])
        self.assertEqual(fake_order_service.buy_calls, 0)
        self.assertEqual(fake_order_service.sell_calls, 0)

    def test_trading_window_timezone(self):
        trader = SamsungTrader(dry_run=True, paper_trading=True, quantity=1, client=DummyClient())
        now = trader._now()
        start_time = datetime.strptime("09:10", "%H:%M").time()
        end_time = datetime.strptime("15:30", "%H:%M").time()
        self.assertEqual(trader._is_within_trading_window(), now.weekday() < 5 and start_time <= now.time() <= end_time)

    def test_sunday_morning_is_outside_trading_window(self):
        trader = SamsungTrader(dry_run=True, paper_trading=True, quantity=1, client=DummyClient())
        sunday_morning = datetime(2026, 6, 21, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        with patch.object(trader, "_now", return_value=sunday_morning):
            self.assertFalse(trader._is_within_trading_window())

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
        with TemporaryDirectory() as temp_outputs:
            original_outputs_dir = app_config.outputs_dir
            object.__setattr__(app_config, "outputs_dir", temp_outputs)
            try:
                trader._run_inspect()
                from pathlib import Path

                report_path = Path(temp_outputs) / "execution_report.md"
                self.assertTrue(report_path.exists())
                report_content = report_path.read_text()
                self.assertIn("최근 주문내역 조회 불가", report_content)
                self.assertIn("Deposit total: unavailable", report_content)
                self.assertIn("Buying power: not queried", report_content)
                self.assertNotIn("Available " + "cash", report_content)
                summary_content = (Path(temp_outputs) / "account_summary.svg").read_text()
                self.assertIn("Deposit total", summary_content)
                self.assertNotIn("Available " + "cash", summary_content)
                orders_csv = Path(temp_outputs) / "recent_orders.csv"
                self.assertTrue(orders_csv.exists())
            finally:
                object.__setattr__(app_config, "outputs_dir", original_outputs_dir)

    def test_order_row_korean_summary_maps_filled_and_remaining_quantities(self):
        trader = SamsungTrader(dry_run=True, paper_trading=True, quantity=1, client=DummyClient())
        row = {
            "odno": "B001",
            "ord_qty": "3",
            "ord_unpr": "70000",
            "tot_ccld_qty": "2",
            "avg_prvs": "70100",
            "rmn_qty": "1",
            "rjct_qty": "0",
            "cncl_yn": "N",
            "sll_buy_dvsn_cd_name": "매수",
            "pdno": "005930",
        }
        with patch("samsung_auto_trader.api_client.requests.get") as get:
            with patch("samsung_auto_trader.api_client.requests.post") as post:
                summary = trader.format_order_row_korean(row)
        get.assert_not_called()
        post.assert_not_called()
        self.assertIn("체결수량: 2주", summary)
        self.assertIn("미체결수량: 1주", summary)

    def test_holding_korean_summary_maps_holding_quantity_and_average_buy_price(self):
        trader = SamsungTrader(dry_run=True, paper_trading=True, quantity=1, client=DummyClient())
        holding = {
            "pdno": "005930",
            "hldg_qty": "4",
            "ord_psbl_qty": "3",
            "pchs_avg_pric": "71000",
            "pchs_amt": "284000",
            "evlu_amt": "300000",
            "evlu_pfls_amt": "16000",
            "evlu_pfls_rt": "5.63",
        }
        with patch("samsung_auto_trader.api_client.requests.get") as get:
            with patch("samsung_auto_trader.api_client.requests.post") as post:
                summary = trader.format_holding_korean(holding, current_price=75000)
        get.assert_not_called()
        post.assert_not_called()
        self.assertIn("보유수량: 4주", summary)
        self.assertIn("매입평균가: 71,000원", summary)

    def test_cancelled_order_with_cancel_confirmed_quantity_is_summarized_as_cancelled(self):
        trader = SamsungTrader(dry_run=True, paper_trading=True, quantity=1, client=DummyClient())
        row = {
            "odno": "S001",
            "ord_qty": "1",
            "ord_unpr": "98500",
            "tot_ccld_qty": "0",
            "avg_prvs": "0",
            "rmn_qty": "0",
            "rjct_qty": "0",
            "cncl_yn": "N",
            "cncl_cfrm_qty": "1",
            "sll_buy_dvsn_cd_name": "매도",
            "pdno": "005930",
        }
        with patch("samsung_auto_trader.api_client.requests.get") as get:
            with patch("samsung_auto_trader.api_client.requests.post") as post:
                summary = trader.format_order_row_korean(row)
        get.assert_not_called()
        post.assert_not_called()
        self.assertIn("상태: 취소", summary)
        self.assertIn("미체결수량: 0주", summary)

    def test_account_snapshot_korean_summary_uses_holding_fields_without_external_requests(self):
        trader = SamsungTrader(dry_run=True, paper_trading=True, quantity=1, client=DummyClient())
        snapshot = {
            "deposit_total": 1000000,
            "next_day_settlement_amount": 200000,
            "provisional_settlement_amount": 300000,
            "holdings": [
                {
                    "pdno": "005930",
                    "hldg_qty": "5",
                    "ord_psbl_qty": "4",
                    "pchs_avg_pric": "70000",
                    "pchs_amt": "350000",
                    "evlu_amt": "360000",
                    "evlu_pfls_amt": "10000",
                    "evlu_pfls_rt": "2.86",
                }
            ],
        }
        with patch("samsung_auto_trader.api_client.requests.get") as get:
            with patch("samsung_auto_trader.api_client.requests.post") as post:
                summary = trader.format_account_snapshot_korean(snapshot, current_price=72000)
        get.assert_not_called()
        post.assert_not_called()
        self.assertIn("[계좌 요약]", summary)
        self.assertIn("보유수량: 5주", summary)
        self.assertIn("매입평균가: 70,000원", summary)

    def test_no_sensitive_logs_and_csv_on_error(self):
        """Ensure logs do not contain CANO/account/appkey/appsecret/token/authorization or query strings."""
        # inject sensitive-looking values into environment variables
        env_updates = {
            "GH_ACCOUNT": "CANO12345",
            "GH_APPKEY": "APPKEY_SECRET",
            "GH_APPSECRET": "APPSECRET_SECRET",
        }
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
        with patch.dict(os.environ, env_updates, clear=False):
            with TemporaryDirectory() as temp_outputs:
                original_outputs_dir = app_config.outputs_dir
                object.__setattr__(app_config, "outputs_dir", temp_outputs)
                try:
                    try:
                        trader._run_inspect()
                    finally:
                        logger.removeHandler(handler)

                    logs = stream.getvalue()
                    self.assertNotIn("CANO=", logs)
                    self.assertNotIn("CANO12345", logs)
                    self.assertNotIn("APPKEY_SECRET", logs)
                    self.assertNotIn("APPSECRET_SECRET", logs)
                    self.assertNotIn("http://", logs)
                    self.assertNotIn("https://", logs)
                    self.assertNotIn("authorization", logs)
                finally:
                    object.__setattr__(app_config, "outputs_dir", original_outputs_dir)

        # The test should not write into the repository outputs directory.


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None):
        self.status_code = status_code
        self.payload = payload if payload is not None else {"rt_cd": "0"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class TestKISClient(unittest.TestCase):
    def setUp(self) -> None:
        reset_kis_rate_limiter()

    def _client_without_auth(self) -> KISClient:
        client = KISClient.__new__(KISClient)
        client.token = "test-token"
        return client

    def test_request_throttling(self):
        client = self._client_without_auth()
        with patch.dict(os.environ, {"KIS_MIN_REQUEST_INTERVAL_SECONDS": "1.2"}, clear=False):
            with patch("samsung_auto_trader.kis_rate_limit.time.monotonic", side_effect=[100.0, 100.5, 101.2]):
                with patch("samsung_auto_trader.kis_rate_limit.time.sleep") as sleep:
                    with patch("samsung_auto_trader.api_client.requests.get", return_value=FakeResponse()) as get:
                        client._request("GET", "/first")
                        client._request("GET", "/second")

        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.7)

    def test_buy_order_payload_matches_official_cash_order_shape(self):
        client = self._client_without_auth()
        with patch.dict(os.environ, {"KIS_MIN_REQUEST_INTERVAL_SECONDS": "0"}, clear=False):
            with patch("samsung_auto_trader.api_client.requests.post", return_value=FakeResponse()) as post:
                client.place_order("buy", "005930", 1, 70000)

        payload = post.call_args.kwargs["json"]
        self.assertEqual(
            payload,
            {
                "CANO": app_config.gh_account,
                "ACNT_PRDT_CD": app_config.gh_product_code,
                "PDNO": "005930",
                "ORD_DVSN": "00",
                "ORD_QTY": "1",
                "ORD_UNPR": "70000",
                "EXCG_ID_DVSN_CD": "KRX",
                "SLL_TYPE": "",
                "CNDT_PRIC": "",
            },
        )
        self.assertEqual(post.call_count, 1)

    def test_sell_order_payload_matches_official_cash_order_shape(self):
        client = self._client_without_auth()
        with patch.dict(os.environ, {"KIS_MIN_REQUEST_INTERVAL_SECONDS": "0"}, clear=False):
            with patch("samsung_auto_trader.api_client.requests.post", return_value=FakeResponse()) as post:
                client.place_order("sell", "005930", 1, 70000)

        payload = post.call_args.kwargs["json"]
        self.assertEqual(
            payload,
            {
                "CANO": app_config.gh_account,
                "ACNT_PRDT_CD": app_config.gh_product_code,
                "PDNO": "005930",
                "ORD_DVSN": "00",
                "ORD_QTY": "1",
                "ORD_UNPR": "70000",
                "EXCG_ID_DVSN_CD": "KRX",
                "SLL_TYPE": "01",
                "CNDT_PRIC": "",
            },
        )
        self.assertEqual(post.call_count, 1)

    def test_get_buying_power_uses_inquire_psbl_order_and_parses_output(self):
        client = self._client_without_auth()
        response = FakeResponse(
            payload={"rt_cd": "0", "output": {"nrcvb_buy_amt": "123000", "nrcvb_buy_qty": "3"}},
        )
        with patch.dict(os.environ, {"KIS_MIN_REQUEST_INTERVAL_SECONDS": "0"}, clear=False):
            with patch("samsung_auto_trader.api_client.requests.get", return_value=response) as get:
                buying_power = client.get_buying_power("005930", 70000)

        self.assertEqual(buying_power, {"buying_power_amount": 123000, "buying_power_quantity": 3})
        self.assertEqual(get.call_count, 1)
        self.assertTrue(get.call_args.args[0].endswith("/uapi/domestic-stock/v1/trading/inquire-psbl-order"))
        self.assertEqual(
            get.call_args.kwargs["params"],
            {
                "CANO": app_config.gh_account,
                "ACNT_PRDT_CD": app_config.gh_product_code,
                "PDNO": "005930",
                "ORD_UNPR": "70000",
                "ORD_DVSN": "00",
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            },
        )
        self.assertEqual(get.call_args.kwargs["headers"]["tr_id"], "VTTC8908R")

    def test_get_order_status_matches_order_number_and_parses_daily_fill_row(self):
        client = self._client_without_auth()
        response = FakeResponse(
            payload={
                "rt_cd": "0",
                "output1": [
                    {
                        "odno": "OTHER",
                        "ord_qty": "1",
                        "ord_unpr": "70000",
                        "tot_ccld_qty": "0",
                        "avg_prvs": "0",
                        "rmn_qty": "1",
                        "rjct_qty": "0",
                        "cncl_yn": "N",
                        "sll_buy_dvsn_cd_name": "매수",
                        "pdno": "005930",
                    },
                    {
                        "odno": "B001",
                        "ord_qty": "2",
                        "ord_unpr": "98000",
                        "tot_ccld_qty": "1",
                        "avg_prvs": "97900",
                        "rmn_qty": "1",
                        "rjct_qty": "0",
                        "cncl_yn": "N",
                        "sll_buy_dvsn_cd_name": "매수",
                        "pdno": "005930",
                    },
                ],
            },
        )
        with patch.dict(os.environ, {"KIS_MIN_REQUEST_INTERVAL_SECONDS": "0"}, clear=False):
            with patch("samsung_auto_trader.api_client.requests.get", return_value=response) as get:
                status = client.get_order_status("B001")

        self.assertEqual(get.call_count, 1)
        self.assertTrue(get.call_args.args[0].endswith("/uapi/domestic-stock/v1/trading/inquire-daily-ccld"))
        self.assertEqual(status.order_number, "B001")
        self.assertEqual(status.side, "buy")
        self.assertEqual(status.symbol, "005930")
        self.assertEqual(status.ordered_quantity, 2)
        self.assertEqual(status.filled_quantity, 1)
        self.assertEqual(status.remaining_quantity, 1)
        self.assertEqual(status.order_price, 98000)
        self.assertEqual(status.average_fill_price, 97900)
        self.assertEqual(status.rejected_quantity, 0)
        self.assertFalse(status.cancelled)
        self.assertEqual(status.status, "PARTIALLY_FILLED")

    def test_parse_order_status_treats_cancel_confirmed_zero_remaining_as_cancelled(self):
        client = self._client_without_auth()
        row = {
            "odno": "S001",
            "ord_qty": "1",
            "ord_unpr": "98500",
            "tot_ccld_qty": "0",
            "avg_prvs": "0",
            "rmn_qty": "0",
            "rjct_qty": "0",
            "cncl_yn": "N",
            "cncl_cfrm_qty": "1",
            "sll_buy_dvsn_cd_name": "매도",
            "pdno": "005930",
        }
        status = client._parse_order_status(row)
        self.assertTrue(status.cancelled)
        self.assertEqual(status.cancel_confirmed_quantity, 1)
        self.assertEqual(status.remaining_quantity, 0)
        self.assertEqual(status.status, "CANCELLED")

    def test_parse_order_status_treats_cancel_side_name_as_cancelled(self):
        client = self._client_without_auth()
        row = {
            "odno": "C001",
            "ord_qty": "1",
            "ord_unpr": "98500",
            "tot_ccld_qty": "0",
            "avg_prvs": "0",
            "rmn_qty": "0",
            "rjct_qty": "0",
            "cncl_yn": "N",
            "cncl_cfrm_qty": "0",
            "sll_buy_dvsn_cd_name": "매도취소",
            "pdno": "005930",
        }
        status = client._parse_order_status(row)
        self.assertTrue(status.cancelled)
        self.assertEqual(status.status, "CANCELLED")

    def test_place_order_http_500_error_is_sanitized(self):
        client = self._client_without_auth()
        response = FakeResponse(
            status_code=500,
            payload={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "Too many requests"},
        )
        env_updates = {
            "GH_ACCOUNT": "CANO12345",
            "GH_APPKEY": "APPKEY_SECRET",
            "GH_APPSECRET": "APPSECRET_SECRET",
            "KIS_MIN_REQUEST_INTERVAL_SECONDS": "0",
        }
        with patch.dict(os.environ, env_updates, clear=False):
            with patch("samsung_auto_trader.api_client.requests.post", return_value=response) as post:
                with self.assertLogs("samsung_auto_trader", level="ERROR") as captured:
                    with self.assertRaises(requests.HTTPError) as raised:
                        client.place_order("buy", "005930", 1, 70000)

        combined = "\n".join(captured.output + [str(raised.exception)])
        self.assertIn("status=500", combined)
        self.assertIn("msg_cd=EGW00201", combined)
        self.assertIn("msg1=Too many requests", combined)
        self.assertNotIn("CANO12345", combined)
        self.assertNotIn("APPKEY_SECRET", combined)
        self.assertNotIn("APPSECRET_SECRET", combined)
        self.assertNotIn("test-token", combined)
        self.assertNotIn("http://", combined)
        self.assertNotIn("https://", combined)
        self.assertEqual(post.call_count, 1)

    def test_place_order_nonzero_rt_cd_raises_sanitized_runtime_error(self):
        client = self._client_without_auth()
        response = FakeResponse(
            status_code=200,
            payload={"rt_cd": "1", "msg_cd": "ORD001", "msg1": "Rejected"},
        )
        with patch.dict(os.environ, {"KIS_MIN_REQUEST_INTERVAL_SECONDS": "0"}, clear=False):
            with patch("samsung_auto_trader.api_client.requests.post", return_value=response) as post:
                with self.assertRaises(RuntimeError) as raised:
                    client.place_order("sell", "005930", 1, 70000)

        message = str(raised.exception)
        self.assertIn("rt_cd=1", message)
        self.assertIn("msg_cd=ORD001", message)
        self.assertIn("msg1=Rejected", message)
        self.assertNotIn("http://", message)
        self.assertNotIn("https://", message)
        self.assertEqual(post.call_count, 1)

    def test_kis_client_tests_do_not_make_external_requests(self):
        client = self._client_without_auth()
        with patch.dict(os.environ, {"KIS_MIN_REQUEST_INTERVAL_SECONDS": "0"}, clear=False):
            with patch("samsung_auto_trader.api_client.requests.get", return_value=FakeResponse()) as get:
                with patch("samsung_auto_trader.api_client.requests.post", return_value=FakeResponse()) as post:
                    client._request("GET", "/mock-get")
                    client.place_order("buy", "005930", 1, 70000)

        self.assertEqual(get.call_count, 1)
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
