"""Provider-neutral daily market data contract; native SDKs are lazy-loaded."""
from __future__ import annotations

from typing import Protocol, Any
import pandas as pd


class MarketDataProvider(Protocol):
    provider: str
    source_endpoints: tuple[str, ...]
    provenance: dict[str, Any]

    def fetch_trade_calendar(self, as_of_date: str, *, offset: int = -10) -> pd.DataFrame: ...
    def fetch_universe(self, trade_date: str) -> pd.DataFrame: ...
    def fetch_security_reference(self, codes: list[str], trade_date: str, **kwargs) -> pd.DataFrame: ...
    def fetch_daily_quotes(self, codes: list[str], trade_date: str, **kwargs) -> pd.DataFrame: ...
