"""Normalization, market summary, and promotion quality gates."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

import pandas as pd


UNIVERSE_COLUMNS = (
    "as_of_date",
    "thscode",
    "security_name",
    "security_name_in_time",
    "exchange",
    "board",
)
QUOTE_COLUMNS = (
    "trade_date",
    "thscode",
    "security_name",
    "exchange",
    "board",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "change_ratio",
    "source_provider",
    "source_endpoint",
)


@dataclass(frozen=True)
class QualityReport:
    ok: bool
    metrics: dict[str, Any]
    errors: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "metrics": self.metrics, "errors": self.errors}


def exchange_from_code(thscode: str) -> str:
    suffix = str(thscode).upper().rsplit(".", 1)[-1]
    return {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(suffix, "UNKNOWN")


def board_from_code(thscode: str) -> str:
    code, _, suffix = str(thscode).upper().partition(".")
    if suffix == "BJ":
        return "BSE"
    if suffix == "SH" and code.startswith("688"):
        return "STAR"
    if suffix == "SZ" and code.startswith(("300", "301")):
        return "CHINEXT"
    if suffix == "SH":
        return "SSE_MAIN"
    if suffix == "SZ":
        return "SZSE_MAIN"
    return "UNKNOWN"


def normalize_universe(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"as_of_date", "thscode", "security_name", "security_name_in_time"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError("universe missing columns: " + ", ".join(missing))
    output = frame.copy()
    output["thscode"] = output["thscode"].astype("string").str.upper().str.strip()
    output["exchange"] = output["thscode"].map(exchange_from_code)
    output["board"] = output["thscode"].map(board_from_code)
    output = output.loc[output["exchange"] != "UNKNOWN"]
    return (
        output.loc[:, UNIVERSE_COLUMNS]
        .drop_duplicates("thscode", keep="last")
        .sort_values("thscode")
        .reset_index(drop=True)
    )


def normalize_quotes(frame: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    required = {
        "trade_date",
        "thscode",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "change_ratio",
        "source_provider",
        "source_endpoint",
    }
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError("quotes missing columns: " + ", ".join(missing))
    metadata = universe.loc[:, ["thscode", "security_name", "exchange", "board"]]
    output = frame.copy()
    output["thscode"] = output["thscode"].astype("string").str.upper().str.strip()
    output = output.merge(metadata, how="left", on="thscode", validate="many_to_one")
    for column in ("open", "high", "low", "close", "volume", "amount", "change_ratio"):
        output[column] = pd.to_numeric(output[column], errors="coerce")
    return (
        output.loc[:, QUOTE_COLUMNS]
        .drop_duplicates(["trade_date", "thscode"], keep="last")
        .sort_values("thscode")
        .reset_index(drop=True)
    )


def frame_hash(frame: pd.DataFrame, sort_columns: list[str]) -> str:
    ordered = frame.sort_values(sort_columns).reset_index(drop=True)
    canonical = ordered.to_json(
        orient="records", date_format="iso", double_precision=12, force_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def schema_signature(frame: pd.DataFrame) -> str:
    schema = [(str(column), str(dtype)) for column, dtype in frame.dtypes.items()]
    encoded = json.dumps(schema, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_data(
    universe: pd.DataFrame,
    quotes: pd.DataFrame,
    requested_trade_date: str,
    *,
    min_universe_size: int = 5000,
    min_quote_coverage: float = 0.98,
) -> QualityReport:
    errors: list[str] = []
    universe_codes = set(universe.get("thscode", pd.Series(dtype=str)).dropna().astype(str))
    quote_codes = set(quotes.get("thscode", pd.Series(dtype=str)).dropna().astype(str))
    universe_count = len(universe_codes)
    quote_count = len(quote_codes)
    coverage = quote_count / universe_count if universe_count else 0.0

    universe_duplicates = int(universe.duplicated("thscode").sum()) if not universe.empty else 0
    quote_duplicates = (
        int(quotes.duplicated(["trade_date", "thscode"]).sum()) if not quotes.empty else 0
    )
    universe_dates = sorted(
        universe.get("as_of_date", pd.Series(dtype=str)).dropna().astype(str).unique().tolist()
    )
    quote_dates = sorted(
        quotes.get("trade_date", pd.Series(dtype=str)).dropna().astype(str).unique().tolist()
    )
    universe_exchanges = sorted(
        universe.get("exchange", pd.Series(dtype=str)).dropna().astype(str).unique().tolist()
    )
    quote_exchanges = sorted(
        quotes.get("exchange", pd.Series(dtype=str)).dropna().astype(str).unique().tolist()
    )

    complete_ohlc = quotes[["open", "high", "low", "close"]].notna().all(axis=1)
    ohlc = quotes.loc[complete_ohlc, ["open", "high", "low", "close"]]
    ohlc_invalid = int(
        (
            (ohlc["high"] < ohlc[["open", "low", "close"]].max(axis=1))
            | (ohlc["low"] > ohlc[["open", "high", "close"]].min(axis=1))
        ).sum()
    )
    negative_volume = int((quotes["volume"].dropna() < 0).sum())
    negative_amount = int((quotes["amount"].dropna() < 0).sum())
    unknown_quote_codes = sorted(quote_codes.difference(universe_codes))

    if universe_count < min_universe_size:
        errors.append(
            f"universe_count {universe_count} below minimum {min_universe_size}"
        )
    if coverage < min_quote_coverage:
        errors.append(
            f"quote_coverage {coverage:.4f} below minimum {min_quote_coverage:.4f}"
        )
    if universe_dates != [requested_trade_date]:
        errors.append(
            f"universe source dates {universe_dates} do not match {requested_trade_date}"
        )
    if quote_dates != [requested_trade_date]:
        errors.append(f"quote source dates {quote_dates} do not match {requested_trade_date}")
    required_exchanges = {"SSE", "SZSE", "BSE"}
    if not required_exchanges.issubset(set(universe_exchanges)):
        errors.append("universe does not cover SSE, SZSE, and BSE")
    if not required_exchanges.issubset(set(quote_exchanges)):
        errors.append("quotes do not cover SSE, SZSE, and BSE")
    if universe_duplicates:
        errors.append(f"universe contains {universe_duplicates} duplicate codes")
    if quote_duplicates:
        errors.append(f"quotes contain {quote_duplicates} duplicate code/date rows")
    if unknown_quote_codes:
        errors.append(f"quotes contain {len(unknown_quote_codes)} codes outside universe")
    if ohlc_invalid:
        errors.append(f"quotes contain {ohlc_invalid} invalid OHLC rows")
    if negative_volume:
        errors.append(f"quotes contain {negative_volume} negative volume rows")
    if negative_amount:
        errors.append(f"quotes contain {negative_amount} negative amount rows")

    metrics: dict[str, Any] = {
        "requested_trade_date": requested_trade_date,
        "universe_count": universe_count,
        "quote_count": quote_count,
        "quote_coverage": round(coverage, 6),
        "universe_dates": universe_dates,
        "quote_dates": quote_dates,
        "universe_exchanges": universe_exchanges,
        "quote_exchanges": quote_exchanges,
        "universe_duplicates": universe_duplicates,
        "quote_duplicates": quote_duplicates,
        "unknown_quote_codes": len(unknown_quote_codes),
        "complete_ohlc_rows": int(complete_ohlc.sum()),
        "invalid_ohlc_rows": ohlc_invalid,
        "negative_volume_rows": negative_volume,
        "negative_amount_rows": negative_amount,
    }
    return QualityReport(ok=not errors, metrics=metrics, errors=errors)


def _number(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def build_market_summary(quotes: pd.DataFrame, trade_date: str) -> dict[str, Any]:
    changes = pd.to_numeric(quotes["change_ratio"], errors="coerce").dropna()
    amounts = pd.to_numeric(quotes["amount"], errors="coerce").dropna()
    exchange_counts = {
        str(key): int(value)
        for key, value in quotes.groupby("exchange", dropna=False).size().items()
    }
    board_counts = {
        str(key): int(value)
        for key, value in quotes.groupby("board", dropna=False).size().items()
    }
    return {
        "schema_version": 1,
        "trade_date": trade_date,
        "quoted_securities": int(quotes["thscode"].nunique()),
        "advancers": int((changes > 0).sum()),
        "decliners": int((changes < 0).sum()),
        "unchanged": int((changes == 0).sum()),
        "moves_ge_9_5pct": int((changes >= 9.5).sum()),
        "moves_le_minus_9_5pct": int((changes <= -9.5).sum()),
        "equal_weight_change_pct": _number(changes.mean()),
        "median_change_pct": _number(changes.median()),
        "total_amount": _number(amounts.sum()),
        "exchange_quote_counts": exchange_counts,
        "board_quote_counts": board_counts,
    }


__all__ = [
    "QualityReport",
    "build_market_summary",
    "frame_hash",
    "normalize_quotes",
    "normalize_universe",
    "schema_signature",
    "validate_data",
]
