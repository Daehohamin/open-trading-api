import argparse
import csv
import sys
import time
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from samsung_auto_trader.account import AccountService
from samsung_auto_trader.api_client import KISClient, OrderStatus
from samsung_auto_trader.config import config
from samsung_auto_trader.logger import logger
from samsung_auto_trader.market_data import MarketDataService
from samsung_auto_trader.orders import OrderService
from samsung_auto_trader.price_utils import get_tick_size, normalize_order_price


@dataclass
class OrderWaitResult:
    order_status: OrderStatus
    timed_out: bool = False


@dataclass
class HoldingWaitResult:
    quantity: int
    timed_out: bool = False


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
        auto_cycle: bool = False,
        buy_offset: int | None = None,
        take_profit: int | None = None,
        cycle_count: int | None = None,
        order_status_timeout: float = 120.0,
        order_status_poll_interval: float = 5.0,
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
        self.auto_cycle = auto_cycle
        self.buy_offset = buy_offset
        self.take_profit = take_profit
        self.cycle_count = cycle_count
        self.order_status_timeout = order_status_timeout
        self.order_status_poll_interval = order_status_poll_interval

        self.client = client if client is not None else KISClient()
        self.market_data = market_data if market_data is not None else MarketDataService(self.client)
        self.account_service = account_service if account_service is not None else AccountService(self.client)
        self.order_service = order_service if order_service is not None else OrderService(self.client, paper_trading=self.paper_trading)

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(config.local_timezone))

    def _is_within_trading_window(self) -> bool:
        now = self._now()
        if now.weekday() >= 5:
            return False
        start = datetime.strptime(config.start_time_str, "%H:%M").time()
        end = datetime.strptime(config.end_time_str, "%H:%M").time()
        return start <= now.time() <= end

    def _print_window_status(self) -> None:
        if self._is_within_trading_window():
            logger.info("Trading window is open.")
        else:
            logger.info("Trading window is closed.")

    def _determine_quantity(self, buying_power_quantity: int | None) -> int:
        if buying_power_quantity is None or buying_power_quantity <= 0:
            return 0
        quantity = max(1, self.requested_quantity)
        capped_quantity = min(quantity, config.max_order_quantity)
        if capped_quantity != quantity:
            logger.warning(
                "Requested quantity %s capped to max_order_quantity=%s.",
                quantity,
                config.max_order_quantity,
            )

        final_quantity = min(capped_quantity, buying_power_quantity)
        if final_quantity != capped_quantity:
            logger.warning(
                "Order quantity reduced to buying-power quantity %s.",
                final_quantity,
            )
        return final_quantity

    def _get_buying_power_safe(self, symbol: str, order_price: int) -> dict[str, int | None]:
        try:
            buying_power = self.client.get_buying_power(symbol, order_price)
        except Exception as exc:
            logger.warning("Buying power retrieval failed: %s (no balance fallback will be used)", type(exc).__name__)
            return {"buying_power_amount": None, "buying_power_quantity": None}
        logger.info(
            "Buying power: amount=%s quantity=%s",
            buying_power.get("buying_power_amount"),
            buying_power.get("buying_power_quantity"),
        )
        return buying_power

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

    def _parse_int_display(self, value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(float(str(value).replace(",", "")))
        except (TypeError, ValueError):
            return None

    def format_krw(self, value: Any) -> str:
        amount = self._parse_int_display(value)
        if amount is None:
            return "조회불가"
        return f"{amount:,}원"

    def _format_quantity(self, value: Any) -> str:
        quantity = self._parse_int_display(value)
        if quantity is None:
            return "조회불가"
        return f"{quantity:,}주"

    def _format_percent(self, value: Any) -> str:
        if value in (None, ""):
            return "조회불가"
        try:
            percent = float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return "조회불가"
        return f"{percent:.2f}%"

    def _format_symbol_name(self, symbol: str) -> str:
        normalized_symbol = symbol.zfill(6)
        if normalized_symbol == config.symbol:
            return f"삼성전자({normalized_symbol})"
        return normalized_symbol

    def _format_order_side_korean(self, row: dict[str, Any], parsed_status: OrderStatus) -> str:
        side_name = str(row.get("sll_buy_dvsn_cd_name") or "").strip()
        if side_name:
            if "취소" in side_name:
                return side_name
            if "매수" in side_name:
                return "매수"
            if "매도" in side_name:
                return "매도"
        if parsed_status.side == "buy":
            return "매수"
        if parsed_status.side == "sell":
            return "매도"
        return parsed_status.side or "조회불가"

    def _format_order_status_korean(self, status: str) -> str:
        return {
            "FILLED": "전량체결",
            "PENDING": "미체결",
            "CANCELLED": "취소",
            "PARTIALLY_FILLED": "부분체결",
            "REJECTED": "거절",
            "NOT_FOUND": "조회불가",
        }.get(status, status or "조회불가")

    def format_order_row_korean(self, row: dict[str, Any]) -> str:
        parsed_status = self.client._parse_order_status(row)
        lines = [
            "[최근 주문 요약]",
            f"주문번호: {parsed_status.order_number or '조회불가'}",
            f"구분: {self._format_order_side_korean(row, parsed_status)}",
            f"종목: {self._format_symbol_name(parsed_status.symbol or str(row.get('pdno') or ''))}",
            f"주문가: {self.format_krw(parsed_status.order_price)}",
            f"주문수량: {self._format_quantity(parsed_status.ordered_quantity)}",
            f"체결수량: {self._format_quantity(parsed_status.filled_quantity)}",
            f"미체결수량: {self._format_quantity(parsed_status.remaining_quantity)}",
            f"평균체결가: {self.format_krw(parsed_status.average_fill_price)}",
            f"상태: {self._format_order_status_korean(parsed_status.status)}",
        ]
        return "\n".join(lines)

    def format_holding_korean(self, holding: dict[str, Any], current_price: int | None = None) -> str:
        symbol = str(holding.get("pdno") or config.symbol)
        current_price_value = current_price if current_price is not None else holding.get("prpr")
        profit_loss = holding.get("evlu_pfls_amt") or holding.get("evlu_pfls")
        profit_loss_rate = holding.get("evlu_pfls_rt") or holding.get("evlu_erng_rt")
        lines = [
            f"종목: {self._format_symbol_name(symbol)}",
            f"보유수량: {self._format_quantity(holding.get('hldg_qty'))}",
            f"주문가능수량: {self._format_quantity(holding.get('ord_psbl_qty'))}",
            f"매입평균가: {self.format_krw(holding.get('pchs_avg_pric'))}",
            f"매입금액: {self.format_krw(holding.get('pchs_amt'))}",
            f"현재가: {self.format_krw(current_price_value)}",
            f"평가금액: {self.format_krw(holding.get('evlu_amt'))}",
            f"평가손익: {self.format_krw(profit_loss)} ({self._format_percent(profit_loss_rate)})",
        ]
        return "\n".join(lines)

    def format_account_snapshot_korean(self, snapshot: dict[str, Any], current_price: int | None = None) -> str:
        holding = self.account_service.find_holding(snapshot.get("holdings", []), config.symbol) or {}
        lines = [
            "[계좌 요약]",
            f"예수금총액: {self.format_krw(snapshot.get('deposit_total'))}",
            f"익일정산금액: {self.format_krw(snapshot.get('next_day_settlement_amount'))}",
            f"가수도정산금액: {self.format_krw(snapshot.get('provisional_settlement_amount'))}",
            self.format_holding_korean(holding, current_price=current_price),
        ]
        return "\n".join(lines)

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
            report_file.write(f"- Deposit total: {self._format_optional_amount(account_snapshot.get('deposit_total'))}\n")
            report_file.write(
                f"- Next-day settlement amount: {self._format_optional_amount(account_snapshot.get('next_day_settlement_amount'))}\n"
            )
            report_file.write(
                f"- Provisional settlement amount: {self._format_optional_amount(account_snapshot.get('provisional_settlement_amount'))}\n"
            )
            if "buying_power_amount" in account_snapshot or "buying_power_quantity" in account_snapshot:
                report_file.write(f"- Buying power amount: {self._format_optional_amount(account_snapshot.get('buying_power_amount'))}\n")
                report_file.write(f"- Buying power quantity: {self._format_optional_amount(account_snapshot.get('buying_power_quantity'))}\n")
            else:
                report_file.write("- Buying power: not queried\n")
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

        deposit_total = account_snapshot.get("deposit_total") or 0
        holding = self.account_service.find_holding(account_snapshot.get("holdings", []), config.symbol) or {}
        holding_qty = self._holding_quantity(holding)
        max_deposit = max(1, deposit_total)
        bar_deposit = int(min(300, deposit_total / max_deposit * 300)) if deposit_total else 0
        bar_holding = int(min(300, holding_qty / max(1, holding_qty) * 300)) if holding_qty else 0

        svg_template = f"""<?xml version='1.0' encoding='UTF-8'?>
<svg width='450' height='140' xmlns='http://www.w3.org/2000/svg'>
  <rect width='450' height='140' fill='#f8f9fa' />
  <text x='20' y='30' font-size='14' fill='#212529'>Samsung Auto Trader Summary</text>
  <text x='20' y='55' font-size='12' fill='#495057'>Price: {current_price or 'n/a'} KRW</text>
  <text x='20' y='72' font-size='12' fill='#495057'>Deposit total: {deposit_total}</text>
  <text x='20' y='89' font-size='12' fill='#495057'>Holding qty: {holding_qty}</text>
  <rect x='20' y='100' width='{bar_deposit}' height='16' fill='#0d6efd' />
  <text x='20' y='132' font-size='11' fill='#ffffff'>Deposit bar</text>
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
            logger.info("\n%s", self.format_order_row_korean(row))
            logger.debug("Raw recent order row: %s", {k: v for k, v in row.items()})
        return orders

    def _record_execution(self, before_snapshot: dict[str, Any], after_snapshot: dict[str, Any]) -> bool:
        if not before_snapshot or not after_snapshot:
            return False

        before_holding = self.account_service.find_holding(before_snapshot.get("holdings", []), config.symbol) or {}
        after_holding = self.account_service.find_holding(after_snapshot.get("holdings", []), config.symbol) or {}
        before_qty = self._holding_quantity(before_holding)
        after_qty = self._holding_quantity(after_holding)
        return before_qty != after_qty

    def _extract_order_number(self, payload: dict[str, Any]) -> str:
        output = payload.get("output") or {}
        if isinstance(output, list):
            output = output[0] if output else {}
        if not isinstance(output, dict):
            return ""
        return str(output.get("ODNO") or output.get("odno") or output.get("order_no") or "")

    def _account_holding_quantity(self) -> int:
        snapshot = self.account_service.get_account_snapshot()
        holding = self.account_service.find_holding(snapshot.get("holdings", []), config.symbol) or {}
        return self._holding_quantity(holding)

    def wait_for_holding_quantity(
        self,
        expected_quantity: int,
        timeout: float,
        poll_interval: float,
    ) -> HoldingWaitResult:
        started_at = time.monotonic()
        last_quantity: int | None = None

        while True:
            quantity = self._account_holding_quantity()
            if quantity != last_quantity:
                logger.info(
                    "Holding quantity transition: symbol=%s quantity=%s expected=%s",
                    config.symbol,
                    quantity,
                    expected_quantity,
                )
                last_quantity = quantity

            if quantity == expected_quantity:
                return HoldingWaitResult(quantity=quantity, timed_out=False)

            elapsed = time.monotonic() - started_at
            if elapsed >= timeout:
                logger.warning(
                    "Holding quantity confirmation timed out: symbol=%s quantity=%s expected=%s",
                    config.symbol,
                    quantity,
                    expected_quantity,
                )
                return HoldingWaitResult(quantity=quantity, timed_out=True)

            time.sleep(poll_interval)

    def _is_pending_order_row(self, row: dict[str, Any], symbol: str) -> bool:
        status = self.client._parse_order_status(row)
        return (
            status.symbol.zfill(6) == symbol
            and status.remaining_quantity > 0
            and status.status in ("PENDING", "PARTIALLY_FILLED")
        )

    def _has_existing_pending_order(self, orders: list[dict[str, Any]], symbol: str) -> bool:
        for row in orders:
            try:
                if self._is_pending_order_row(row, symbol):
                    return True
            except Exception:
                logger.warning("Could not parse recent order row while checking pending orders.")
        return False

    def wait_for_order_completion(
        self,
        order_number: str,
        expected_quantity: int,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> OrderWaitResult:
        started_at = time.monotonic()
        last_state: str | None = None
        latest_status = OrderStatus(
            order_number=order_number,
            side="",
            symbol="",
            ordered_quantity=expected_quantity,
            filled_quantity=0,
            remaining_quantity=expected_quantity,
            order_price=0,
            average_fill_price=0,
            rejected_quantity=0,
            cancelled=False,
            status="NOT_FOUND",
        )

        while True:
            latest_status = self.client.get_order_status(order_number)
            state = (
                latest_status.status,
                latest_status.filled_quantity,
                latest_status.remaining_quantity,
                latest_status.rejected_quantity,
            )
            state_text = str(state)
            if state_text != last_state:
                logger.info(
                    "Order status transition: order_number=%s status=%s filled=%s remaining=%s rejected=%s",
                    latest_status.order_number,
                    latest_status.status,
                    latest_status.filled_quantity,
                    latest_status.remaining_quantity,
                    latest_status.rejected_quantity,
                )
                last_state = state_text

            if latest_status.status in ("FILLED", "REJECTED", "CANCELLED"):
                return OrderWaitResult(order_status=latest_status, timed_out=False)

            elapsed = time.monotonic() - started_at
            if elapsed >= timeout_seconds:
                logger.warning("Order status polling timed out: order_number=%s status=%s", order_number, latest_status.status)
                return OrderWaitResult(order_status=latest_status, timed_out=True)

            time.sleep(poll_interval_seconds)

    def _log_auto_stopped(self, reason: str) -> None:
        logger.info("AUTO_STATE=STOPPED reason=%s", reason)

    def _run_auto_cycle_once(self) -> bool:
        logger.info("AUTO_STATE=PRECHECK")
        if not self._is_within_trading_window():
            self._log_auto_stopped("trading_window_closed")
            return False

        before_snapshot = self.account_service.get_account_snapshot()
        holding = self.account_service.find_holding(before_snapshot.get("holdings", []), config.symbol) or {}
        baseline_quantity = self._holding_quantity(holding)
        orders = self._get_recent_orders_safe()
        if self._has_existing_pending_order(orders, config.symbol):
            self._log_auto_stopped("existing_pending_order")
            return False

        current_price = self.market_data.get_current_price(config.symbol)
        if current_price is None:
            self._log_auto_stopped("price_unavailable")
            return False
        effective_buy_offset = self.buy_offset if self.buy_offset is not None else self.offset
        raw_buy_price = current_price - effective_buy_offset
        buy_price = normalize_order_price(raw_buy_price, "buy")
        if buy_price != raw_buy_price:
            logger.info(
                "Order price adjusted to KRX tick: side=buy raw=%s normalized=%s tick=%s",
                raw_buy_price,
                buy_price,
                get_tick_size(raw_buy_price),
            )
        buying_power = self._get_buying_power_safe(config.symbol, buy_price)
        quantity = self._determine_quantity(buying_power.get("buying_power_quantity"))
        if quantity <= 0:
            self._log_auto_stopped("buying_power_unavailable")
            return False
        if self.dry_run:
            logger.info("DRY_RUN enabled, skipping auto-cycle buy order. buy_price=%s qty=%s", buy_price, quantity)
            self._log_auto_stopped("dry_run")
            return False

        buy_payload = self.order_service.place_buy_order(config.symbol, quantity, buy_price)
        buy_order_number = self._extract_order_number(buy_payload)
        if not buy_order_number:
            self._log_auto_stopped("missing_buy_order_number")
            return False
        logger.info("AUTO_STATE=BUY_SUBMITTED order_number=%s qty=%s price=%s", buy_order_number, quantity, buy_price)
        logger.info("AUTO_STATE=BUY_PENDING order_number=%s", buy_order_number)
        buy_wait = self.wait_for_order_completion(
            buy_order_number,
            quantity,
            self.order_status_timeout,
            self.order_status_poll_interval,
        )
        buy_status = buy_wait.order_status
        if buy_wait.timed_out:
            self._log_auto_stopped("buy_timeout")
            self._print_auto_summary(buy_status, None, baseline_quantity, self._account_holding_quantity())
            return False
        if buy_status.status != "FILLED":
            self._log_auto_stopped(f"buy_{buy_status.status.lower()}")
            self._print_auto_summary(buy_status, None, baseline_quantity, self._account_holding_quantity())
            return False
        if buy_status.filled_quantity < quantity or buy_status.remaining_quantity > 0:
            self._log_auto_stopped("buy_not_fully_filled")
            self._print_auto_summary(buy_status, None, baseline_quantity, self._account_holding_quantity())
            return False
        logger.info("AUTO_STATE=BUY_FILLED order_number=%s", buy_order_number)

        expected_after_buy = baseline_quantity + quantity
        after_buy_wait = self.wait_for_holding_quantity(
            expected_after_buy,
            self.order_status_timeout,
            self.order_status_poll_interval,
        )
        after_buy_quantity = after_buy_wait.quantity
        if after_buy_wait.timed_out:
            self._log_auto_stopped("buy_holding_not_confirmed")
            self._print_auto_summary(buy_status, None, baseline_quantity, after_buy_quantity)
            return False

        if self.take_profit is None:
            latest_price = self.market_data.get_current_price(config.symbol)
            if latest_price is None:
                self._log_auto_stopped("sell_price_unavailable")
                self._print_auto_summary(buy_status, None, baseline_quantity, after_buy_quantity)
                return False
            raw_sell_price = latest_price + self.offset
        else:
            buy_average_fill_price = buy_status.average_fill_price or buy_price
            raw_sell_price = buy_average_fill_price + self.take_profit
            sell_price_for_log = normalize_order_price(raw_sell_price, "sell")
            logger.info(
                "Take-profit sell target: buy_average_fill_price=%s take_profit=%s raw_sell_price=%s normalized_sell_price=%s",
                buy_average_fill_price,
                self.take_profit,
                raw_sell_price,
                sell_price_for_log,
            )
        sell_price = normalize_order_price(raw_sell_price, "sell")
        if sell_price != raw_sell_price:
            logger.info(
                "Order price adjusted to KRX tick: side=sell raw=%s normalized=%s tick=%s",
                raw_sell_price,
                sell_price,
                get_tick_size(raw_sell_price),
            )
        sell_quantity = buy_status.filled_quantity or quantity
        sell_payload = self.order_service.place_sell_order(config.symbol, sell_quantity, sell_price)
        sell_order_number = self._extract_order_number(sell_payload)
        if not sell_order_number:
            self._log_auto_stopped("missing_sell_order_number")
            self._print_auto_summary(buy_status, None, baseline_quantity, after_buy_quantity)
            return False
        logger.info("AUTO_STATE=SELL_SUBMITTED order_number=%s qty=%s price=%s", sell_order_number, sell_quantity, sell_price)
        logger.info("AUTO_STATE=SELL_PENDING order_number=%s", sell_order_number)
        sell_wait = self.wait_for_order_completion(
            sell_order_number,
            sell_quantity,
            self.order_status_timeout,
            self.order_status_poll_interval,
        )
        sell_status = sell_wait.order_status
        if sell_wait.timed_out:
            self._log_auto_stopped("sell_timeout")
            self._print_auto_summary(buy_status, sell_status, baseline_quantity, self._account_holding_quantity())
            return False
        if sell_status.status != "FILLED":
            self._log_auto_stopped(f"sell_{sell_status.status.lower()}")
            self._print_auto_summary(buy_status, sell_status, baseline_quantity, self._account_holding_quantity())
            return False
        logger.info("AUTO_STATE=SELL_FILLED order_number=%s", sell_order_number)

        final_wait = self.wait_for_holding_quantity(
            baseline_quantity,
            self.order_status_timeout,
            self.order_status_poll_interval,
        )
        final_quantity = final_wait.quantity
        if final_wait.timed_out:
            self._log_auto_stopped("final_holding_not_confirmed")
            self._print_auto_summary(buy_status, sell_status, baseline_quantity, final_quantity)
            return False

        logger.info("AUTO_STATE=COMPLETE")
        self._print_auto_summary(buy_status, sell_status, baseline_quantity, final_quantity)
        return True

    def _print_auto_summary(
        self,
        buy_status: OrderStatus | None,
        sell_status: OrderStatus | None,
        starting_holdings: int,
        final_holdings: int,
    ) -> None:
        logger.info(
            "AUTO_SUMMARY buy_order_number=%s buy_fill_status=%s buy_filled_quantity=%s buy_average_fill_price=%s "
            "sell_order_number=%s sell_fill_status=%s sell_filled_quantity=%s sell_average_fill_price=%s "
            "starting_holdings=%s final_holdings=%s",
            buy_status.order_number if buy_status else "",
            buy_status.status if buy_status else "",
            buy_status.filled_quantity if buy_status else 0,
            buy_status.average_fill_price if buy_status else 0,
            sell_status.order_number if sell_status else "",
            sell_status.status if sell_status else "",
            sell_status.filled_quantity if sell_status else 0,
            sell_status.average_fill_price if sell_status else 0,
            starting_holdings,
            final_holdings,
        )

    def _run_auto_cycle(self, once: bool = False) -> None:
        if self.buy_only or self.sell_only:
            if not self._is_within_trading_window():
                self._log_auto_stopped("trading_window_closed")
                return
            self._run_trade_cycle()
            return
        if once:
            self._run_auto_cycle_once()
            return
        if not self._is_within_trading_window():
            self._run_auto_cycle_once()
            return
        completed_cycles = 0
        while self._is_within_trading_window():
            completed = self._run_auto_cycle_once()
            if not completed:
                break
            completed_cycles += 1
            if self.cycle_count is not None and completed_cycles >= self.cycle_count:
                logger.info("AUTO_STATE=COMPLETE cycle_count=%s", completed_cycles)
                break
            if not self._is_within_trading_window():
                logger.info("Trading window ended. Stopping trader.")
                break
            logger.info("Waiting for next auto-cycle after %s seconds.", config.polling_interval_seconds)
            time.sleep(config.polling_interval_seconds)

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
            snapshot = {
                "holdings": [],
                "deposit_total": None,
                "next_day_settlement_amount": None,
                "provisional_settlement_amount": None,
            }
        
        orders = self._get_recent_orders_safe()
        if self.show_orders and orders:
            logger.info("Recent order history (%s rows):", len(orders))
            for row in orders:
                logger.info("\n%s", self.format_order_row_korean(row))
                logger.debug("Raw recent order row: %s", {k: v for k, v in row.items()})
        
        logger.info("Token reuse source: %s", self.client.auth.token_source)
        logger.info("\n%s", self.format_account_snapshot_korean(snapshot, current_price=current_price))
        logger.debug(
            "Raw account snapshot: deposit_total=%s next_day_settlement_amount=%s provisional_settlement_amount=%s holdings=%s",
            snapshot.get("deposit_total"),
            snapshot.get("next_day_settlement_amount"),
            snapshot.get("provisional_settlement_amount"),
            snapshot.get("holdings"),
        )

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

        raw_buy_price = current_price - self.offset
        raw_sell_price = current_price + self.offset
        order_price_buy = normalize_order_price(raw_buy_price, "buy")
        order_price_sell = normalize_order_price(raw_sell_price, "sell")
        if order_price_buy != raw_buy_price:
            logger.info(
                "Order price adjusted to KRX tick: side=buy raw=%s normalized=%s tick=%s",
                raw_buy_price,
                order_price_buy,
                get_tick_size(raw_buy_price),
            )
        if order_price_sell != raw_sell_price:
            logger.info(
                "Order price adjusted to KRX tick: side=sell raw=%s normalized=%s tick=%s",
                raw_sell_price,
                order_price_sell,
                get_tick_size(raw_sell_price),
            )
        if self.buy_only:
            buying_power = self._get_buying_power_safe(config.symbol, order_price_buy)
            before_snapshot.update(buying_power)
            quantity = self._determine_quantity(buying_power.get("buying_power_quantity"))
            if quantity <= 0:
                logger.info("Buy-only mode: no buying power available for a buy order at %s KRW.", order_price_buy)
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
            buying_power = self._get_buying_power_safe(config.symbol, order_price_buy)
            before_snapshot.update(buying_power)
            quantity = self._determine_quantity(buying_power.get("buying_power_quantity"))
            if quantity <= 0:
                logger.info("No buying power available for a buy order at %s KRW.", order_price_buy)
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

    def _format_optional_amount(self, value: Any) -> str:
        return str(value) if value is not None else "unavailable"

    def run_cycle(self, once: bool = False) -> None:
        logger.info(
            "Starting Samsung Electronics auto trader. dry_run=%s paper_trading=%s offset=%s quantity=%s buy_only=%s sell_only=%s show_orders=%s report=%s inspect=%s auto_cycle=%s",
            self.dry_run,
            self.paper_trading,
            self.offset,
            self.requested_quantity,
            self.buy_only,
            self.sell_only,
            self.show_orders,
            self.report,
            self.inspect,
            self.auto_cycle,
        )
        logger.info(
            "AUTO_CONFIG buy_offset=%s take_profit=%s cycle_count=%s",
            self.buy_offset,
            self.take_profit,
            self.cycle_count,
        )

        if not self.paper_trading:
            logger.error("Real trading is disabled. paper_trading must remain enabled.")
            return

        if self.inspect:
            self._run_inspect()
            return

        self._print_window_status()
        if self.auto_cycle:
            self._run_auto_cycle(once=once)
            return

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
    parser.add_argument("--buy-offset", type=int, help="Optional auto-cycle buy offset. Defaults to --offset behavior when omitted.")
    parser.add_argument("--take-profit", type=int, help="Optional auto-cycle sell target above the buy average fill price.")
    parser.add_argument("--cycle-count", type=int, help="Maximum completed auto-cycle attempts to run.")
    parser.add_argument("--quantity", type=int, default=config.default_order_quantity, help="Order quantity for buy/sell actions.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--buy-only", action="store_true", help="Only place buy orders.")
    group.add_argument("--sell-only", action="store_true", help="Only place sell orders.")
    parser.add_argument("--show-orders", action="store_true", help="Show recent order history.")
    parser.add_argument("--report", action="store_true", help="Create sanitized report outputs without exposing secrets.")
    parser.add_argument("--inspect", action="store_true", help="Inspect read-only account and order history without placing orders.")
    parser.add_argument("--auto-cycle", action="store_true", help="Run a stateful mock buy-to-sell cycle using order-status polling.")
    parser.add_argument("--order-status-timeout", type=float, default=120.0, help="Seconds to wait for each submitted order to finish.")
    parser.add_argument("--order-status-poll-interval", type=float, default=5.0, help="Seconds between order-status polls.")
    parser.set_defaults(dry_run=True, paper_trading=True)
    return parser


def run_from_cli(argv: list[str] | None = None) -> None:
    parser = create_parser()
    args = parser.parse_args(argv)

    if args.dry_run is False and not args.confirm_paper_order:
        parser.error("--no-dry-run must be accompanied by --confirm-paper-order to allow paper order submission.")
    if args.paper_trading is False:
        parser.error("Real trading is disabled. Use paper trading only.")
    if args.cycle_count is not None and args.cycle_count <= 0:
        parser.error("--cycle-count must be a positive integer.")

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
        auto_cycle=args.auto_cycle,
        buy_offset=args.buy_offset,
        take_profit=args.take_profit,
        cycle_count=args.cycle_count,
        order_status_timeout=args.order_status_timeout,
        order_status_poll_interval=args.order_status_poll_interval,
    )
    trader.run_cycle(once=args.once)


if __name__ == "__main__":
    run_from_cli()
