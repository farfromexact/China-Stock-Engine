"""Point-in-time derived data, tradability facts, and reference contracts."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from .storage import json_sha256, publish_data_reference_artifacts


DATA_REFERENCE_SCHEMA_VERSION = 2
STOCK_STATE_SCHEMA_VERSION = 2
MAX_DATA_REFERENCE_BYTES = 2 * 1024 * 1024
TARGET_HISTORY_SESSIONS = 252
MIN_ADJUSTMENT_COVERAGE = 0.98
MIN_INDUSTRY_COVERAGE = 0.95
MIN_TRADABILITY_COVERAGE = 0.95
DEFAULT_MIN_ADT20 = 20_000_000.0
DEFAULT_MIN_LISTING_AGE_DAYS = 30
READINESS_STATES = {"ready", "stale", "missing", "not_entitled"}

ADJUSTMENT_REQUIRED_COLUMNS = {
    "trade_date",
    "thscode",
    "adj_factor",
    "known_at",
}
CORPORATE_ACTION_REQUIRED_COLUMNS = {
    "thscode",
    "event_type",
    "published_at",
    "effective_at",
    "known_at",
}
INDUSTRY_REQUIRED_COLUMNS = {
    "thscode",
    "classification_system",
    "level",
    "industry_code",
    "industry_name",
    "effective_from",
    "effective_to",
    "known_at",
}
INDEX_MEMBERSHIP_REQUIRED_COLUMNS = {
    "index_code",
    "index_name",
    "thscode",
    "weight",
    "effective_from",
    "effective_to",
    "known_at",
}
PROVIDER_TRADABILITY_REQUIRED_COLUMNS = {
    "as_of_date",
    "thscode",
    "is_st",
    "is_suspended",
    "daily_price_limit_pct",
    "lot_size",
    "known_at",
}

STOCK_STATE_COLUMNS = (
    "schema_version",
    "trade_date",
    "data_cutoff_time",
    "thscode",
    "security_name",
    "exchange",
    "board",
    "raw_close",
    "adj_factor",
    "forward_adj_close",
    "adjusted_close",
    "total_return_index",
    "corporate_action_flag",
    "corporate_action_types",
    "adjusted_ready",
    "return_1d_pct",
    "return_3d_pct",
    "return_5d_pct",
    "return_20d_pct",
    "return_60d_pct",
    "rv20_pct",
    "rv60_pct",
    "turnover_z20",
    "amount_z20",
    "volume_z20",
    "gap_pct",
    "intraday_range_pct",
    "close_location",
    "close_vs_avg_pct",
    "turnover_change_pct",
    "amount_change_pct",
    "float_market_cap",
    "total_market_cap",
    "distance_from_high_20_pct",
    "distance_from_high_60_pct",
    "drawdown_from_high_252_pct",
    "relative_return_industry_20d_pct",
    "relative_return_csi300_20d_pct",
    "relative_return_csi1000_20d_pct",
    "sw1_code",
    "sw1_name",
    "sw2_code",
    "sw2_name",
    "index_memberships",
    "history_sessions",
    "history_start_date",
    "listing_age_calendar_days",
    "adt20",
    "is_st",
    "is_suspended",
    "limit_up",
    "limit_down",
    "one_word_limit",
    "daily_price_limit_pct",
    "lot_size",
    "stock_connect_eligible",
    "margin_eligible",
    "short_sell_eligible",
    "tradability_state",
    "tradability_reason_codes",
    "source_snapshot_sha256",
)

def data_cutoff_time_for_date(trade_date: str) -> str:
    return f"{trade_date}T20:15:00+08:00"


def _as_utc_timestamp(value: str) -> pd.Timestamp:
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize("Asia/Shanghai")
    return parsed.tz_convert("UTC")


def _known_by(frame: pd.DataFrame, decision_time: str) -> pd.Series:
    if "known_at" not in frame.columns:
        return pd.Series(False, index=frame.index, dtype=bool)
    values = pd.to_datetime(frame["known_at"], errors="coerce", utc=True)
    return values.notna() & values.le(_as_utc_timestamp(decision_time))


def _normalize_date_column(frame: pd.DataFrame, column: str) -> None:
    frame[column] = pd.to_datetime(
        frame[column].astype(str), errors="coerce"
    ).dt.strftime("%Y-%m-%d")


def _require_columns(
    frame: pd.DataFrame, required: set[str], label: str
) -> pd.DataFrame:
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"{label} missing columns: " + ", ".join(missing))
    return frame.copy()


def normalize_adjustments(frame: pd.DataFrame) -> pd.DataFrame:
    output = _require_columns(frame, ADJUSTMENT_REQUIRED_COLUMNS, "adjustments")
    _normalize_date_column(output, "trade_date")
    output["thscode"] = output["thscode"].astype("string").str.upper().str.strip()
    output["adj_factor"] = pd.to_numeric(output["adj_factor"], errors="coerce")
    output = output.loc[output["adj_factor"].gt(0)].copy()
    return output.sort_values(["trade_date", "thscode", "known_at"]).reset_index(
        drop=True
    )


def normalize_corporate_actions(frame: pd.DataFrame) -> pd.DataFrame:
    output = _require_columns(
        frame, CORPORATE_ACTION_REQUIRED_COLUMNS, "corporate actions"
    )
    output["thscode"] = output["thscode"].astype("string").str.upper().str.strip()
    output["event_type"] = output["event_type"].astype("string").str.strip()
    _normalize_date_column(output, "effective_at")
    return output.sort_values(["effective_at", "thscode", "known_at"]).reset_index(
        drop=True
    )


def normalize_industry_membership(frame: pd.DataFrame) -> pd.DataFrame:
    output = _require_columns(
        frame, INDUSTRY_REQUIRED_COLUMNS, "industry membership"
    )
    output["thscode"] = output["thscode"].astype("string").str.upper().str.strip()
    for column in ("effective_from", "effective_to"):
        _normalize_date_column(output, column)
    for column in (
        "classification_system",
        "level",
        "industry_code",
        "industry_name",
    ):
        output[column] = output[column].astype("string").str.strip()
    return output.reset_index(drop=True)


def normalize_index_membership(frame: pd.DataFrame) -> pd.DataFrame:
    output = _require_columns(
        frame, INDEX_MEMBERSHIP_REQUIRED_COLUMNS, "index membership"
    )
    output["thscode"] = output["thscode"].astype("string").str.upper().str.strip()
    output["index_code"] = output["index_code"].astype("string").str.upper().str.strip()
    output["index_name"] = output["index_name"].astype("string").str.strip()
    output["weight"] = pd.to_numeric(output["weight"], errors="coerce")
    for column in ("effective_from", "effective_to"):
        _normalize_date_column(output, column)
    return output.reset_index(drop=True)


def normalize_provider_tradability(frame: pd.DataFrame) -> pd.DataFrame:
    output = _require_columns(
        frame, PROVIDER_TRADABILITY_REQUIRED_COLUMNS, "provider tradability"
    )
    _normalize_date_column(output, "as_of_date")
    output["thscode"] = output["thscode"].astype("string").str.upper().str.strip()
    for column in (
        "is_st",
        "is_suspended",
        "stock_connect_eligible",
        "margin_eligible",
        "short_sell_eligible",
    ):
        if column not in output.columns:
            output[column] = pd.NA
        output[column] = output[column].astype("boolean")
    for column in ("daily_price_limit_pct", "lot_size"):
        output[column] = pd.to_numeric(output[column], errors="coerce")
    return output.reset_index(drop=True)


def _partition_value(path: Path, prefix: str) -> str | None:
    for part in path.parts:
        if part.startswith(prefix):
            return part.split("=", 1)[1]
    return None


def load_market_history(
    data_dir: Path, end_date: str, *, limit: int = TARGET_HISTORY_SESSIONS
) -> pd.DataFrame:
    paths = sorted(
        (data_dir / "facts" / "market").glob(
            "trade_date=*/daily_quotes.parquet"
        )
    )
    eligible = [
        path
        for path in paths
        if (_partition_value(path, "trade_date=") or "") <= end_date
    ]
    if not eligible:
        eligible = sorted(
            path
            for path in (data_dir / "snapshots").glob("*/daily_quotes.parquet")
            if path.parent.name <= end_date
        )
    selected_dates = sorted(
        {
            _partition_value(path, "trade_date=") or path.parent.name
            for path in eligible
        }
    )[-limit:]
    selected = [
        path
        for path in eligible
        if (_partition_value(path, "trade_date=") or path.parent.name)
        in selected_dates
    ]
    if not selected:
        return pd.DataFrame()
    frames = [pd.read_parquet(path) for path in selected]
    output = pd.concat(frames, ignore_index=True)
    _normalize_date_column(output, "trade_date")
    output["thscode"] = output["thscode"].astype("string").str.upper().str.strip()
    numeric = (
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "avg_price",
        "volume",
        "amount",
        "turnover_ratio",
        "change_ratio",
    )
    for column in numeric:
        if column in output.columns:
            output[column] = pd.to_numeric(output[column], errors="coerce")
    return (
        output.loc[output["trade_date"].le(end_date)]
        .drop_duplicates(["trade_date", "thscode"], keep="last")
        .sort_values(["thscode", "trade_date"])
        .reset_index(drop=True)
    )


def load_index_history(
    data_dir: Path, end_date: str, *, limit: int = TARGET_HISTORY_SESSIONS
) -> pd.DataFrame:
    paths = sorted(
        (data_dir / "facts" / "index").glob(
            "trade_date=*/index_quotes.parquet"
        )
    )
    eligible = [
        path
        for path in paths
        if (_partition_value(path, "trade_date=") or "") <= end_date
    ]
    selected_dates = sorted(
        {_partition_value(path, "trade_date=") or "" for path in eligible}
    )[-limit:]
    selected = [
        path
        for path in eligible
        if (_partition_value(path, "trade_date=") or "") in selected_dates
    ]
    if not selected:
        return pd.DataFrame()
    output = pd.concat([pd.read_parquet(path) for path in selected], ignore_index=True)
    _normalize_date_column(output, "trade_date")
    output["thscode"] = output["thscode"].astype("string").str.upper().str.strip()
    for column in ("open", "high", "low", "close"):
        if column in output.columns:
            output[column] = pd.to_numeric(output[column], errors="coerce")
    return (
        output.drop_duplicates(["trade_date", "thscode"], keep="last")
        .sort_values(["thscode", "trade_date"])
        .reset_index(drop=True)
    )


def _latest_partition_frame(
    data_dir: Path,
    category: str,
    file_name: str,
    as_of_date: str,
) -> pd.DataFrame:
    paths = sorted((data_dir / "facts" / category).glob(f"as_of_date=*/{file_name}"))
    eligible = [
        path
        for path in paths
        if (_partition_value(path, "as_of_date=") or "") <= as_of_date
    ]
    if eligible:
        return pd.read_parquet(eligible[-1])
    latest_path = data_dir / "latest" / file_name
    return pd.read_parquet(latest_path) if latest_path.exists() else pd.DataFrame()


def load_adjustment_snapshot(data_dir: Path, as_of_date: str) -> pd.DataFrame:
    """Load the newest adjustment snapshot that existed by ``as_of_date``."""

    return _latest_partition_frame(
        data_dir, "adjustment", "adjustment_factors.parquet", as_of_date
    )


def load_module_status(data_dir: Path, as_of_date: str) -> dict[str, Any]:
    paths = sorted(
        (data_dir / "facts" / "module_status").glob(
            "as_of_date=*/module_status.json"
        )
    )
    eligible = [
        path
        for path in paths
        if (_partition_value(path, "as_of_date=") or "") <= as_of_date
    ]
    return _read_json(eligible[-1]) if eligible else {}


def _active_intervals(
    frame: pd.DataFrame, as_of_date: str, decision_time: str
) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    start = frame["effective_from"].fillna("").astype(str).le(as_of_date)
    end = frame["effective_to"].isna() | frame["effective_to"].astype(str).ge(
        as_of_date
    )
    return frame.loc[start & end & _known_by(frame, decision_time)].copy()


def _rolling_z(values: pd.Series, window: int) -> pd.Series:
    rolling = values.rolling(window, min_periods=window)
    mean = rolling.mean()
    deviation = rolling.std(ddof=0).replace(0, pd.NA)
    return (values - mean) / deviation


def _pct_change(values: pd.Series, periods: int = 1) -> pd.Series:
    return values.pct_change(periods=periods, fill_method=None) * 100.0


def _safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    return numerator / denominator.where(denominator.ne(0))


def apply_adjustments(
    history: pd.DataFrame,
    adjustments: pd.DataFrame,
    decision_time: str,
) -> pd.DataFrame:
    output = history.copy()
    output["raw_close"] = pd.to_numeric(output["close"], errors="coerce")
    if adjustments.empty:
        output["adj_factor"] = float("nan")
    else:
        normalized = normalize_adjustments(adjustments)
        normalized = normalized.loc[_known_by(normalized, decision_time)].copy()
        normalized = normalized.sort_values("known_at").drop_duplicates(
            ["trade_date", "thscode"], keep="last"
        )
        output = output.merge(
            normalized.loc[:, ["trade_date", "thscode", "adj_factor"]],
            how="left",
            on=["trade_date", "thscode"],
            validate="one_to_one",
        )
    output["adjusted_close"] = output["raw_close"] * pd.to_numeric(
        output["adj_factor"], errors="coerce"
    )
    output["forward_adj_close"] = output["adjusted_close"]
    output["adjusted_ready"] = output["adjusted_close"].notna()

    def total_return(series: pd.Series) -> pd.Series:
        first = series.dropna()
        if first.empty or first.iloc[0] == 0:
            return pd.Series(pd.NA, index=series.index, dtype="Float64")
        return series / first.iloc[0] * 100.0

    output["total_return_index"] = output.groupby("thscode", group_keys=False)[
        "adjusted_close"
    ].apply(total_return)
    return output


def _current_reference(data_dir: Path, trade_date: str) -> pd.DataFrame:
    return _latest_partition_frame(
        data_dir, "reference", "security_reference.parquet", trade_date
    )


def build_tradability_state(
    history: pd.DataFrame,
    reference: pd.DataFrame,
    daily_status: pd.DataFrame,
    provider_tradability: pd.DataFrame,
    trade_date: str,
    decision_time: str,
    *,
    min_adt20: float = DEFAULT_MIN_ADT20,
    min_listing_age_days: int = DEFAULT_MIN_LISTING_AGE_DAYS,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    current = history.loc[history["trade_date"].eq(trade_date)].copy()
    current = current.sort_values("thscode").drop_duplicates("thscode", keep="last")
    amount_history = history.loc[:, ["trade_date", "thscode", "amount"]].copy()
    amount_history["amount"] = pd.to_numeric(amount_history["amount"], errors="coerce")
    amount_history["adt20"] = amount_history.groupby("thscode")["amount"].transform(
        lambda values: values.rolling(20, min_periods=20).mean()
    )
    current = current.merge(
        amount_history.loc[
            amount_history["trade_date"].eq(trade_date), ["thscode", "adt20"]
        ],
        how="left",
        on="thscode",
        validate="one_to_one",
    )
    reference_fields = reference.loc[
        :, ["thscode", "listing_date", "total_shares", "float_a_shares"]
    ].drop_duplicates("thscode", keep="last")
    current = current.merge(
        reference_fields, how="left", on="thscode", validate="one_to_one"
    )
    status_fields = daily_status.loc[
        :, ["thscode", "observation_state"]
    ].drop_duplicates("thscode", keep="last")
    current = current.merge(
        status_fields, how="left", on="thscode", validate="one_to_one"
    )

    normalized_provider = pd.DataFrame()
    if not provider_tradability.empty:
        normalized_provider = normalize_provider_tradability(provider_tradability)
        normalized_provider = normalized_provider.loc[
            normalized_provider["as_of_date"].eq(trade_date)
            & _known_by(normalized_provider, decision_time)
        ].copy()
        normalized_provider = normalized_provider.sort_values("known_at").drop_duplicates(
            "thscode", keep="last"
        )
    provider_columns = [
        "thscode",
        "is_st",
        "is_suspended",
        "daily_price_limit_pct",
        "lot_size",
        "stock_connect_eligible",
        "margin_eligible",
        "short_sell_eligible",
    ]
    if normalized_provider.empty:
        provider = pd.DataFrame({"thscode": current["thscode"]})
        for column in provider_columns[1:]:
            provider[column] = pd.NA
    else:
        provider = normalized_provider.reindex(columns=provider_columns)
    current = current.merge(provider, how="left", on="thscode", validate="one_to_one")

    listing = pd.to_datetime(current["listing_date"], errors="coerce")
    current["listing_age_calendar_days"] = (
        pd.Timestamp(trade_date) - listing
    ).dt.days.astype("Int64")
    limit = pd.to_numeric(current["daily_price_limit_pct"], errors="coerce")
    change = pd.to_numeric(current["change_ratio"], errors="coerce")
    current["limit_up"] = limit.notna() & change.ge(limit - 0.05)
    current["limit_down"] = limit.notna() & change.le(-limit + 0.05)
    flat_price = (
        current[["open", "high", "low", "close"]].notna().all(axis=1)
        & current["high"].sub(current["low"]).abs().le(1e-8)
        & current["open"].sub(current["close"]).abs().le(1e-8)
    )
    current["one_word_limit"] = flat_price & (
        current["limit_up"] | current["limit_down"]
    )

    mandatory_known = (
        current["is_st"].notna()
        & current["is_suspended"].notna()
        & current["daily_price_limit_pct"].notna()
        & current["lot_size"].notna()
    )

    def reasons(row: pd.Series) -> list[str]:
        values: list[str] = []
        if pd.isna(row["is_st"]) or pd.isna(row["is_suspended"]):
            values.append("provider_tradability_missing")
        if pd.isna(row["daily_price_limit_pct"]) or pd.isna(row["lot_size"]):
            values.append("market_rule_missing")
        observation_state = row.get("observation_state")
        if pd.isna(observation_state) or str(observation_state) != "traded":
            values.append("quote_observation_not_traded")
        is_st = row.get("is_st")
        if not pd.isna(is_st) and bool(is_st):
            values.append("st_security")
        is_suspended = row.get("is_suspended")
        if not pd.isna(is_suspended) and bool(is_suspended):
            values.append("provider_suspended")
        if bool(row.get("one_word_limit")):
            values.append("one_word_limit")
        listing_age = row.get("listing_age_calendar_days")
        if pd.isna(listing_age) or int(listing_age) < min_listing_age_days:
            values.append("listing_age_below_minimum")
        adt20 = row.get("adt20")
        if pd.isna(adt20) or float(adt20) < min_adt20:
            values.append("adt20_below_minimum")
        return values

    current["tradability_reason_codes"] = current.apply(reasons, axis=1)
    current["tradability_state"] = "restricted"
    current.loc[~mandatory_known, "tradability_state"] = "unknown"
    current.loc[
        mandatory_known & current["tradability_reason_codes"].map(len).eq(0),
        "tradability_state",
    ] = "clear"
    provider_coverage = (
        float(mandatory_known.mean()) if len(mandatory_known) else 0.0
    )
    readiness = {
        "state": "ready"
        if provider_coverage >= MIN_TRADABILITY_COVERAGE
        else "missing",
        "coverage": round(provider_coverage, 6),
        "clear_count": int(current["tradability_state"].eq("clear").sum()),
        "restricted_count": int(
            current["tradability_state"].eq("restricted").sum()
        ),
        "unknown_count": int(current["tradability_state"].eq("unknown").sum()),
        "unknown_is_not_suspension": True,
    }
    return current, readiness


def _industry_for_date(
    membership: pd.DataFrame, trade_date: str, decision_time: str
) -> pd.DataFrame:
    if membership.empty:
        return membership.copy()
    normalized = normalize_industry_membership(membership)
    active = _active_intervals(normalized, trade_date, decision_time)
    active = active.loc[active["classification_system"].str.upper().eq("SW")]
    active = active.sort_values("known_at").drop_duplicates(
        ["thscode", "level"], keep="last"
    )
    return active


def _index_for_date(
    membership: pd.DataFrame, trade_date: str, decision_time: str
) -> pd.DataFrame:
    if membership.empty:
        return membership.copy()
    normalized = normalize_index_membership(membership)
    active = _active_intervals(normalized, trade_date, decision_time)
    return active.sort_values("known_at").drop_duplicates(
        ["index_code", "thscode"], keep="last"
    )


def build_stock_state(
    history: pd.DataFrame,
    reference: pd.DataFrame,
    daily_status: pd.DataFrame,
    adjustments: pd.DataFrame,
    corporate_actions: pd.DataFrame,
    industry_membership: pd.DataFrame,
    index_membership: pd.DataFrame,
    index_history: pd.DataFrame,
    provider_tradability: pd.DataFrame,
    trade_date: str,
    source_snapshot_sha256: str,
    *,
    decision_time: str | None = None,
    min_adt20: float = DEFAULT_MIN_ADT20,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if history.empty:
        return pd.DataFrame(columns=STOCK_STATE_COLUMNS), {
            "history": {"state": "missing", "sessions": 0},
            "adjustment": {"state": "missing", "coverage": 0.0},
            "industry": {"state": "missing", "coverage": 0.0},
            "index_membership": {"state": "missing", "index_count": 0},
            "tradability": {"state": "missing", "coverage": 0.0},
            "stock_state": {"state": "missing", "rows": 0},
        }
    decision_time = decision_time or data_cutoff_time_for_date(trade_date)
    scoped = history.loc[history["trade_date"].le(trade_date)].copy()
    scoped = scoped.sort_values(["thscode", "trade_date"]).reset_index(drop=True)
    adjusted = apply_adjustments(scoped, adjustments, decision_time)
    grouped = adjusted.groupby("thscode", group_keys=False)
    adjusted["history_sessions"] = grouped.cumcount() + 1
    adjusted["history_start_date"] = grouped["trade_date"].transform("min")
    for periods in (1, 3, 5, 20, 60):
        adjusted[f"return_{periods}d_pct"] = grouped["adjusted_close"].transform(
            lambda values, p=periods: _pct_change(values, p)
        )
    adjusted["daily_adjusted_return_pct"] = grouped["adjusted_close"].transform(
        _pct_change
    )
    for window in (20, 60):
        adjusted[f"rv{window}_pct"] = grouped[
            "daily_adjusted_return_pct"
        ].transform(
            lambda values, w=window: values.rolling(w, min_periods=w).std()
            * math.sqrt(252)
        )
    for column in ("turnover_ratio", "amount", "volume"):
        adjusted[f"{column}_z20"] = grouped[column].transform(_rolling_z, window=20)
    adjusted["gap_pct"] = (
        _safe_ratio(adjusted["open"], adjusted["pre_close"]) - 1
    ) * 100
    adjusted["intraday_range_pct"] = (
        _safe_ratio(adjusted["high"], adjusted["low"]) - 1
    ) * 100
    adjusted["close_location"] = _safe_ratio(
        adjusted["close"] - adjusted["low"], adjusted["high"] - adjusted["low"]
    )
    adjusted["close_vs_avg_pct"] = (
        _safe_ratio(adjusted["close"], adjusted["avg_price"]) - 1
    ) * 100
    adjusted["turnover_change_pct"] = grouped["turnover_ratio"].transform(_pct_change)
    adjusted["amount_change_pct"] = grouped["amount"].transform(_pct_change)
    for window, target in (
        (20, "distance_from_high_20_pct"),
        (60, "distance_from_high_60_pct"),
        (252, "drawdown_from_high_252_pct"),
    ):
        rolling_high = grouped["adjusted_close"].transform(
            lambda values, w=window: values.rolling(w, min_periods=w).max()
        )
        adjusted[target] = (
            _safe_ratio(adjusted["adjusted_close"], rolling_high) - 1
        ) * 100

    current = adjusted.loc[adjusted["trade_date"].eq(trade_date)].copy()
    if current.empty:
        raise ValueError(f"market history does not contain requested date {trade_date}")
    reference_current = reference.sort_values("as_of_date").drop_duplicates(
        "thscode", keep="last"
    )
    current = current.merge(
        reference_current.loc[:, ["thscode", "total_shares", "float_a_shares"]],
        how="left",
        on="thscode",
        validate="one_to_one",
        suffixes=("", "_reference"),
    )
    current["float_market_cap"] = current["raw_close"] * current["float_a_shares"]
    current["total_market_cap"] = current["raw_close"] * current["total_shares"]

    industry = _industry_for_date(industry_membership, trade_date, decision_time)
    for level, prefix in (("SW1", "sw1"), ("SW2", "sw2")):
        if industry.empty:
            selected = pd.DataFrame(
                columns=["thscode", f"{prefix}_code", f"{prefix}_name"]
            )
        else:
            selected = industry.loc[
                industry["level"].str.upper().eq(level),
                ["thscode", "industry_code", "industry_name"],
            ].rename(
                columns={
                    "industry_code": f"{prefix}_code",
                    "industry_name": f"{prefix}_name",
                }
            )
        current = current.merge(
            selected, how="left", on="thscode", validate="one_to_one"
        )
    current["relative_return_industry_20d_pct"] = pd.NA
    if "sw1_code" in current.columns:
        industry_mean = current.groupby("sw1_code", dropna=True)[
            "return_20d_pct"
        ].transform("mean")
        current["relative_return_industry_20d_pct"] = (
            current["return_20d_pct"] - industry_mean
        )
    benchmark_returns: dict[str, float | None] = {
        "000300.SH": None,
        "000852.SH": None,
    }
    if not index_history.empty:
        index_scoped = index_history.loc[index_history["trade_date"].le(trade_date)].copy()
        for code in benchmark_returns:
            values = index_scoped.loc[
                index_scoped["thscode"].eq(code), "close"
            ].dropna()
            if len(values) >= 21 and values.iloc[-21] > 0:
                benchmark_returns[code] = float(
                    (values.iloc[-1] / values.iloc[-21] - 1) * 100
                )
    current["relative_return_csi300_20d_pct"] = (
        current["return_20d_pct"] - benchmark_returns["000300.SH"]
        if benchmark_returns["000300.SH"] is not None
        else pd.NA
    )
    current["relative_return_csi1000_20d_pct"] = (
        current["return_20d_pct"] - benchmark_returns["000852.SH"]
        if benchmark_returns["000852.SH"] is not None
        else pd.NA
    )

    current["corporate_action_flag"] = pd.NA
    current["corporate_action_types"] = current["thscode"].map(lambda _: [])
    if not corporate_actions.empty:
        actions = normalize_corporate_actions(corporate_actions)
        actions = actions.loc[
            actions["effective_at"].eq(trade_date)
            & _known_by(actions, decision_time)
        ]
        action_map = {
            str(code): sorted(set(group["event_type"].dropna().astype(str)))
            for code, group in actions.groupby("thscode")
        }
        current["corporate_action_types"] = current["thscode"].map(
            lambda value: action_map.get(str(value), [])
        )
        current["corporate_action_flag"] = current[
            "corporate_action_types"
        ].map(bool)

    indexes = _index_for_date(index_membership, trade_date, decision_time)
    membership_map: dict[str, list[str]] = {}
    if not indexes.empty:
        membership_map = {
            str(code): sorted(set(group["index_code"].dropna().astype(str)))
            for code, group in indexes.groupby("thscode")
        }
    current["index_memberships"] = current["thscode"].map(
        lambda value: membership_map.get(str(value), [])
    )

    tradability, tradability_readiness = build_tradability_state(
        scoped,
        reference,
        daily_status,
        provider_tradability,
        trade_date,
        decision_time,
        min_adt20=min_adt20,
    )
    tradability_columns = [
        "thscode",
        "listing_age_calendar_days",
        "adt20",
        "is_st",
        "is_suspended",
        "limit_up",
        "limit_down",
        "one_word_limit",
        "daily_price_limit_pct",
        "lot_size",
        "stock_connect_eligible",
        "margin_eligible",
        "short_sell_eligible",
        "tradability_state",
        "tradability_reason_codes",
    ]
    current = current.merge(
        tradability.loc[:, tradability_columns],
        how="left",
        on="thscode",
        validate="one_to_one",
    )
    current["schema_version"] = STOCK_STATE_SCHEMA_VERSION
    current["data_cutoff_time"] = decision_time
    current["source_snapshot_sha256"] = source_snapshot_sha256
    current = current.rename(
        columns={
            "close": "raw_close_source",
            "turnover_ratio_z20": "turnover_z20",
        }
    )
    current["volume_z20"] = current.get("volume_z20", pd.Series(pd.NA, index=current.index))
    current["amount_z20"] = current.get("amount_z20", pd.Series(pd.NA, index=current.index))
    for column in ("sw1_code", "sw1_name", "sw2_code", "sw2_name"):
        if column not in current.columns:
            current[column] = pd.NA
    current = current.rename(columns={"turnover_ratio_z20": "turnover_z20"})
    current = current.replace([float("inf"), float("-inf")], pd.NA)
    state = current.reindex(columns=STOCK_STATE_COLUMNS).sort_values("thscode")

    session_count = int(scoped["trade_date"].nunique())
    current_adjustment_coverage = float(state["adjusted_ready"].fillna(False).mean())
    sw1_coverage = float(state["sw1_code"].notna().mean()) if len(state) else 0.0
    index_count = int(indexes["index_code"].nunique()) if not indexes.empty else 0
    history_state = "ready" if session_count >= TARGET_HISTORY_SESSIONS else "missing"
    adjustment_state = (
        "ready"
        if current_adjustment_coverage >= MIN_ADJUSTMENT_COVERAGE
        else "missing"
    )
    industry_state = "ready" if sw1_coverage >= MIN_INDUSTRY_COVERAGE else "missing"
    stock_state_ready = (
        history_state == "ready"
        and adjustment_state == "ready"
        and tradability_readiness["state"] == "ready"
    )
    readiness = {
        "history": {
            "state": history_state,
            "sessions": session_count,
            "target_sessions": TARGET_HISTORY_SESSIONS,
            "scope_end": trade_date,
        },
        "adjustment": {
            "state": adjustment_state,
            "coverage": round(current_adjustment_coverage, 6),
            "method": "point_in_time_factor_input",
        },
        "corporate_actions": {
            "state": "ready" if not corporate_actions.empty else "missing",
            "events_effective_today": int(
                state["corporate_action_flag"].fillna(False).sum()
            ),
        },
        "industry": {
            "state": industry_state,
            "coverage": round(sw1_coverage, 6),
            "classification": "SW",
        },
        "index_membership": {
            "state": "ready" if index_count else "missing",
            "index_count": index_count,
        },
        "index_prices": {
            "state": "ready"
            if all(value is not None for value in benchmark_returns.values())
            else "missing",
            "benchmarks": {
                "CSI300": _finite(benchmark_returns["000300.SH"]),
                "CSI1000": _finite(benchmark_returns["000852.SH"]),
            },
        },
        "tradability": tradability_readiness,
        "stock_state": {
            "state": "ready" if stock_state_ready else "missing",
            "rows": int(len(state)),
            "tradability_state_counts": {
                str(key): int(value)
                for key, value in state["tradability_state"]
                .value_counts(dropna=False)
                .items()
            },
        },
    }
    return state.reset_index(drop=True), readiness


def _finite(value: Any, digits: int | None = 6) -> float | None:
    if value is None or pd.isna(value):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return round(number, digits) if digits is not None else number


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        return _finite(value)
    return value


def _industry_summary(stock_state: pd.DataFrame) -> list[dict[str, Any]]:
    scoped = stock_state.loc[stock_state["sw1_name"].notna()].copy()
    if scoped.empty:
        return []
    scoped["positive_20d"] = pd.to_numeric(
        scoped["return_20d_pct"], errors="coerce"
    ).gt(0)
    grouped = scoped.groupby(["sw1_code", "sw1_name"], dropna=True).agg(
        member_count=("thscode", "nunique"),
        observed_return_count=("return_20d_pct", "count"),
        mean_return_20d_pct=("return_20d_pct", "mean"),
        median_return_20d_pct=("return_20d_pct", "median"),
        positive_breadth_20d=("positive_20d", "mean"),
        adt20_sum=("adt20", "sum"),
    )
    grouped = grouped.reset_index().sort_values(["sw1_code", "sw1_name"])
    return [_json_value(item) for item in grouped.to_dict(orient="records")]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _source_snapshot_hash(manifest: dict[str, Any]) -> str:
    derived = {
        "stock_state.parquet",
        "data_reference_latest.json",
    }
    artifacts = {
        key: value
        for key, value in (manifest.get("artifacts") or {}).items()
        if key not in derived
    }
    return json_sha256(
        {
            "trade_date": manifest.get("trade_date"),
            "provenance": manifest.get("provenance") or {},
            "artifacts": artifacts,
        }
    )


def _field_coverage(stock_state: pd.DataFrame) -> list[dict[str, Any]]:
    row_count = int(len(stock_state))
    output: list[dict[str, Any]] = []
    for field in STOCK_STATE_COLUMNS:
        series = stock_state.get(field, pd.Series(dtype=object))
        non_null = int(series.notna().sum())
        output.append(
            {
                "field": field,
                "non_null_count": non_null,
                "row_count": row_count,
                "coverage_ratio": round(non_null / row_count, 6)
                if row_count
                else 0.0,
            }
        )
    return output


def _data_catalog(
    manifest: dict[str, Any], trade_date: str, stock_state_rows: int
) -> list[dict[str, Any]]:
    descriptions = {
        "universe.parquet": "PIT A-share security universe",
        "security_reference.parquet": "security names, boards, listing and share data",
        "daily_quotes.parquet": "unadjusted daily OHLCV and turnover facts",
        "trading_calendar.parquet": "SSE trading-calendar observations",
        "daily_security_status.parquet": "quote observation status by security",
        "market_summary.json": "factual market breadth and turnover aggregates",
    }
    catalog: list[dict[str, Any]] = []
    excluded = {
        "stock_state.parquet",
        "data_reference_latest.json",
    }
    for name, metadata in sorted((manifest.get("artifacts") or {}).items()):
        if name in excluded:
            continue
        catalog.append(
            {
                "name": name,
                "layer": "snapshot",
                "path": f"snapshots/{trade_date}/{name}",
                "rows": (metadata or {}).get("rows"),
                "sha256": (metadata or {}).get("sha256"),
                "description": descriptions.get(name, "verified snapshot artifact"),
            }
        )
    catalog.append(
        {
            "name": "stock_state.parquet",
            "layer": "derived_feature",
            "path": (
                f"features/stock_state/trade_date={trade_date}/stock_state.parquet"
            ),
            "rows": stock_state_rows,
            "sha256": None,
            "description": "PIT per-security adjusted-price and market-state fields",
        }
    )
    return catalog


def build_data_reference(
    manifest: dict[str, Any],
    market_summary: dict[str, Any],
    stock_state: pd.DataFrame,
    readiness: dict[str, Any],
    source_snapshot_sha256: str,
) -> dict[str, Any]:
    trade_date = str(manifest.get("trade_date") or "")
    factual_readiness = dict(readiness)
    for item in factual_readiness.values():
        if item.get("state") not in READINESS_STATES:
            raise ValueError(f"unsupported readiness state: {item.get('state')}")
    reference = {
        "schema_version": DATA_REFERENCE_SCHEMA_VERSION,
        "engine": "China-Stock-Engine",
        "document_type": "a_share_data_reference",
        "as_of": {
            "source_trade_date": trade_date,
            "data_cutoff_time": data_cutoff_time_for_date(trade_date),
            "timezone": "Asia/Shanghai",
            "generated_at_utc": manifest.get("collected_at_utc")
            or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "run": {
            "state": "ready"
            if manifest.get("verified") is True and manifest.get("data_fresh") is True
            else "stale",
            "data_fresh": bool(manifest.get("data_fresh")),
            "source_snapshot_sha256": source_snapshot_sha256,
            "provider": manifest.get("provider"),
            "raw_payload_persisted": False,
        },
        "quality": {
            "verified": manifest.get("verified") is True,
            "quality_gate": manifest.get("quality") or {},
            "quality_thresholds": manifest.get("quality_thresholds") or {},
        },
        "readiness": _json_value(factual_readiness),
        "market_snapshot": {
            "breadth": {
                "advancers": market_summary.get("advancers"),
                "decliners": market_summary.get("decliners"),
                "unchanged": market_summary.get("unchanged"),
                "equal_weight_change_pct": market_summary.get(
                    "equal_weight_change_pct"
                ),
                "median_change_pct": market_summary.get("median_change_pct"),
            },
            "total_amount": market_summary.get("total_amount"),
            "quoted_securities": market_summary.get("quoted_securities"),
            "observation_state_counts": market_summary.get(
                "observation_state_counts", {}
            ),
            "industry_summary": _industry_summary(stock_state),
            "index_reference": (factual_readiness.get("index_prices") or {}).get(
                "benchmarks"
            ),
        },
        "coverage": {
            "stock_state_rows": int(len(stock_state)),
            "fields": _field_coverage(stock_state),
        },
        "data_catalog": _data_catalog(manifest, trade_date, int(len(stock_state))),
        "drilldown": {
            "stock_state": f"features/stock_state/trade_date={trade_date}/stock_state.parquet",
            "market_facts": f"facts/market/trade_date={trade_date}/daily_quotes.parquet",
            "security_status": (
                f"facts/market/trade_date={trade_date}/daily_security_status.parquet"
            ),
            "reference_facts": (
                f"facts/reference/as_of_date={trade_date}/security_reference.parquet"
            ),
        },
        "reference_contract": {
            "data_dictionary": "docs/DATA_DICTIONARY.md",
            "pit_policy": "known_at <= data_cutoff_time",
            "missing_value_policy": "unknown values remain null",
        },
    }
    reference = _json_value(reference)
    encoded = json.dumps(
        reference, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_DATA_REFERENCE_BYTES:
        raise ValueError(
            "data_reference_latest.json is "
            f"{len(encoded)} bytes; limit is {MAX_DATA_REFERENCE_BYTES}"
        )
    return reference


def build_data_reference_outputs(
    data_dir: Path,
    trade_date: str | None = None,
    *,
    min_adt20: float = DEFAULT_MIN_ADT20,
) -> dict[str, Any]:
    latest = data_dir / "latest"
    manifest = _read_json(latest / "manifest.json")
    if not manifest:
        raise FileNotFoundError("latest manifest does not exist")
    latest_trade_date = str(manifest.get("trade_date") or "")
    selected_date = trade_date or latest_trade_date
    publish_latest = selected_date == latest_trade_date
    if not publish_latest:
        snapshot_manifest = _read_json(
            data_dir / "snapshots" / selected_date / "manifest.json"
        )
        if not snapshot_manifest:
            raise FileNotFoundError(f"snapshot manifest does not exist: {selected_date}")
        manifest = snapshot_manifest
    if manifest.get("verified") is not True or manifest.get("data_fresh") is not True:
        raise ValueError("selected snapshot is not verified and fresh")
    source_hash = _source_snapshot_hash(manifest)
    history = load_market_history(data_dir, selected_date)
    reference = _current_reference(data_dir, selected_date)
    snapshot_dir = data_dir / "snapshots" / selected_date
    status_path = snapshot_dir / "daily_security_status.parquet"
    if not status_path.exists():
        status_path = latest / "daily_security_status.parquet"
    daily_status = pd.read_parquet(status_path)
    adjustments = _latest_partition_frame(
        data_dir, "adjustment", "adjustment_factors.parquet", selected_date
    )
    corporate_actions = _latest_partition_frame(
        data_dir, "adjustment", "corporate_actions.parquet", selected_date
    )
    industry = _latest_partition_frame(
        data_dir, "classification", "industry_membership.parquet", selected_date
    )
    indexes = _latest_partition_frame(
        data_dir, "classification", "index_membership.parquet", selected_date
    )
    index_history = load_index_history(data_dir, selected_date)
    provider_tradability = _latest_partition_frame(
        data_dir, "tradability", "provider_tradability.parquet", selected_date
    )
    stock_state, readiness = build_stock_state(
        history,
        reference,
        daily_status,
        adjustments,
        corporate_actions,
        industry,
        indexes,
        index_history,
        provider_tradability,
        selected_date,
        source_hash,
        min_adt20=min_adt20,
    )
    module_status = load_module_status(data_dir, selected_date)
    module_mapping = {
        "history": "history",
        "adjustment": "adjustment",
        "industry": "industry",
        "index-membership": "index_membership",
        "tradability": "tradability",
    }
    recorded_modules = module_status.get("modules") or {}
    for canary_name, readiness_name in module_mapping.items():
        recorded = recorded_modules.get(canary_name) or {}
        current_state = (readiness.get(readiness_name) or {}).get("state")
        if recorded.get("state") == "not_entitled" and current_state != "ready":
            readiness[readiness_name] = {
                **(readiness.get(readiness_name) or {}),
                "state": "not_entitled",
                "entitlement_checked_at_utc": recorded.get("checked_at_utc"),
            }
    summary_path = snapshot_dir / "market_summary.json"
    if not summary_path.exists():
        summary_path = latest / "market_summary.json"
    market_summary = _read_json(summary_path)
    data_reference = build_data_reference(
        manifest, market_summary, stock_state, readiness, source_hash
    )
    reference_metadata = {
        "schema_version": DATA_REFERENCE_SCHEMA_VERSION,
        "source_snapshot_sha256": source_hash,
        "readiness": readiness,
        "stock_state_rows": int(len(stock_state)),
    }
    final_manifest = publish_data_reference_artifacts(
        data_dir,
        selected_date,
        stock_state,
        data_reference,
        reference_metadata,
        publish_latest=publish_latest,
    )
    reference_path = (
        latest / "data_reference_latest.json"
        if publish_latest
        else snapshot_dir / "data_reference_latest.json"
    )
    return {
        "ok": True,
        "trade_date": selected_date,
        "stock_state_rows": int(len(stock_state)),
        "readiness": readiness,
        "source_snapshot_sha256": source_hash,
        "manifest_artifact_count": len(final_manifest.get("artifacts") or {}),
        "data_reference_path": str(reference_path.resolve()),
    }


__all__ = [
    "ADJUSTMENT_REQUIRED_COLUMNS",
    "CORPORATE_ACTION_REQUIRED_COLUMNS",
    "DATA_REFERENCE_SCHEMA_VERSION",
    "INDEX_MEMBERSHIP_REQUIRED_COLUMNS",
    "INDUSTRY_REQUIRED_COLUMNS",
    "PROVIDER_TRADABILITY_REQUIRED_COLUMNS",
    "STOCK_STATE_COLUMNS",
    "TARGET_HISTORY_SESSIONS",
    "apply_adjustments",
    "build_data_reference",
    "build_data_reference_outputs",
    "build_stock_state",
    "build_tradability_state",
    "data_cutoff_time_for_date",
    "load_adjustment_snapshot",
    "load_market_history",
    "load_index_history",
    "load_module_status",
    "normalize_adjustments",
    "normalize_corporate_actions",
    "normalize_index_membership",
    "normalize_industry_membership",
    "normalize_provider_tradability",
]
