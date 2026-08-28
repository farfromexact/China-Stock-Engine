"""Normalization, market summary, and promotion quality gates."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

import pandas as pd


UNIVERSE_COLUMNS = (
    "as_of_date", "thscode", "security_name", "security_name_in_time", "exchange", "board",
)
REFERENCE_COLUMNS = (
    "as_of_date", "thscode", "security_name", "exchange", "board", "listing_date",
    "total_shares", "float_a_shares", "source_provider", "source_endpoint",
)
QUOTE_COLUMNS = (
    "trade_date", "thscode", "security_name", "exchange", "board", "open", "high",
    "low", "close", "pre_close", "avg_price", "volume", "amount", "turnover_ratio",
    "change_ratio", "source_provider", "source_endpoint",
)
CALENDAR_COLUMNS = (
    "as_of_date", "trade_date", "calendar", "market_code", "is_open",
    "source_provider", "source_endpoint",
)
STATUS_COLUMNS = (
    "trade_date", "thscode", "security_name", "exchange", "board", "quote_row_present",
    "has_price_observation", "has_turnover_observation", "observation_state",
    "source_provider", "source_endpoint",
)
PRICE_COMPARISON_TOLERANCE = 1e-6


@dataclass(frozen=True)
class QualityReport:
    ok: bool
    metrics: dict[str, Any]
    errors: list[str]
    warnings: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "metrics": self.metrics,
            "errors": self.errors,
            "warnings": self.warnings,
        }


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


def _normalized_date(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values.astype(str), errors="coerce").dt.strftime("%Y-%m-%d")


def normalize_universe(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"as_of_date", "thscode", "security_name", "security_name_in_time"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError("universe missing columns: " + ", ".join(missing))
    output = frame.copy()
    output["as_of_date"] = _normalized_date(output["as_of_date"])
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


def normalize_security_reference(
    frame: pd.DataFrame, universe: pd.DataFrame
) -> pd.DataFrame:
    required = {
        "as_of_date", "thscode", "listing_date", "total_shares", "float_a_shares",
        "source_provider", "source_endpoint",
    }
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError("security reference missing columns: " + ", ".join(missing))
    metadata = universe.loc[:, ["thscode", "security_name", "exchange", "board"]]
    output = frame.copy()
    output["as_of_date"] = _normalized_date(output["as_of_date"])
    output["listing_date"] = _normalized_date(output["listing_date"])
    output["thscode"] = output["thscode"].astype("string").str.upper().str.strip()
    # Cached fact partitions are already normalized and therefore already carry
    # these descriptive columns.  Drop them before joining the PIT universe so
    # normalization is idempotent and cannot create ``*_x``/``*_y`` columns.
    output = output.drop(
        columns=["security_name", "exchange", "board"], errors="ignore"
    )
    output = output.merge(metadata, how="left", on="thscode", validate="many_to_one")
    for column in ("total_shares", "float_a_shares"):
        output[column] = pd.to_numeric(output[column], errors="coerce")
    return (
        output.loc[:, REFERENCE_COLUMNS]
        .drop_duplicates("thscode", keep="last")
        .sort_values("thscode")
        .reset_index(drop=True)
    )


def normalize_quotes(frame: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    required = {
        "trade_date", "thscode", "open", "high", "low", "close", "pre_close",
        "avg_price", "volume", "amount", "turnover_ratio", "change_ratio",
        "source_provider", "source_endpoint",
    }
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError("quotes missing columns: " + ", ".join(missing))
    metadata = universe.loc[:, ["thscode", "security_name", "exchange", "board"]]
    output = frame.copy()
    output["trade_date"] = _normalized_date(output["trade_date"])
    output["thscode"] = output["thscode"].astype("string").str.upper().str.strip()
    output = output.drop(
        columns=["security_name", "exchange", "board"], errors="ignore"
    )
    output = output.merge(metadata, how="left", on="thscode", validate="many_to_one")
    numeric = (
        "open", "high", "low", "close", "pre_close", "avg_price", "volume", "amount",
        "turnover_ratio", "change_ratio",
    )
    for column in numeric:
        output[column] = pd.to_numeric(output[column], errors="coerce")
    return (
        output.loc[:, QUOTE_COLUMNS]
        .drop_duplicates(["trade_date", "thscode"], keep="last")
        .sort_values("thscode")
        .reset_index(drop=True)
    )


def normalize_trade_calendar(frame: pd.DataFrame) -> pd.DataFrame:
    missing = sorted(set(CALENDAR_COLUMNS).difference(frame.columns))
    if missing:
        raise ValueError("trade calendar missing columns: " + ", ".join(missing))
    output = frame.copy()
    output["as_of_date"] = _normalized_date(output["as_of_date"])
    output["trade_date"] = _normalized_date(output["trade_date"])
    output["market_code"] = output["market_code"].astype("string")
    output["is_open"] = output["is_open"].astype("boolean")
    return (
        output.loc[:, CALENDAR_COLUMNS]
        .dropna(subset=["trade_date"])
        .drop_duplicates(["calendar", "trade_date"], keep="last")
        .sort_values(["calendar", "trade_date"])
        .reset_index(drop=True)
    )


def build_daily_security_status(
    universe: pd.DataFrame, quotes: pd.DataFrame, trade_date: str
) -> pd.DataFrame:
    """Build observation states without claiming that missing quotes are suspensions."""

    observed = quotes.loc[:, ["thscode", "close", "volume", "amount"]].copy()
    observed["quote_row_present"] = True
    output = universe.loc[
        :, ["thscode", "security_name", "exchange", "board"]
    ].merge(observed, how="left", on="thscode", validate="one_to_one")
    output["quote_row_present"] = output["quote_row_present"].fillna(False).astype(bool)
    output["has_price_observation"] = output["close"].notna()
    output["has_turnover_observation"] = (
        output["volume"].fillna(0).gt(0) & output["amount"].fillna(0).gt(0)
    )
    output["observation_state"] = "no_quote_observed"
    output.loc[
        output["quote_row_present"] & ~output["has_turnover_observation"],
        "observation_state",
    ] = "quote_without_turnover"
    output.loc[output["has_turnover_observation"], "observation_state"] = "traded"
    output["trade_date"] = trade_date
    output["source_provider"] = "derived"
    output["source_endpoint"] = "universe+cmd_history_quotation"
    return output.loc[:, STATUS_COLUMNS].sort_values("thscode").reset_index(drop=True)


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


def _coverage(frame: pd.DataFrame, column: str) -> float:
    if frame.empty or column not in frame.columns:
        return 0.0
    return float(frame[column].notna().mean())


def _cross_day_drift(
    universe: pd.DataFrame,
    security_reference: pd.DataFrame,
    quotes: pd.DataFrame,
    daily_status: pd.DataFrame,
    previous: dict[str, pd.DataFrame] | None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Compare the current normalized snapshot with the prior persisted session."""

    if not previous:
        return {"state": "missing_previous_session", "alerts": []}, [], []
    prior_universe = previous.get("universe", pd.DataFrame())
    prior_reference = previous.get("security_reference", pd.DataFrame())
    prior_quotes = previous.get("quotes", pd.DataFrame())
    prior_status = previous.get("daily_status", pd.DataFrame())
    if any(
        frame.empty
        for frame in (prior_universe, prior_reference, prior_quotes, prior_status)
    ):
        return {"state": "incomplete_previous_session", "alerts": []}, [], []

    warnings: list[str] = []
    errors: list[str] = []
    alerts: list[dict[str, Any]] = []

    def add_alert(metric: str, severity: str, message: str) -> None:
        alerts.append({"metric": metric, "severity": severity, "message": message})
        (errors if severity == "error" else warnings).append(message)

    def code_count(frame: pd.DataFrame) -> int:
        return int(frame["thscode"].dropna().astype(str).nunique())

    current_universe_count = code_count(universe)
    prior_universe_count = code_count(prior_universe)
    universe_delta = current_universe_count - prior_universe_count
    universe_delta_ratio = (
        universe_delta / prior_universe_count if prior_universe_count else None
    )
    if universe_delta_ratio is not None and abs(universe_delta_ratio) > 0.08:
        add_alert(
            "universe_count",
            "error",
            f"universe_count changed {universe_delta_ratio:.2%} across sessions",
        )
    elif universe_delta_ratio is not None and abs(universe_delta_ratio) > 0.02:
        add_alert(
            "universe_count",
            "warning",
            f"universe_count changed {universe_delta_ratio:.2%} across sessions",
        )

    def snapshot_coverage(numerator: pd.DataFrame, denominator: pd.DataFrame) -> float:
        total = code_count(denominator)
        return code_count(numerator) / total if total else 0.0

    coverage_metrics: dict[str, Any] = {}
    for name, current_frame, prior_frame in (
        ("quote_coverage", quotes, prior_quotes),
        ("reference_coverage", security_reference, prior_reference),
    ):
        current_value = snapshot_coverage(current_frame, universe)
        prior_value = snapshot_coverage(prior_frame, prior_universe)
        delta = current_value - prior_value
        coverage_metrics[name] = {
            "current": round(current_value, 6),
            "previous": round(prior_value, 6),
            "delta": round(delta, 6),
        }
        if delta < -0.05:
            add_alert(name, "error", f"{name} dropped {abs(delta):.2%} across sessions")
        elif delta < -0.01:
            add_alert(
                name, "warning", f"{name} dropped {abs(delta):.2%} across sessions"
            )

    count_drift: dict[str, Any] = {}
    for field in ("exchange", "board"):
        current_counts = {
            str(key): int(value)
            for key, value in universe.groupby(field, dropna=False).size().items()
        }
        prior_counts = {
            str(key): int(value)
            for key, value in prior_universe.groupby(field, dropna=False).size().items()
        }
        field_metrics: dict[str, Any] = {}
        for key in sorted(set(current_counts) | set(prior_counts)):
            current_value = current_counts.get(key, 0)
            prior_value = prior_counts.get(key, 0)
            delta = current_value - prior_value
            ratio = delta / prior_value if prior_value else None
            field_metrics[key] = {
                "current": current_value,
                "previous": prior_value,
                "delta": delta,
                "delta_ratio": round(ratio, 6) if ratio is not None else None,
            }
            if prior_value >= 20 and ratio is not None and abs(ratio) > 0.20:
                add_alert(
                    f"{field}_count.{key}",
                    "error",
                    f"{field} {key} count changed {ratio:.2%} across sessions",
                )
            elif prior_value >= 20 and ratio is not None and abs(ratio) > 0.05:
                add_alert(
                    f"{field}_count.{key}",
                    "warning",
                    f"{field} {key} count changed {ratio:.2%} across sessions",
                )
        count_drift[field] = field_metrics

    def no_quote_count(frame: pd.DataFrame) -> int:
        return int(frame["observation_state"].astype(str).eq("no_quote_observed").sum())

    current_no_quote = no_quote_count(daily_status)
    prior_no_quote = no_quote_count(prior_status)
    no_quote_delta = current_no_quote - prior_no_quote
    if current_no_quote > max(prior_no_quote + 200, prior_no_quote * 5):
        add_alert(
            "no_quote_observed",
            "error",
            f"no_quote_observed increased from {prior_no_quote} to {current_no_quote}",
        )
    elif current_no_quote > max(prior_no_quote + 25, prior_no_quote * 2):
        add_alert(
            "no_quote_observed",
            "warning",
            f"no_quote_observed increased from {prior_no_quote} to {current_no_quote}",
        )

    current_amount = float(pd.to_numeric(quotes["amount"], errors="coerce").sum())
    prior_amount = float(
        pd.to_numeric(prior_quotes["amount"], errors="coerce").sum()
    )
    amount_ratio = current_amount / prior_amount if prior_amount > 0 else None
    if amount_ratio is not None and (amount_ratio < 0.10 or amount_ratio > 10.0):
        add_alert(
            "total_amount",
            "error",
            f"total_amount ratio versus prior session is {amount_ratio:.3f}",
        )
    elif amount_ratio is not None and (amount_ratio < 0.35 or amount_ratio > 2.85):
        add_alert(
            "total_amount",
            "warning",
            f"total_amount ratio versus prior session is {amount_ratio:.3f}",
        )

    continuity = quotes.loc[:, ["thscode", "pre_close"]].merge(
        prior_quotes.loc[:, ["thscode", "close"]].rename(
            columns={"close": "previous_close"}
        ),
        how="inner",
        on="thscode",
        validate="one_to_one",
    )
    continuity["pre_close"] = pd.to_numeric(
        continuity["pre_close"], errors="coerce"
    )
    continuity["previous_close"] = pd.to_numeric(
        continuity["previous_close"], errors="coerce"
    )
    comparable = continuity[["pre_close", "previous_close"]].notna().all(axis=1)
    comparable &= continuity["previous_close"].gt(0)
    continuity_delta = (
        continuity.loc[comparable, "pre_close"]
        .div(continuity.loc[comparable, "previous_close"])
        .sub(1)
        .abs()
    )
    mismatch_count = int(continuity_delta.gt(0.05).sum())
    comparable_count = int(comparable.sum())
    mismatch_ratio = mismatch_count / comparable_count if comparable_count else 0.0
    if comparable_count and mismatch_ratio > 0.10:
        add_alert(
            "pre_close_continuity",
            "error",
            f"pre_close continuity mismatch ratio is {mismatch_ratio:.2%}",
        )
    elif comparable_count and mismatch_ratio > 0.02:
        add_alert(
            "pre_close_continuity",
            "warning",
            f"pre_close continuity mismatch ratio is {mismatch_ratio:.2%}",
        )

    previous_dates = sorted(
        prior_quotes["trade_date"].dropna().astype(str).unique().tolist()
    )
    metrics = {
        "state": "checked",
        "previous_trade_date": previous_dates[-1] if previous_dates else None,
        "universe_count": {
            "current": current_universe_count,
            "previous": prior_universe_count,
            "delta": universe_delta,
            "delta_ratio": round(universe_delta_ratio, 6)
            if universe_delta_ratio is not None
            else None,
        },
        **coverage_metrics,
        "universe_group_counts": count_drift,
        "no_quote_observed": {
            "current": current_no_quote,
            "previous": prior_no_quote,
            "delta": no_quote_delta,
        },
        "total_amount": {
            "current": current_amount,
            "previous": prior_amount,
            "ratio": round(amount_ratio, 6) if amount_ratio is not None else None,
        },
        "pre_close_continuity": {
            "comparable_count": comparable_count,
            "mismatch_count": mismatch_count,
            "mismatch_ratio": round(mismatch_ratio, 6),
            "per_security_tolerance": 0.05,
        },
        "alerts": alerts,
    }
    return metrics, warnings, errors


