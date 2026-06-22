from typing import Any

from samsung_auto_trader.api_client import KISClient
from samsung_auto_trader.logger import logger


class AccountService:
    def __init__(self, client: KISClient) -> None:
        self.client = client

    def get_account_snapshot(self) -> dict[str, Any]:
        logger.info("Requesting account balance and holdings.")
        payload = self.client.get_balance()
        holdings = []
        deposit_total = None
        next_day_settlement_amount = None
        provisional_settlement_amount = None

        output1 = payload.get("output1")
        if isinstance(output1, dict):
            output1 = [output1]
        if isinstance(output1, list):
            holdings = output1
        elif isinstance(payload.get("output"), list):
            holdings = payload["output"]

        if not holdings:
            logger.info("No holdings found in balance response.")

        output2 = payload.get("output2")
        if isinstance(output2, list) and output2:
            output2 = output2[0]

        if isinstance(output2, dict):
            deposit_total = self._parse_int(output2.get("dnca_tot_amt"))
            next_day_settlement_amount = self._parse_int(output2.get("nxdy_excc_amt"))
            provisional_settlement_amount = self._parse_int(output2.get("prvs_rcdl_excc_amt"))

        logger.info(
            "Account settlement amounts: deposit_total=%s next_day_settlement_amount=%s provisional_settlement_amount=%s",
            deposit_total,
            next_day_settlement_amount,
            provisional_settlement_amount,
        )
        return {
            "holdings": holdings,
            "deposit_total": deposit_total,
            "next_day_settlement_amount": next_day_settlement_amount,
            "provisional_settlement_amount": provisional_settlement_amount,
        }

    def _parse_int(self, value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def find_holding(self, holdings: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
        for item in holdings:
            if str(item.get("pdno", "")) == symbol or str(item.get("pdno", "")).zfill(6) == symbol:
                return item
        return None
