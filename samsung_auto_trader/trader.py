import argparse
import csv
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from samsung_auto_trader.account import AccountService
from samsung_auto_trader.api_client import KISClient
from samsung_auto_trader.config import config
from samsung_auto_trader.logger import logger
from samsung_auto_trader.market_data import MarketDataService
from samsung_auto_trader.orders import OrderService


class SamsungTrader:
    def __init__(
        self,
        offset: int | None = None,
        dry_run: bool | None = None,
        paper_trading: bool | None = None,
        quantity: int | None = None,
        buy_only: bool = False,
        sell_only: bool = False,
        show_orders: bool = False,
        report: bool = False,
        inspect: bool = False,
        client: KISClient | None = None,
        market_data: MarketDataService | None = None,
        account_service: AccountService | None = None,
        order_service: OrderService | None = None,
    ) -> None:
        self.offset = offset if offset is not None else config.order_offset_krw
        self.dry_run = config.dry_run if dry_run is None else dry_run
        self.paper_trading = config.paper_trading if paper_trading is None else paper_trading
        self.requested_quantity = quantity if quantity is not None else config.default_order_quantity
        self.buy_only = buy_only
        self.sell_only = sell_only
        self.show_orders = show_orders
        self.report = report
        self.inspect = inspect

        self.client = client if client is not None else KISClient()
        self.market_data = market_data if market_data is not None else MarketDataService(self.client)
        self.account_service = account_service if account_service is not None else AccountService(self.client)
        self.order_service = order_service if order_service is not None else OrderService(self.client, paper_trading=self.paper_trading)

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(config.local_timezone))

    def _is_within_trading_window(self) -> bool:
        now = self._now()
        start = datetime.strptime(config.start_time_str, "%H:%M").time()
        end = datetime.strptime(config.end_time_str, "%H:%M").time()
        return start <= now.time() <= end

    def _print_window_status(self) -> None:
        if self._is_within_trading_window():
            logger.info("Trading window is open.")
        else:
            logger.info("Trading window is closed.")

    def _determine_quantity(self, available_cash: int, price: int) -> int:
        if price <= 0:
            return 0

        quantity = max(1, self.requested_quantity)
        capped_quantity = min(quantity, config.max_order_quantity)
        if capped_quantity != quantity:
            logger.warning(
                "Requested quantity %s capped to max_order_quantity=%s.",
                quantity,
                config.max_order_quantity,
            )

        affordable_quantity = available_cash // price
        if affordable_quantity <= 0:
            return 0

        final_quantity = min(capped_quantity, affordable_quantity)
        if final_quantity != capped_quantity:
            logger.warning(
                "Order quantity reduced to affordable amount %s based on available cash.",
                final_quantity,
            )
        return final_quantity

    def _holding_quantity(self, holding: dict[str, Any]) -> int:
        for key in ["hldg_qty", "hldg_qty1", "ord_psbl_qty", "qty", "pdqty", "ord_qty"]:
            value = holding.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return 0

    def _format_sanitized_order_row(self, row: dict[str, Any]) -> dict[str, Any]:
        sanitized = {}
        for key, value in row.items():
            if key.upper().startswith(("CANO", "ACNT", "APPKEY", "APPSECRET", "TOKEN", "PWD", "PASSWORD")):
                continue
            sanitized[key] = value
        return sanitized

    def _extract_orders(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if not payload:
            return []
        rows = []
        output1 = payload.get("output1")
        if isinstance(output1, dict):
            rows = [output1]
        elif isinstance(output1, list):
            rows = output1
        elif isinstance(payload.get("output"), list):
            rows = payload["output"]
        elif isinstance(payload.get("output"), dict):
            rows = [payload["output"]]

        sanitized_rows = [self._format_sanitized_order_row(row) for row in rows]
        return sanitized_rows

    def _write_report(self, account_snapshot: dict[str, Any], current_price: int | None, orders: list[dict[str, Any]]) -> None:
        outputs_dir = Path(config.outputs_dir)
        outputs_dir.mkdir(parents=True, exist_ok=True)

        report_path = outputs_dir / "execution_report.md"
        orders_path = outputs_dir / "recent_orders.csv"
        summary_path = outputs_dir / "account_summary.svg"

        with report_path.open("w", encoding="utf-8") as report_file:
            report_file.write("# Samsung Auto Trader Execution Report\n\n")
            report_file.write(f"- Date: {self._now().isoformat()}\n")
            report_file.write(f"- Dry run: {self.dry_run}\n")
            report_file.write(f"- Paper trading: {self.paper_trading}\n")
            report_file.write(f"- Token source: {self.client.auth.token_source}\n")
            report_file.write(f"- Current price: {current_price or 'n/a'}\n")
            available_cash = account_snapshot.get('available_cash')
            report_file.write(f"- Available cash: {available_cash if available_cash is not None else 'unavailable'}\n")
            holding = self.account_service.find_holding(account_snapshot.get("holdings", []), config.symbol) or {}
            report_file.write(f"- Samsung Electronics holding quantity: {self._holding_quantity(holding)}\n")
            if orders:
                report_file.write(f"- Recent orders count: {len(orders)}\n")
            else:
                report_file.write("- Recent orders: 최근 주문내역 조회 불가\n")

        # Always produce recent_orders.csv. If orders are empty/unavailable, create headers-only CSV.
        if orders:
            fieldnames = sorted({key for order in orders for key in order.keys()})
        else:
            # Use a safe single header when no orders available
            fieldnames = ["info"]

        with orders_path.open("w", encoding="utf-8", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            if orders:
                for order in orders:
                    writer.writerow(order)

        cash_value = account_snapshot.get("available_cash") or 0
        holding = self.account_service.find_holding(account_snapshot.get("holdings", []), config.symbol) or {}
        holding_qty = self._holding_quantity(holding)
        max_cash = max(1, cash_value)
        bar_cash = int(min(300, cash_value / max_cash * 300)) if cash_value else 0
        bar_holding = int(min(300, holding_qty / max(1, holding_qty) * 300)) if holding_qty else 0

        svg_template = f"""<?xml version='1.0' encoding='UTF-8'?>
<svg width='450' height='140' xmlns='http://www.w3.org/2000/svg'>
  <rect width='450' height='140' fill='#f8f9fa' />
  <text x='20' y='30' font-size='14' fill='#212529'>Samsung Auto Trader Summary</text>
  <text x='20' y='55' font-size='12' fill='#495057'>Price: {current_price or 'n/a'} KRW</text>
  <text x='20' y='72' font-size='12' fill='#495057'>Available cash: {cash_value}</text>
  <text x='20' y='89' font-size='12' fill='#495057'>Holding qty: {holding_qty}</text>
  <rect x='20' y='100' width='{bar_cash}' height='16' fill='#0d6efd' />
  <text x='20' y='132' font-size='11' fill='#ffffff'>Cash bar</text>
  <rect x='200' y='100' width='{bar_holding}' height='16' fill='#198754' />
  <text x='200' y='132' font-size='11' fill='#ffffff'>Holding bar</text>
</svg>
"""
        summary_path.write_text(svg_template, encoding="utf-8")
        logger.info("Report written to outputs directory")

    def _show_recent_orders(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        orders = self._extract_orders(payload)
        if not orders:
            logger.info("No recent orders found.")
            return []

        logger.info("Recent order history (%s rows):", len(orders))
        for row in orders:
            logger.info("  %s", {k: v for k, v in row.items()})
        return orders

    def _record_execution(self, before_snapshot: dict[str, Any], after_snapshot: dict[str, Any]) -> bool:
        if not before_snapshot or not after_snapshot:
            return False

        before_cash = before_snapshot.get("available_cash") or 0
        after_cash = after_snapshot.get("available_cash") or 0
        if before_cash != after_cash:
            return True

        before_holding = self.account_service.find_holding(before_snapshot.get("holdings", []), config.symbol) or {}
        after_holding = self.account_service.find_holding(after_snapshot.get("holdings", []), config.symbol) or {}
        before_qty = self._holding_quantity(before_holding)
        after_qty = self._holding_quantity(after_holding)
        return before_qty != after_qty

    def _get_recent_orders_safe(self) -> list[dict[str, Any]]:
        """Retrieve recent orders with graceful error handling.
        
        Returns empty list if endpoint is unavailable (e.g., non-trading day).
        """
        try:
            orders_payload = self.client.get_recent_daily_orders(days=1)
            return self._extract_orders(orders_payload)
        except Exception as exc:
            logger.warning("Recent order history retrieval failed: %s (continuing with empty order list)", type(exc).__name__)
            return []

    def _run_inspect(self) -> None:
        logger.info("Inspect mode: read-only account and order status.")
        
        try:
            current_price = self.market_data.get_current_price(config.symbol)
        except Exception as exc:
            logger.warning("Current price retrieval failed: %s (continuing with null price)", type(exc).__name__)
            current_price = None
        
        try:
            snapshot = self.account_service.get_account_snapshot()
        except Exception as exc:
            logger.warning("Account snapshot retrieval failed: %s (continuing with unavailable snapshot)", type(exc).__name__)
            snapshot = {"available_cash": 0, "holdings": []}
        
        orders = self._get_recent_orders_safe()
        if self.show_orders and orders:
            logger.info("Recent order history (%s rows):", len(orders))
            for row in orders:
                logger.info("  %s", {k: v for k, v in row.items()})
        
        logger.info("Token reuse source: %s", self.client.auth.token_source)
        logger.info("Current price: %s", current_price)
        logger.info("Account snapshot: available_cash=%s holdings=%s", snapshot.get("available_cash"), snapshot.get("holdings"))

        if self.report:
            self._write_report(snapshot, current_price, orders)

    def _run_trade_cycle(self) -> None:
        current_price = self.market_data.get_current_price(config.symbol)
        if current_price is None:
            logger.error("Could not read current price. Aborting trade cycle.")
            return

        before_snapshot = self.account_service.get_account_snapshot()
        holding = self.account_service.find_holding(before_snapshot["holdings"], config.symbol) or {}
        samsung_qty = self._holding_quantity(holding)
        logger.info("Current Samsung holdings qty: %s", samsung_qty)

        order_price_buy = current_price - self.offset
        order_price_sell = current_price + self.offset
        quantity = self._determine_quantity(before_snapshot["available_cash"], order_price_buy)

        if self.buy_only:
            if quantity <= 0:
                logger.info("Buy-only mode: not enough available cash to place a buy order at %s KRW.", order_price_buy)
            elif self.dry_run:
                logger.info("DRY_RUN enabled, skipping buy-only order. buy_price=%s qty=%s", order_price_buy, quantity)
            else:
                self.order_service.place_buy_order(config.symbol, quantity, order_price_buy)
        elif self.sell_only:
            if samsung_qty < self.requested_quantity:
                logger.info(
                    "Sell-only mode: skipping sell order because Samsung holdings %s < requested sell quantity %s.",
                    samsung_qty,
                    self.requested_quantity,
                )
            elif self.dry_run:
                logger.info("DRY_RUN enabled, skipping sell-only order. sell_price=%s qty=%s", order_price_sell, self.requested_quantity)
            else:
                self.order_service.place_sell_order(config.symbol, self.requested_quantity, order_price_sell)
        else:
            if quantity <= 0:
                logger.info("Not enough available cash to place a buy order at %s KRW.", order_price_buy)
            elif self.dry_run:
                logger.info("DRY_RUN enabled, skipping buy order. buy_price=%s qty=%s", order_price_buy, quantity)
            else:
                self.order_service.place_buy_order(config.symbol, quantity, order_price_buy)

            if samsung_qty < self.requested_quantity:
                logger.info(
                    "Skipping sell order because Samsung holdings %s < requested sell quantity %s.",
                    samsung_qty,
                    self.requested_quantity,
                )
            elif self.dry_run:
                logger.info("DRY_RUN enabled, skipping sell order. sell_price=%s qty=%s", order_price_sell, self.requested_quantity)
            else:
                self.order_service.place_sell_order(config.symbol, self.requested_quantity, order_price_sell)

        if self.show_orders or self.report:
            orders = self._get_recent_orders_safe()
            if self.show_orders and orders:
                logger.info("Recent order history (%s rows):", len(orders))
                for row in orders:
                    logger.info("  %s", {k: v for k, v in row.items()})
        else:
            orders = []

        if self.report:
            self._write_report(before_snapshot, current_price, orders)

        time.sleep(config.polling_interval_seconds)
        after_snapshot = self.account_service.get_account_snapshot()
        logger.info("Holdings after order: %s", after_snapshot["holdings"])
        execution_happened = self._record_execution(before_snapshot, after_snapshot)
        logger.info("Execution observed from snapshots: %s", execution_happened)

    def run_cycle(self, once: bool = False) -> None:
        logger.info(
            "Starting Samsung Electronics auto trader. dry_run=%s paper_trading=%s offset=%s quantity=%s buy_only=%s sell_only=%s show_orders=%s report=%s inspect=%s",
            self.dry_run,
            self.paper_trading,
            self.offset,
            self.requested_quantity,
            self.buy_only,
            self.sell_only,
            self.show_orders,
            self.report,
            self.inspect,
        )

        if not self.paper_trading:
            logger.error("Real trading is disabled. paper_trading must remain enabled.")
            return

        if self.inspect:
            self._run_inspect()
            return

        self._print_window_status()
        if not self._is_within_trading_window():
            logger.info("Exiting because current time is outside the trading window.")
            return

        if once:
            self._run_trade_cycle()
            logger.info("One-cycle mode complete. Exiting.")
            return

        while True:
            self._run_trade_cycle()
            if not self._is_within_trading_window():
                logger.info("Trading window ended. Stopping trader.")
                break
            logger.info("Waiting for next check after %s seconds.", config.polling_interval_seconds)
            time.sleep(config.polling_interval_seconds)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Samsung Electronics auto trader for mock KIS REST API.")
    parser.add_argument("--once", action="store_true", help="Run a single polling cycle and exit.")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="Enable dry run mode without sending orders.")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="Disable dry run mode.")
    parser.add_argument("--confirm-paper-order", action="store_true", help="Confirm that paper order submission is intentional when disabling dry run.")
    parser.add_argument("--paper-trading", dest="paper_trading", action="store_true", help="Enable paper trading mode.")
    parser.add_argument("--no-paper-trading", dest="paper_trading", action="store_false", help="Disable paper trading mode (not supported).")
    parser.add_argument("--offset", type=int, help="ORDER_OFFSET_KRW to use for buy/sell levels.")
    parser.add_argument("--quantity", type=int, default=config.default_order_quantity, help="Order quantity for buy/sell actions.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--buy-only", action="store_true", help="Only place buy orders.")
    group.add_argument("--sell-only", action="store_true", help="Only place sell orders.")
    parser.add_argument("--show-orders", action="store_true", help="Show recent order history.")
    parser.add_argument("--report", action="store_true", help="Create sanitized report outputs without exposing secrets.")
    parser.add_argument("--inspect", action="store_true", help="Inspect read-only account and order history without placing orders.")
    parser.set_defaults(dry_run=True, paper_trading=True)
    return parser


def run_from_cli(argv: list[str] | None = None) -> None:
    parser = create_parser()
    args = parser.parse_args(argv)

    if args.dry_run is False and not args.confirm_paper_order:
        parser.error("--no-dry-run must be accompanied by --confirm-paper-order to allow paper order submission.")
    if args.paper_trading is False:
        parser.error("Real trading is disabled. Use paper trading only.")

    trader = SamsungTrader(
        offset=args.offset,
        dry_run=args.dry_run,
        paper_trading=args.paper_trading,
        quantity=args.quantity,
        buy_only=args.buy_only,
        sell_only=args.sell_only,
        show_orders=args.show_orders,
        report=args.report,
        inspect=args.inspect,
    )
    trader.run_cycle(once=args.once)


if __name__ == "__main__":
    run_from_cli()