def validate_data(
    universe: pd.DataFrame,
    security_reference: pd.DataFrame,
    quotes: pd.DataFrame,
    trading_calendar: pd.DataFrame,
    daily_status: pd.DataFrame,
    requested_trade_date: str,
    *,
    min_universe_size: int = 5000,
    min_quote_coverage: float = 0.98,
    min_reference_coverage: float = 0.98,
    min_extended_field_coverage: float = 0.95,
    previous: dict[str, pd.DataFrame] | None = None,
) -> QualityReport:
    errors: list[str] = []
    warnings: list[str] = []
    codes = lambda frame: set(
        frame.get("thscode", pd.Series(dtype=str)).dropna().astype(str)
    )
    universe_codes = codes(universe)
    reference_codes = codes(security_reference)
    quote_codes = codes(quotes)
    status_codes = codes(daily_status)
    universe_count = len(universe_codes)
    reference_count = len(reference_codes)
    quote_count = len(quote_codes)
    quote_coverage = quote_count / universe_count if universe_count else 0.0
    reference_coverage = reference_count / universe_count if universe_count else 0.0

    universe_duplicates = int(universe.duplicated("thscode").sum()) if not universe.empty else 0
    reference_duplicates = int(security_reference.duplicated("thscode").sum()) if not security_reference.empty else 0
    quote_duplicates = int(quotes.duplicated(["trade_date", "thscode"]).sum()) if not quotes.empty else 0
    status_duplicates = int(daily_status.duplicated(["trade_date", "thscode"]).sum()) if not daily_status.empty else 0

    def unique_dates(frame: pd.DataFrame, column: str) -> list[str]:
        return sorted(frame.get(column, pd.Series(dtype=str)).dropna().astype(str).unique().tolist())

    universe_dates = unique_dates(universe, "as_of_date")
    reference_dates = unique_dates(security_reference, "as_of_date")
    quote_dates = unique_dates(quotes, "trade_date")
    status_dates = unique_dates(daily_status, "trade_date")
    calendar_as_of_dates = unique_dates(trading_calendar, "as_of_date")
    open_mask = trading_calendar["is_open"].fillna(False).astype(bool)
    open_calendar_dates = set(
        trading_calendar.loc[open_mask, "trade_date"].dropna().astype(str)
    )
    universe_exchanges = unique_dates(universe, "exchange")
    quote_exchanges = unique_dates(quotes, "exchange")

    complete_ohlc = quotes[["open", "high", "low", "close"]].notna().all(axis=1)
    ohlc = quotes.loc[complete_ohlc, ["open", "high", "low", "close"]]
    ohlc_invalid = int(
        ((ohlc["high"] < ohlc[["open", "low", "close"]].max(axis=1))
         | (ohlc["low"] > ohlc[["open", "high", "close"]].min(axis=1))).sum()
    )
    nonpositive_prices = int(sum(
        (quotes[column].dropna() <= 0).sum()
        for column in ("open", "high", "low", "close", "pre_close")
    ))
    negative_volume = int((quotes["volume"].dropna() < 0).sum())
    negative_amount = int((quotes["amount"].dropna() < 0).sum())
    negative_turnover = int((quotes["turnover_ratio"].dropna() < 0).sum())
    avg_comparable = (
        quotes[["avg_price", "low", "high"]].notna().all(axis=1)
        & quotes["volume"].fillna(0).gt(0)
    )
    avg_outside_range = int((
        (
            quotes.loc[avg_comparable, "avg_price"]
            < quotes.loc[avg_comparable, "low"] - PRICE_COMPARISON_TOLERANCE
        )
        | (
            quotes.loc[avg_comparable, "avg_price"]
            > quotes.loc[avg_comparable, "high"] + PRICE_COMPARISON_TOLERANCE
        )
    ).sum())
    change_comparable = (
        quotes[["close", "pre_close", "change_ratio"]].notna().all(axis=1)
        & quotes["pre_close"].gt(0)
    )
    expected_change = (
        quotes.loc[change_comparable, "close"]
        / quotes.loc[change_comparable, "pre_close"] - 1
    ) * 100
    change_ratio_inconsistent = int((
        quotes.loc[change_comparable, "change_ratio"].sub(expected_change).abs() > 0.06
    ).sum())

    listing_dates = pd.to_datetime(security_reference["listing_date"], errors="coerce")
    future_listing_dates = int((listing_dates > pd.Timestamp(requested_trade_date)).sum())
    negative_total_shares = int((security_reference["total_shares"].dropna() < 0).sum())
    negative_float_shares = int((security_reference["float_a_shares"].dropna() < 0).sum())
    share_comparable = security_reference[["total_shares", "float_a_shares"]].notna().all(axis=1)
    float_exceeds_total = int((
        security_reference.loc[share_comparable, "float_a_shares"]
        > security_reference.loc[share_comparable, "total_shares"] + 1e-6
    ).sum())

    quote_field_coverage = {
        field: round(_coverage(quotes, field), 6)
        for field in ("pre_close", "avg_price", "turnover_ratio")
    }
    reference_field_coverage = {
        field: round(_coverage(security_reference, field), 6)
        for field in ("listing_date", "total_shares", "float_a_shares")
    }
    status_quote_codes = set(
        daily_status.loc[
            daily_status["quote_row_present"].fillna(False).astype(bool), "thscode"
        ].astype(str)
    )
    allowed_states = {"traded", "quote_without_turnover", "no_quote_observed"}
    observed_states = set(daily_status["observation_state"].dropna().astype(str))
    state_counts = {
        str(key): int(value)
        for key, value in daily_status.groupby("observation_state", dropna=False).size().items()
    }

    if universe_count < min_universe_size:
        errors.append(f"universe_count {universe_count} below minimum {min_universe_size}")
    if quote_coverage < min_quote_coverage:
        errors.append(f"quote_coverage {quote_coverage:.4f} below minimum {min_quote_coverage:.4f}")
    if reference_coverage < min_reference_coverage:
        errors.append(f"reference_coverage {reference_coverage:.4f} below minimum {min_reference_coverage:.4f}")
    for field, coverage in quote_field_coverage.items():
        if coverage < min_extended_field_coverage:
            errors.append(f"{field}_coverage {coverage:.4f} below minimum {min_extended_field_coverage:.4f}")
    for field, coverage in reference_field_coverage.items():
        if coverage < min_extended_field_coverage:
            errors.append(f"{field}_coverage {coverage:.4f} below minimum {min_extended_field_coverage:.4f}")
    for label, dates in (
        ("universe", universe_dates), ("security reference", reference_dates),
        ("quote", quote_dates), ("daily status", status_dates),
        ("calendar as-of", calendar_as_of_dates),
    ):
        if dates != [requested_trade_date]:
            errors.append(f"{label} source dates {dates} do not match {requested_trade_date}")
    if requested_trade_date not in open_calendar_dates:
        errors.append(f"requested date {requested_trade_date} is not open in SSE calendar")
    required_exchanges = {"SSE", "SZSE", "BSE"}
    if not required_exchanges.issubset(set(universe_exchanges)):
        errors.append("universe does not cover SSE, SZSE, and BSE")
    if not required_exchanges.issubset(set(quote_exchanges)):
        errors.append("quotes do not cover SSE, SZSE, and BSE")
    for count, message in (
        (universe_duplicates, "universe contains {} duplicate codes"),
        (reference_duplicates, "security reference contains {} duplicate codes"),
        (quote_duplicates, "quotes contain {} duplicate code/date rows"),
        (status_duplicates, "daily status contains {} duplicate code/date rows"),
    ):
        if count:
            errors.append(message.format(count))
    if quote_codes - universe_codes:
        errors.append(f"quotes contain {len(quote_codes - universe_codes)} codes outside universe")
    if reference_codes - universe_codes:
        errors.append(f"security reference contains {len(reference_codes - universe_codes)} codes outside universe")
    if universe_codes - status_codes or status_codes - universe_codes:
        errors.append(
            "daily status universe mismatch: "
            f"missing={len(universe_codes - status_codes)}, extra={len(status_codes - universe_codes)}"
        )
    if status_quote_codes != quote_codes:
        errors.append("daily status quote_row_present does not match quote codes")
    if not observed_states.issubset(allowed_states):
        errors.append("daily status contains unsupported observation states")
    for count, message in (
        (ohlc_invalid, "quotes contain {} invalid OHLC rows"),
        (nonpositive_prices, "quotes contain {} nonpositive price values"),
        (negative_volume, "quotes contain {} negative volume rows"),
        (negative_amount, "quotes contain {} negative amount rows"),
        (negative_turnover, "quotes contain {} negative turnover rows"),
        (avg_outside_range, "quotes contain {} average prices outside daily range"),
        (change_ratio_inconsistent, "quotes contain {} inconsistent change-ratio rows"),
        (future_listing_dates, "security reference contains {} future listing dates"),
        (negative_total_shares, "security reference contains {} negative total shares"),
        (negative_float_shares, "security reference contains {} negative float shares"),
        (float_exceeds_total, "security reference contains {} float shares above total shares"),
    ):
        if count:
            errors.append(message.format(count))

    drift, drift_warnings, drift_errors = _cross_day_drift(
        universe, security_reference, quotes, daily_status, previous
    )
    warnings.extend(drift_warnings)
    errors.extend(drift_errors)
    metrics: dict[str, Any] = {
        "requested_trade_date": requested_trade_date,
        "universe_count": universe_count,
        "reference_count": reference_count,
        "quote_count": quote_count,
        "status_count": len(status_codes),
        "quote_coverage": round(quote_coverage, 6),
        "reference_coverage": round(reference_coverage, 6),
        "quote_field_coverage": quote_field_coverage,
        "reference_field_coverage": reference_field_coverage,
        "universe_dates": universe_dates,
        "reference_dates": reference_dates,
        "quote_dates": quote_dates,
        "status_dates": status_dates,
        "calendar_as_of_dates": calendar_as_of_dates,
        "calendar_open_dates": sorted(open_calendar_dates),
        "universe_exchanges": universe_exchanges,
        "quote_exchanges": quote_exchanges,
        "universe_duplicates": universe_duplicates,
        "reference_duplicates": reference_duplicates,
        "quote_duplicates": quote_duplicates,
        "status_duplicates": status_duplicates,
        "unknown_quote_codes": len(quote_codes - universe_codes),
        "unknown_reference_codes": len(reference_codes - universe_codes),
        "complete_ohlc_rows": int(complete_ohlc.sum()),
        "invalid_ohlc_rows": ohlc_invalid,
        "nonpositive_price_values": nonpositive_prices,
        "negative_volume_rows": negative_volume,
        "negative_amount_rows": negative_amount,
        "negative_turnover_rows": negative_turnover,
        "average_price_outside_range_rows": avg_outside_range,
        "change_ratio_inconsistent_rows": change_ratio_inconsistent,
        "future_listing_date_rows": future_listing_dates,
        "negative_total_shares_rows": negative_total_shares,
        "negative_float_shares_rows": negative_float_shares,
        "float_exceeds_total_rows": float_exceeds_total,
        "observation_state_counts": state_counts,
        "drift": drift,
    }
    return QualityReport(
        ok=not errors, metrics=metrics, errors=errors, warnings=warnings
    )


def _number(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def build_market_summary(
    quotes: pd.DataFrame,
    trade_date: str,
    daily_status: pd.DataFrame | None = None,
) -> dict[str, Any]:
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
    state_counts: dict[str, int] = {}
    if daily_status is not None:
        state_counts = {
            str(key): int(value)
            for key, value in daily_status.groupby("observation_state", dropna=False).size().items()
        }
    return {
        "schema_version": 2,
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
        "observation_state_counts": state_counts,
    }


__all__ = [
    "CALENDAR_COLUMNS", "PRICE_COMPARISON_TOLERANCE", "QUOTE_COLUMNS",
    "REFERENCE_COLUMNS", "STATUS_COLUMNS",
    "QualityReport", "build_daily_security_status", "build_market_summary", "frame_hash",
    "normalize_quotes", "normalize_security_reference", "normalize_trade_calendar",
    "normalize_universe", "schema_signature", "validate_data",
]
