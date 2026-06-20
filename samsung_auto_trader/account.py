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
        available_cash = 0

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
            available_cash = int(output2.get("ord_psbl_cash", 0) or 0)
            if available_cash == 0:
                available_cash = int(output2.get("dnca_tot_amt", 0) or 0)
            if available_cash == 0:
                available_cash = int(output2.get("prvs_rcdl_excc_amt", 0) or 0)
        elif isinstance(payload.get("output"), dict):
            available_cash = int(payload["output"].get("ord_psbl_cash", 0) or 0)
        elif isinstance(payload.get("output"), list) and payload["output"]:
            available_cash = int(payload["output"][0].get("ord_psbl_cash", 0) or 0)

        if available_cash == 0 and isinstance(payload.get("output1"), list) and payload["output1"]:
            available_cash = int(payload["output1"][0].get("ord_psbl_cash", 0) or 0)

        logger.info("Account available cash: %s", available_cash)
        return {"holdings": holdings, "available_cash": available_cash}

    def find_holding(self, holdings: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
        for item in holdings:
            if str(item.get("pdno", "")) == symbol or str(item.get("pdno", "")).zfill(6) == symbol:
                return item
        return None
