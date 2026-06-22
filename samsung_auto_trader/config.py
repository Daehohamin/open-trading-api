import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    symbol: str = "005930"
    market_division_code: str = "J"
    order_offset_krw: int = 2000
    default_order_quantity: int = 1
    max_order_quantity: int = 10
    start_time_str: str = "09:10"
    end_time_str: str = "15:30"
    local_timezone: str = "Asia/Seoul"
    outputs_dir: str = "outputs"
    token_cache_file: str = "token_cache.json"
    default_product_code: str = "01"
    dry_run: bool = True
    paper_trading: bool = True
    polling_interval_seconds: int = 30
    order_timeout_seconds: int = 10
    retry_interval_seconds: int = 5
    max_retries: int = 2
    default_min_request_interval_seconds: float = 1.2

    @property
    def gh_account(self) -> str:
        return os.getenv("GH_ACCOUNT", "").strip()

    @property
    def gh_appkey(self) -> str:
        return os.getenv("GH_APPKEY", "").strip()

    @property
    def gh_appsecret(self) -> str:
        return os.getenv("GH_APPSECRET", "").strip()

    @property
    def gh_product_code(self) -> str:
        return os.getenv("GH_PRODUCT_CODE", self.default_product_code).strip()

    @property
    def kis_min_request_interval_seconds(self) -> float:
        raw_value = os.getenv("KIS_MIN_REQUEST_INTERVAL_SECONDS", "").strip()
        if not raw_value:
            return self.default_min_request_interval_seconds
        try:
            return max(0.0, float(raw_value))
        except ValueError:
            return self.default_min_request_interval_seconds

    @property
    def is_trading_window_enabled(self) -> bool:
        return bool(self.gh_appkey and self.gh_appsecret and self.gh_account)


config = Config()
