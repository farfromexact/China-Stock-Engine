"""Read-only iFinD Quant HTTP client for A-share daily data."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
import os
import time
from typing import Any
import urllib.error
import urllib.request

import pandas as pd


DEFAULT_BASE_URL = "https://quantapi.51ifind.com/api/v1"
ALL_A_SHARE_REPORT = "p03291"
ALL_A_SHARE_BLOCK = "001005010"
UNIVERSE_FIELDS = (
    "p03291_f001",
    "p03291_f002",
    "p03291_f003",
    "p03291_f004",
)
QUOTE_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "changeRatio",
)

Transport = Callable[
    [str, dict[str, str], dict[str, Any] | None, int], dict[str, Any]
]
ProgressCallback = Callable[[int, int], None]


class IFindHTTPError(RuntimeError):
    """Raised for iFinD authentication, entitlement, transport, or schema errors."""


class IFindHTTPTransientError(IFindHTTPError):
    """Raised for a transport failure that is safe to retry."""


def _safe_message(value: Any) -> str:
    return str(value or "unknown iFinD error")[:400]


def _default_transport(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout: int,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Accept", "application/json")
    request.add_header("Connection", "close")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "china-stock-engine-ifind/0.1")
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
            code = parsed.get("errorcode", parsed.get("errcode"))
            message = parsed.get("errmsg", parsed.get("message"))
            detail = f"code {code}: {_safe_message(message)}"
        except Exception:
            detail = "non-JSON error response"
        error_type = (
            IFindHTTPTransientError
            if exc.code == 429 or 500 <= exc.code <= 599
            else IFindHTTPError
        )
        raise error_type(f"iFinD HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise IFindHTTPTransientError(
            f"iFinD transport failed: {type(exc).__name__}: {_safe_message(exc)}"
        ) from exc
    except Exception as exc:
        raise IFindHTTPError(
            f"iFinD transport failed: {type(exc).__name__}: {_safe_message(exc)}"
        ) from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IFindHTTPError("iFinD response was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise IFindHTTPError("iFinD response JSON was not an object")
    return parsed


def _raise_api_error(endpoint: str, response: dict[str, Any]) -> None:
    error_code = response.get("errorcode", response.get("errcode"))
    if error_code not in (0, "0", None):
        message = response.get("errmsg", response.get("message"))
        raise IFindHTTPError(
            f"iFinD {endpoint} failed with code {error_code}: {_safe_message(message)}"
        )


def _tables_frame(response: dict[str, Any]) -> pd.DataFrame:
    """Normalize common iFinD table-list responses without retaining raw payloads."""

    tables: Any = response.get("tables")
    if tables is None:
        tables = response.get("data") or []
    if isinstance(tables, dict):
        if "table" in tables or "data" in tables:
            tables = [tables]
        elif any(isinstance(value, list) for value in tables.values()):
            tables = [{"table": tables}]
        else:
            tables = []
    if not isinstance(tables, list):
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for item in tables:
        if not isinstance(item, dict):
            continue
        code = item.get("thscode") or item.get("code")
        times = item.get("time") or []
        if not isinstance(times, list):
            times = [times]
        data = item.get("table") or item.get("data") or {}
        if not isinstance(data, dict):
            continue
        list_lengths = [len(value) for value in data.values() if isinstance(value, list)]
        row_count = max(list_lengths + [len(times), 1])
        for index in range(row_count):
            row: dict[str, Any] = {}
            if code is not None:
                row["thscode"] = code
            if times:
                row["time"] = times[index] if index < len(times) else times[-1]
            for field, value in data.items():
                if isinstance(value, list):
                    row[field] = value[index] if index < len(value) else None
                else:
                    row[field] = value
            rows.append(row)
    return pd.DataFrame(rows)


def _chunks(values: Sequence[str], size: int) -> list[list[str]]:
    return [list(values[start : start + size]) for start in range(0, len(values), size)]


def _date_text(values: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(values.astype(str), errors="coerce")
    return parsed.dt.strftime("%Y-%m-%d")


@dataclass
class IFindHTTPClient:
    """Short-lived Quant API client; refresh/access tokens remain in memory."""

    refresh_token: str | None = None
    access_token: str | None = None
    base_url: str = DEFAULT_BASE_URL
    timeout: int = 45
    transport: Transport = _default_transport
    max_transport_attempts: int = 3
    retry_backoff_seconds: float = 1.0
    sleeper: Callable[[float], None] = time.sleep

    def _send(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if self.max_transport_attempts < 1:
            raise ValueError("max_transport_attempts must be positive")
        if self.retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds cannot be negative")

        for attempt in range(1, self.max_transport_attempts + 1):
            try:
                return self.transport(url, headers, payload, self.timeout)
            except IFindHTTPTransientError as exc:
                if attempt == self.max_transport_attempts:
                    raise IFindHTTPTransientError(
                        f"{exc} after {attempt} attempts"
                    ) from exc
                delay = self.retry_backoff_seconds * (2 ** (attempt - 1))
                self.sleeper(delay)
        raise AssertionError("unreachable transport retry state")

    def get_access_token(self) -> str:
        if self.access_token:
            return self.access_token
        refresh_token = self.refresh_token or os.environ.get("IFIND_REFRESH_TOKEN")
        if not refresh_token:
            raise IFindHTTPError(
                "iFinD refresh token is required in IFIND_REFRESH_TOKEN or hidden input"
            )
        response = self._send(
            f"{self.base_url}/get_access_token",
            {"refresh_token": refresh_token},
            None,
        )
        _raise_api_error("get_access_token", response)
        token = (response.get("data") or {}).get("access_token")
        if not token:
            raise IFindHTTPError("iFinD get_access_token returned no access token")
        self.access_token = str(token)
        return self.access_token

    def request(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._send(
            f"{self.base_url}/{endpoint}",
            {"access_token": self.get_access_token(), "ifindlang": "cn"},
            payload,
        )
        _raise_api_error(endpoint, response)
        return response

    def fetch_universe(self, trade_date: str) -> pd.DataFrame:
        compact_date = trade_date.replace("-", "")
        response = self.request(
            "data_pool",
            {
                "reportname": ALL_A_SHARE_REPORT,
                "functionpara": {
                    "date": compact_date,
                    "blockname": ALL_A_SHARE_BLOCK,
                    "iv_type": "allcontract",
                },
                "outputpara": ",".join(UNIVERSE_FIELDS),
            },
        )
        frame = _tables_frame(response)
        missing = sorted(set(UNIVERSE_FIELDS).difference(frame.columns))
        if missing:
            raise IFindHTTPError(
                "iFinD A-share universe response missing fields: " + ", ".join(missing)
            )
        output = pd.DataFrame(
            {
                "as_of_date": _date_text(frame["p03291_f001"]),
                "thscode": frame["p03291_f002"].astype("string").str.upper().str.strip(),
                "security_name": frame["p03291_f003"].astype("string").str.strip(),
                "security_name_in_time": frame["p03291_f004"].astype("string").str.strip(),
            }
        )
        valid_code = output["thscode"].str.match(
            r"^\d{6}\.(?:SH|SZ|BJ)$", case=False, na=False
        )
        output = output.loc[valid_code].drop_duplicates("thscode", keep="last")
        return output.reset_index(drop=True)

    def _history_batch(self, codes: Sequence[str], trade_date: str) -> pd.DataFrame:
        response = self.request(
            "cmd_history_quotation",
            {
                "codes": ",".join(codes),
                "indicators": ",".join(QUOTE_FIELDS),
                "startdate": trade_date,
                "enddate": trade_date,
                "functionpara": {"Fill": "Omit"},
            },
        )
        return _tables_frame(response)

    def fetch_daily_quotes(
        self,
        codes: Sequence[str],
        trade_date: str,
        *,
        batch_size: int = 300,
        request_interval_seconds: float = 0.15,
        progress: ProgressCallback | None = None,
    ) -> pd.DataFrame:
        normalized_codes = sorted(
            {str(code).strip().upper() for code in codes if str(code).strip()}
        )
        if not normalized_codes:
            return pd.DataFrame()
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if request_interval_seconds < 0:
            raise ValueError("request interval cannot be negative")

        batches = _chunks(normalized_codes, batch_size)
        frames: list[pd.DataFrame] = []
        last_request_at = 0.0

        def query(batch: Sequence[str]) -> list[pd.DataFrame]:
            nonlocal last_request_at
            if request_interval_seconds and last_request_at:
                remaining = request_interval_seconds - (time.monotonic() - last_request_at)
                if remaining > 0:
                    time.sleep(remaining)
            last_request_at = time.monotonic()
            try:
                return [self._history_batch(batch, trade_date)]
            except IFindHTTPError as exc:
                if "code -4210" not in str(exc) or len(batch) <= 1:
                    raise
                midpoint = len(batch) // 2
                return query(batch[:midpoint]) + query(batch[midpoint:])

        for index, batch in enumerate(batches, start=1):
            frames.extend(query(batch))
            if progress:
                progress(index, len(batches))
        frames = [frame for frame in frames if not frame.empty]
        raw = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if raw.empty:
            return raw
        required = {"thscode", "time"}
        missing = sorted(required.difference(raw.columns))
        if missing:
            raise IFindHTTPError(
                "iFinD history response missing fields: " + ", ".join(missing)
            )
        market_columns = [field for field in QUOTE_FIELDS if field in raw.columns]
        if not market_columns:
            raise IFindHTTPError("iFinD history response contained no requested fields")
        raw = raw.loc[raw[market_columns].notna().any(axis=1)].copy()
        if raw.empty:
            return raw
        output = pd.DataFrame(
            {
                "trade_date": _date_text(raw["time"]),
                "thscode": raw["thscode"].astype("string").str.upper().str.strip(),
            }
        )
        rename = {
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "amount": "amount",
            "changeRatio": "change_ratio",
        }
        for source, target in rename.items():
            values = raw[source] if source in raw.columns else None
            output[target] = pd.to_numeric(values, errors="coerce")
        output["source_provider"] = "ifind_http"
        output["source_endpoint"] = "cmd_history_quotation"
        return output.drop_duplicates(["trade_date", "thscode"], keep="last").reset_index(
            drop=True
        )


__all__ = [
    "ALL_A_SHARE_BLOCK",
    "ALL_A_SHARE_REPORT",
    "IFindHTTPClient",
    "IFindHTTPError",
    "IFindHTTPTransientError",
    "QUOTE_FIELDS",
    "UNIVERSE_FIELDS",
]
