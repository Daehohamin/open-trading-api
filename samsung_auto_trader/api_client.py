from __future__ import annotations

import time
from typing import Any

import requests

from samsung_auto_trader.auth import KISAuth
from samsung_auto_trader.config import config
from samsung_auto_trader.kis_rate_limit import throttle_kis_request
from samsung_auto_trader.logger import logger


class KISClient:
    BASE_URL = "https://openapivts.koreainvestment.com:29443"

    def __init__(self) -> None:
        self.auth = KISAuth()
        self.token = self.auth.authenticate()

    def _get_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "authorization": f"Bearer {self.token}",
            "appkey": config.gh_appkey,
            "appsecret": config.gh_appsecret,
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.BASE_URL}{path}"
        headers = self._get_headers(extra_headers)
        for attempt in range(config.max_retries + 1):
            try:
                if method == "GET":
                    throttle_kis_request()
                    response = requests.get(url, headers=headers, params=params, timeout=config.order_timeout_seconds)
                else:
                    throttle_kis_request()
                    response = requests.post(url, headers=headers, json=data, timeout=config.order_timeout_seconds)
                response.raise_for_status()
                payload = response.json()
                if payload.get("rt_cd") not in (None, "0", 0):
                    logger.warning("API returned non-zero rt_cd: %s path=%s", payload.get("rt_cd"), path)
                return payload
            except requests.Timeout:
                logger.warning("Request timeout on %s %s attempt %d.", method, path, attempt + 1)
            except requests.HTTPError as exc:
                # Avoid logging exception text (it may contain full URL and query params)
                status = getattr(response, "status_code", None)
                sanitized_msg = ""
                try:
                    payload = response.json()
                    sanitized_msg = payload.get("msg_cd") or payload.get("msg1") or payload.get("msg") or ""
                except Exception:
                    sanitized_msg = ""
                logger.error("HTTP error: method=%s path=%s status=%s msg=%s", method, path, status, sanitized_msg)
                if status == 401 and attempt == 0:
                    self.token = self.auth.authenticate()
                    headers["authorization"] = f"Bearer {self.token}"
                    continue
                # Raise a sanitized HTTPError without full URL or params
                raise requests.HTTPError(f"HTTP error: method={method} path={path} status={status}")
            except Exception as exc:
                logger.error("Request failure for %s %s: %s", method, path, exc)
            if attempt < config.max_retries:
                logger.info("Retrying %s %s after delay.", method, path)
                time.sleep(config.retry_interval_seconds)
        raise RuntimeError(f"Failed API request after retries: {path}")

    def get_price(self, symbol: str) -> dict[str, Any]:
        path = "/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {
            "FID_COND_MRKT_DIV_CODE": config.market_division_code,
            "FID_INPUT_ISCD": symbol,
        }
        payload = self._request(
            "GET",
            path,
            params=params,
            extra_headers={"tr_id": "FHKST01010100"},
        )
        return payload

    def get_balance(self) -> dict[str, Any]:
        path = "/uapi/domestic-stock/v1/trading/inquire-balance"
        params = {
            "CANO": config.gh_account,
            "ACNT_PRDT_CD": config.gh_product_code,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "00",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        payload = self._request(
            "GET",
            path,
            params=params,
            extra_headers={"tr_id": "VTTC8434R"},
        )
        return payload

    def get_recent_daily_orders(self, days: int = 1) -> dict[str, Any]:
        from datetime import date, timedelta

        path = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        today = date.today()
        
        # For weekends, query the most recent weekday instead
        query_date = today
        while query_date.weekday() > 4:  # 5=Saturday, 6=Sunday
            query_date -= timedelta(days=1)
        
        start = query_date - timedelta(days=max(0, days - 1))
        params = {
            "CANO": config.gh_account,
            "ACNT_PRDT_CD": config.gh_product_code,
            "INQR_STRT_DT": start.strftime("%Y%m%d"),
            "INQR_END_DT": query_date.strftime("%Y%m%d"),
            "SLL_BUY_DVSN_CD": "00",
            "CCLD_DVSN": "00",
            "INQR_DVSN": "00",
            "INQR_DVSN_3": "00",
            "PDNO": config.symbol,
            "EXCG_ID_DVSN_CD": "KRX",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        payload = self._request(
            "GET",
            path,
            params=params,
            extra_headers={"tr_id": "VTTC0081R"},
        )
        return payload

    def place_order(self, action: str, symbol: str, quantity: int, price: int, paper_trading: bool = True) -> dict[str, Any]:
        path = "/uapi/domestic-stock/v1/trading/order-cash"
        if action == "buy":
            tr_id = "VTTC0012U" if paper_trading else "TTTC0012U"
        else:
            tr_id = "VTTC0011U" if paper_trading else "TTTC0011U"
        data = {
            "CANO": config.gh_account,
            "ACNT_PRDT_CD": config.gh_product_code,
            "PDNO": symbol,
            "ORD_DVSN": "00",
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price),
            "EXCG_ID_DVSN_CD": "KRX",
            "SLL_TYPE": "" if action == "buy" else "01",
            "CNDT_PRIC": "",
        }
        headers = {"tr_id": tr_id, "custtype": "P", "tr_cont": ""}
        url = f"{self.BASE_URL}{path}"
        throttle_kis_request()
        response = requests.post(url, headers={**self._get_headers(), **headers}, json=data, timeout=config.order_timeout_seconds)
        status = getattr(response, "status_code", None)
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        msg_cd = payload.get("msg_cd", "")
        msg1 = payload.get("msg1", "")

        if status != 200:
            logger.error("Order request failed: status=%s msg_cd=%s msg1=%s", status, msg_cd, msg1)
            raise requests.HTTPError(f"KIS order HTTP error: status={status} msg_cd={msg_cd} msg1={msg1}")

        rt_cd = payload.get("rt_cd")
        if rt_cd != "0":
            logger.error("Order rejected: status=%s msg_cd=%s msg1=%s", status, msg_cd, msg1)
            raise RuntimeError(f"KIS order rejected: rt_cd={rt_cd} msg_cd={msg_cd} msg1={msg1}")

        output = payload.get("output") or {}
        order_number = output.get("ODNO") or output.get("odno") or output.get("order_no") or ""
        logger.info("Order accepted: rt_cd=%s order_number=%s", rt_cd, order_number)
        return payload
