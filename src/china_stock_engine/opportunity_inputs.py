"""Compact factual inputs and deterministic screens for downstream consumers."""

from __future__ import annotations

import math
from typing import Any, Callable

import pandas as pd

from .storage import serialize_json


OPPORTUNITY_INPUTS_SCHEMA_VERSION = 3
MAX_OPPORTUNITY_INPUTS_BYTES = 2 * 1024 * 1024
DEFAULT_SCREEN_LIMIT = 25
DEFAULT_CANDIDATE_UNION_LIMIT = 150
HORIZONS = (1, 3, 5, 20)

BOARD_GROUPS = (
    ("SSE_MAIN", "sse_main"),
    ("SZSE_MAIN", "szse_main"),
    ("STAR", "star"),
    ("CHINEXT", "chinext"),
    ("BSE", "bse"),
)
MARKET_CAP_BUCKETS = (
    "micro_lt_5bn_cny",
    "small_5_to_20bn_cny",
    "mid_20_to_80bn_cny",
    "large_80_to_300bn_cny",
    "mega_ge_300bn_cny",
)
PERCENTILE_SOURCE_FIELDS = {
    "return_1d_pctile": "raw_return_1d_pct",
    "return_3d_pctile": "raw_return_3d_pct",
    "return_5d_pctile": "raw_return_5d_pct",
    "return_20d_pctile": "raw_return_20d_pct",
    "amount_change_pctile": "amount_change_pct",
    "turnover_change_pctile": "turnover_change_pct",
    "close_location_pctile": "close_location",
    "amount_pctile": "amount",
    "turnover_ratio_pctile": "turnover_ratio",
    "gap_pctile": "gap_pct",
    "intraday_range_pctile": "intraday_range_pct",
}
CONTRADICTION_FLAG_DEFINITIONS = {
    "price_up_but_weak_close": "change_ratio > 0 and close_location < 0.4",
    "volume_spike_but_negative_return": (
        "change_ratio < 0 and either amount_change_pct or "
        "turnover_change_pct is positive"
    ),
    "strong_5d_but_negative_1d": (
        "raw_return_5d_pct > 0 and raw_return_1d_pct < 0"
    ),
    "tiny_absolute_amount": "amount < 20,000,000 CNY",
    "micro_cap": "total_market_cap < 5,000,000,000 CNY",
    "high_turnover": "turnover_ratio is at or above the daily 95th percentile",
    "gap_up_failed": "gap_pct > 0 and close_location < 0.4",
}


def _finite(value: Any, digits: int = 6) -> float | None:
    if value is None or pd.isna(value):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, set):
        return [_json_value(item) for item in sorted(value, key=str)]
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if value is None or value is pd.NA:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _coverage(frame: pd.DataFrame, field: str) -> dict[str, Any]:
    row_count = int(len(frame))
    non_null = int(frame[field].notna().sum()) if field in frame.columns else 0
    return {
        "non_null_count": non_null,
        "row_count": row_count,
        "coverage_ratio": round(non_null / row_count, 6) if row_count else 0.0,
    }


def _market_horizon_summary(
    frame: pd.DataFrame, readiness: dict[str, Any]
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    horizons = ((readiness.get("history") or {}).get("horizons") or {})
    for periods in HORIZONS:
        raw_field = f"raw_return_{periods}d_pct"
        values = pd.to_numeric(frame.get(raw_field), errors="coerce").dropna()
        horizon = dict(horizons.get(f"{periods}D") or {})
        output[f"{periods}D"] = {
            "state": horizon.get("state", "missing"),
            "return_field": raw_field,
            "return_basis": "compounded_provider_daily_change_ratio",
            "observations": int(len(values)),
            "coverage_ratio": round(len(values) / len(frame), 6)
            if len(frame)
            else 0.0,
            "equal_weight_mean_pct": _finite(values.mean()) if len(values) else None,
            "median_pct": _finite(values.median()) if len(values) else None,
            "positive_count": int(values.gt(0).sum()),
            "negative_count": int(values.lt(0).sum()),
            "unchanged_count": int(values.eq(0).sum()),
        }
    return output


def _group_summary(frame: pd.DataFrame, field: str) -> list[dict[str, Any]]:
    if field not in frame.columns or frame.empty:
        return []
    rows: list[dict[str, Any]] = []
    for key, group in frame.groupby(field, dropna=False):
        changes = pd.to_numeric(group.get("change_ratio"), errors="coerce")
        amounts = pd.to_numeric(group.get("amount"), errors="coerce")
        rows.append(
            {
                field: None if pd.isna(key) else str(key),
                "security_count": int(group["thscode"].nunique()),
                "amount_observed_count": int(amounts.notna().sum()),
                "total_amount": _finite(amounts.sum(min_count=1)),
                "equal_weight_change_pct": _finite(changes.mean()),
                "median_change_pct": _finite(changes.median()),
            }
        )
    return sorted(rows, key=lambda item: str(item.get(field) or ""))


def _market_cap_bucket(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    number = float(value)
    if number < 5_000_000_000:
        return "micro_lt_5bn_cny"
    if number < 20_000_000_000:
        return "small_5_to_20bn_cny"
    if number < 80_000_000_000:
        return "mid_20_to_80bn_cny"
    if number < 300_000_000_000:
        return "large_80_to_300bn_cny"
    return "mega_ge_300bn_cny"


def _numeric_series(frame: pd.DataFrame, field: str) -> pd.Series:
    if field not in frame.columns:
        return pd.Series(float("nan"), index=frame.index, dtype="float64")
    return pd.to_numeric(frame[field], errors="coerce")


def _cross_sectional_percentile(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    output = pd.Series(float("nan"), index=numeric.index, dtype="float64")
    observed = numeric.notna()
    if observed.any():
        output.loc[observed] = numeric.loc[observed].rank(
            method="average", pct=True, ascending=True
        )
    return output


def _descending_rank(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric.rank(method="min", ascending=False, na_option="keep").astype(
        "Int64"
    )


def _dailyized_return(values: pd.Series, periods: int) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    growth = 1 + numeric / 100.0
    growth = growth.where(growth.ge(0))
    return (growth.pow(1.0 / periods) - 1) * 100.0


def _prepare_cross_section(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = frame.copy()
    prepared["market_cap_bucket"] = _numeric_series(
        prepared, "total_market_cap"
    ).map(_market_cap_bucket)
    for output_field, source_field in PERCENTILE_SOURCE_FIELDS.items():
        prepared[output_field] = _cross_sectional_percentile(
            _numeric_series(prepared, source_field)
        )
    prepared["amount_rank"] = _descending_rank(_numeric_series(prepared, "amount"))
    prepared["turnover_rank"] = _descending_rank(
        _numeric_series(prepared, "turnover_ratio")
    )
    amount = _numeric_series(prepared, "amount")
    float_market_cap = _numeric_series(prepared, "float_market_cap")
    prepared["amount_to_float_market_cap"] = (
        amount / float_market_cap.where(float_market_cap.gt(0))
    )
    prepared["dailyized_return_3d_pct"] = _dailyized_return(
        _numeric_series(prepared, "raw_return_3d_pct"), 3
    )
    prepared["dailyized_return_5d_pct"] = _dailyized_return(
        _numeric_series(prepared, "raw_return_5d_pct"), 5
    )
    trailing_dailyized = pd.concat(
        [
            prepared["dailyized_return_3d_pct"],
            prepared["dailyized_return_5d_pct"],
        ],
        axis=1,
    ).max(axis=1, skipna=False)
    prepared["momentum_acceleration_1d_vs_3d_5d_pct"] = (
        _numeric_series(prepared, "raw_return_1d_pct") - trailing_dailyized
    )
    activity_percentiles = pd.concat(
        [
            prepared["amount_change_pctile"],
            prepared["turnover_change_pctile"],
        ],
        axis=1,
    )
    prepared["activity_expansion_confirmation_pctile"] = (
        activity_percentiles.min(axis=1, skipna=False)
    )
    prepared["activity_contraction_confirmation_pctile"] = 1 - (
        activity_percentiles.max(axis=1, skipna=False)
    )
    prepared["absolute_return_1d_pct"] = _numeric_series(
        prepared, "raw_return_1d_pct"
    ).abs()
    return prepared


SCREEN_FACT_FIELDS = (
    "thscode",
    "security_name",
    "exchange",
    "board",
    "raw_close",
    "change_ratio",
    "amount",
    "volume",
    "turnover_ratio",
    "amount_change_pct",
    "turnover_change_pct",
    "close_location",
    "gap_pct",
    "intraday_range_pct",
    "close_vs_avg_pct",
    "raw_return_1d_pct",
    "raw_return_3d_pct",
    "raw_return_5d_pct",
    "raw_return_20d_pct",
    "return_1d_pct",
    "return_3d_pct",
    "return_5d_pct",
    "return_20d_pct",
    "float_market_cap",
    "total_market_cap",
    "market_cap_bucket",
    "amount_rank",
    "turnover_rank",
    "amount_to_float_market_cap",
    "amount_z20",
    "turnover_z20",
    "adt20",
    "tradability_state",
    "limit_up",
    "limit_down",
    "one_word_limit",
    "effective_pit_cutoff",
)

CANDIDATE_FACT_FIELDS = (
    *SCREEN_FACT_FIELDS,
    "volume_z20",
    "listing_age_calendar_days",
    "distance_from_high_20_pct",
    "is_st",
    "is_suspended",
    "daily_price_limit_pct",
)


def _screen_rows(
    frame: pd.DataFrame,
    metric: str,
    *,
    ascending: bool,
    limit: int,
    definition: str,
    predicate: Callable[[pd.Series], pd.Series] | None = None,
    eligibility: pd.Series | None = None,
    derived_values: pd.Series | None = None,
    basis: str | None = None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values = (
        pd.to_numeric(derived_values.reindex(frame.index), errors="coerce")
        if derived_values is not None
        else _numeric_series(frame, metric)
    )
    eligible = values.notna()
    if predicate is not None:
        eligible &= predicate(values).fillna(False)
    if eligibility is not None:
        eligible &= eligibility.reindex(frame.index).fillna(False).astype(bool)
    metric_percentiles = _cross_sectional_percentile(values)
    ranked = frame.loc[eligible].copy()
    ranked["_screen_metric"] = values.loc[eligible]
    ranked["_screen_metric_percentile"] = metric_percentiles.loc[eligible]
    ranked = ranked.sort_values(
        ["_screen_metric", "thscode"],
        ascending=[ascending, True],
        kind="mergesort",
    ).head(limit)
    rows: list[dict[str, Any]] = []
    for rank, (_, row) in enumerate(ranked.iterrows(), start=1):
        facts = {
            field: _json_value(row.get(field))
            for field in SCREEN_FACT_FIELDS
            if field in row.index
        }
        facts["trigger"] = {
            "metric": metric,
            "value": _finite(row["_screen_metric"]),
            "rank": rank,
            "percentile": _finite(row["_screen_metric_percentile"]),
            "rule": definition,
        }
        if basis:
            facts["trigger"]["basis"] = basis
        rows.append(facts)
    output = {
        "state": "ready" if rows else "unavailable",
        "definition": definition,
        "metric": metric,
        "eligible_count": int(eligible.sum()),
        "limit": limit,
        "rows": rows,
    }
    if scope:
        output["scope"] = _json_value(scope)
    return output


def _number_from_row(row: pd.Series, field: str) -> float | None:
    value = row.get(field)
    if value is None or pd.isna(value):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _contradiction_flags(row: pd.Series) -> list[str]:
    change = _number_from_row(row, "change_ratio")
    return_1d = _number_from_row(row, "raw_return_1d_pct")
    return_5d = _number_from_row(row, "raw_return_5d_pct")
    close_location = _number_from_row(row, "close_location")
    amount = _number_from_row(row, "amount")
    amount_change = _number_from_row(row, "amount_change_pct")
    turnover_change = _number_from_row(row, "turnover_change_pct")
    turnover_pctile = _number_from_row(row, "turnover_ratio_pctile")
    gap = _number_from_row(row, "gap_pct")
    flags: list[str] = []
    if change is not None and close_location is not None:
        if change > 0 and close_location < 0.4:
            flags.append("price_up_but_weak_close")
    if change is not None and change < 0:
        expansion_observed = (
            amount_change is not None and amount_change > 0
        ) or (turnover_change is not None and turnover_change > 0)
        if expansion_observed:
            flags.append("volume_spike_but_negative_return")
    if (
        return_5d is not None
        and return_1d is not None
        and return_5d > 0
        and return_1d < 0
    ):
        flags.append("strong_5d_but_negative_1d")
    if amount is not None and amount < 20_000_000:
        flags.append("tiny_absolute_amount")
    if row.get("market_cap_bucket") == "micro_lt_5bn_cny":
        flags.append("micro_cap")
    if turnover_pctile is not None and turnover_pctile >= 0.95:
        flags.append("high_turnover")
    if gap is not None and close_location is not None:
        if gap > 0 and close_location < 0.4:
            flags.append("gap_up_failed")
    return flags


def _build_candidate_union(
    frame: pd.DataFrame,
    screens: dict[str, dict[str, Any]],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    captures: dict[str, list[dict[str, Any]]] = {}
    for screen_name, screen in screens.items():
        for screen_row in screen.get("rows") or []:
            thscode = str(screen_row.get("thscode") or "")
            if not thscode:
                continue
            trigger = dict(screen_row.get("trigger") or {})
            captures.setdefault(thscode, []).append(
                {
                    "screen": screen_name,
                    "rank": int(trigger.get("rank") or 0),
                    "metric": trigger.get("metric"),
                    "value": trigger.get("value"),
                    "percentile": trigger.get("percentile"),
                }
            )
    if not captures:
        return [], 0

    indexed = frame.drop_duplicates("thscode", keep="last").set_index("thscode")

    def ordering(item: tuple[str, list[dict[str, Any]]]) -> tuple[int, int, str]:
        thscode, triggers = item
        ranks = [int(trigger["rank"]) for trigger in triggers]
        return (-len(triggers), min(ranks), thscode)

    ordered = sorted(captures.items(), key=ordering)
    rows: list[dict[str, Any]] = []
    for union_order, (thscode, triggers) in enumerate(ordered[:limit], start=1):
        source = indexed.loc[thscode]
        if isinstance(source, pd.DataFrame):
            source = source.iloc[-1]
        screen_ranks = {
            str(trigger["screen"]): int(trigger["rank"]) for trigger in triggers
        }
        screen_percentiles = {
            str(trigger["screen"]): trigger.get("percentile") for trigger in triggers
        }
        percentiles = {
            field: _finite(source.get(field))
            for field in PERCENTILE_SOURCE_FIELDS
        }
        facts = {
            field: _json_value(source.get(field))
            for field in CANDIDATE_FACT_FIELDS
            if field not in {"thscode", "security_name"} and field in source.index
        }
        rows.append(
            {
                "union_order": union_order,
                "thscode": thscode,
                "security_name": _json_value(source.get("security_name")),
                "triggered_screens": [
                    str(trigger["screen"]) for trigger in triggers
                ],
                "screen_count": len(triggers),
                "best_screen_rank": min(screen_ranks.values()),
                "screen_ranks": screen_ranks,
                "screen_percentiles": screen_percentiles,
                "percentiles": percentiles,
                "contradiction_flags": _contradiction_flags(source),
                "facts": facts,
            }
        )
    return rows, len(captures)


def build_opportunity_inputs(
    manifest: dict[str, Any],
    market_summary: dict[str, Any],
    quotes: pd.DataFrame,
    stock_state: pd.DataFrame,
    readiness: dict[str, Any],
    source_snapshot_sha256: str,
    pit_timing: dict[str, str],
    *,
    screen_limit: int = DEFAULT_SCREEN_LIMIT,
    candidate_union_limit: int = DEFAULT_CANDIDATE_UNION_LIMIT,
) -> dict[str, Any]:
    """Build opinion-free, deterministic screen inputs for downstream use."""

    if screen_limit < 1 or screen_limit > 30:
        raise ValueError("screen_limit must be between 1 and 30")
    if candidate_union_limit < 1 or candidate_union_limit > 150:
        raise ValueError("candidate_union_limit must be between 1 and 150")
    quote_fields = [
        field
        for field in ("thscode", "change_ratio", "amount", "volume", "turnover_ratio")
        if field in quotes.columns
    ]
    current_quotes = quotes.loc[:, quote_fields].drop_duplicates(
        "thscode", keep="last"
    )
    frame = _prepare_cross_section(
        stock_state.merge(
            current_quotes,
            how="left",
            on="thscode",
            validate="one_to_one",
        )
    )
    market_changes = _market_horizon_summary(frame, readiness)
    field_names = [
        "change_ratio",
        "amount",
        "volume",
        "turnover_ratio",
        "amount_change_pct",
        "turnover_change_pct",
        "close_location",
        "float_market_cap",
        "total_market_cap",
        "gap_pct",
        "intraday_range_pct",
        "amount_to_float_market_cap",
        "amount_rank",
        "turnover_rank",
        "amount_z20",
        "turnover_z20",
        "volume_z20",
        "adt20",
        "listing_age_calendar_days",
        "distance_from_high_20_pct",
        "is_st",
        "is_suspended",
        "daily_price_limit_pct",
        *PERCENTILE_SOURCE_FIELDS.keys(),
        *(f"raw_return_{periods}d_pct" for periods in HORIZONS),
        *(f"return_{periods}d_pct" for periods in HORIZONS),
    ]

    cap_frame = frame.copy()
    cap_summary = _group_summary(
        cap_frame.loc[cap_frame["market_cap_bucket"].notna()],
        "market_cap_bucket",
    )
    unknown_cap_count = int(cap_frame["market_cap_bucket"].isna().sum())

    screens = {
        "largest_positive_moves": _screen_rows(
            frame,
            "change_ratio",
            ascending=False,
            limit=screen_limit,
            definition="change_ratio > 0, descending",
            predicate=lambda values: values.gt(0),
        ),
        "largest_negative_moves": _screen_rows(
            frame,
            "change_ratio",
            ascending=True,
            limit=screen_limit,
            definition="change_ratio < 0, ascending",
            predicate=lambda values: values.lt(0),
        ),
        "highest_amount": _screen_rows(
            frame,
            "amount",
            ascending=False,
            limit=screen_limit,
            definition="amount observed, descending",
        ),
        "amount_expansion": _screen_rows(
            frame,
            "amount_change_pct",
            ascending=False,
            limit=screen_limit,
            definition="amount_change_pct > 0, descending",
            predicate=lambda values: values.gt(0),
        ),
        "turnover_expansion": _screen_rows(
            frame,
            "turnover_change_pct",
            ascending=False,
            limit=screen_limit,
            definition="turnover_change_pct > 0, descending",
            predicate=lambda values: values.gt(0),
        ),
        "strong_close_location": _screen_rows(
            frame,
            "close_location",
            ascending=False,
            limit=screen_limit,
            definition="close_location >= 0.8, descending",
            predicate=lambda values: values.ge(0.8),
        ),
        "weak_close_location": _screen_rows(
            frame,
            "close_location",
            ascending=True,
            limit=screen_limit,
            definition="close_location <= 0.2, ascending",
            predicate=lambda values: values.le(0.2),
        ),
    }
    for periods in (3, 5, 20):
        raw_field = f"raw_return_{periods}d_pct"
        values = pd.to_numeric(frame.get(raw_field), errors="coerce")
        median = values.median(skipna=True)
        relative = values - median if not pd.isna(median) else values * float("nan")
        screens[f"relative_strength_{periods}d"] = _screen_rows(
            frame,
            f"relative_strength_{periods}d_pct",
            ascending=False,
            limit=screen_limit,
            definition=(
                f"{raw_field} minus cross-sectional median, descending"
            ),
            derived_values=relative,
            basis="compounded_provider_daily_change_ratio",
        )
        screens[f"relative_strength_{periods}d"]["market_median_pct"] = _finite(
            median
        )

    change = _numeric_series(frame, "change_ratio")
    return_1d = _numeric_series(frame, "raw_return_1d_pct")
    return_3d = _numeric_series(frame, "raw_return_3d_pct")
    return_5d = _numeric_series(frame, "raw_return_5d_pct")
    amount_change = _numeric_series(frame, "amount_change_pct")
    turnover_change = _numeric_series(frame, "turnover_change_pct")
    close_location = _numeric_series(frame, "close_location")
    return_1d_pctile = frame["return_1d_pctile"]
    return_3d_pctile = frame["return_3d_pctile"]
    return_5d_pctile = frame["return_5d_pctile"]
    close_location_pctile = frame["close_location_pctile"]

    activity_expansion = amount_change.gt(0) & turnover_change.gt(0)
    activity_contraction = amount_change.lt(0) & turnover_change.lt(0)
    screens["price_up_activity_expansion"] = _screen_rows(
        frame,
        "activity_expansion_confirmation_pctile",
        ascending=False,
        limit=screen_limit,
        definition=(
            "change_ratio > 0, amount_change_pct > 0, and "
            "turnover_change_pct > 0; confirmation percentile descending"
        ),
        eligibility=change.gt(0) & activity_expansion,
        basis="minimum of amount_change_pctile and turnover_change_pctile",
    )
    screens["price_down_activity_expansion"] = _screen_rows(
        frame,
        "activity_expansion_confirmation_pctile",
        ascending=False,
        limit=screen_limit,
        definition=(
            "change_ratio < 0, amount_change_pct > 0, and "
            "turnover_change_pct > 0; confirmation percentile descending"
        ),
        eligibility=change.lt(0) & activity_expansion,
        basis="minimum of amount_change_pctile and turnover_change_pctile",
    )
    screens["price_up_activity_contraction"] = _screen_rows(
        frame,
        "activity_contraction_confirmation_pctile",
        ascending=False,
        limit=screen_limit,
        definition=(
            "change_ratio > 0, amount_change_pct < 0, and "
            "turnover_change_pct < 0; contraction confirmation descending"
        ),
        eligibility=change.gt(0) & activity_contraction,
        basis="one minus maximum activity-change percentile",
    )
    screens["price_down_activity_contraction"] = _screen_rows(
        frame,
        "activity_contraction_confirmation_pctile",
        ascending=False,
        limit=screen_limit,
        definition=(
            "change_ratio < 0, amount_change_pct < 0, and "
            "turnover_change_pct < 0; contraction confirmation descending"
        ),
        eligibility=change.lt(0) & activity_contraction,
        basis="one minus maximum activity-change percentile",
    )

    momentum_confirmation = pd.concat(
        [return_1d_pctile, return_3d_pctile, return_5d_pctile], axis=1
    ).min(axis=1, skipna=False)
    screens["positive_momentum_1d_3d_5d"] = _screen_rows(
        frame,
        "positive_momentum_confirmation_pctile",
        ascending=False,
        limit=screen_limit,
        definition=(
            "raw_return_1d_pct > 0, raw_return_3d_pct > 0, and "
            "raw_return_5d_pct > 0; weakest horizon percentile descending"
        ),
        eligibility=return_1d.gt(0) & return_3d.gt(0) & return_5d.gt(0),
        derived_values=momentum_confirmation,
        basis="minimum of 1D, 3D, and 5D cross-sectional percentiles",
    )
    acceleration = frame["momentum_acceleration_1d_vs_3d_5d_pct"]
    screens["momentum_acceleration_1d_vs_3d_5d"] = _screen_rows(
        frame,
        "momentum_acceleration_1d_vs_3d_5d_pct",
        ascending=False,
        limit=screen_limit,
        definition=(
            "1D return exceeds both dailyized 3D and dailyized 5D trends; "
            "difference descending"
        ),
        eligibility=return_1d.gt(0) & acceleration.gt(0),
        basis="1D return minus max(dailyized 3D return, dailyized 5D return)",
    )
    screens["strong_5d_negative_1d_pullback"] = _screen_rows(
        frame,
        "return_5d_pctile",
        ascending=False,
        limit=screen_limit,
        definition=(
            "return_5d_pctile >= 0.8 and raw_return_1d_pct < 0; "
            "5D percentile descending"
        ),
        eligibility=return_5d_pctile.ge(0.8) & return_1d.lt(0),
    )
    reversal_confirmation = pd.concat(
        [return_1d_pctile, 1 - return_5d_pctile], axis=1
    ).min(axis=1, skipna=False)
    screens["weak_5d_positive_1d_reversal"] = _screen_rows(
        frame,
        "reversal_confirmation_pctile",
        ascending=False,
        limit=screen_limit,
        definition=(
            "return_5d_pctile <= 0.2, return_1d_pctile >= 0.8, and "
            "raw_return_1d_pct > 0; confirmation descending"
        ),
        eligibility=(
            return_5d_pctile.le(0.2)
            & return_1d_pctile.ge(0.8)
            & return_1d.gt(0)
        ),
        derived_values=reversal_confirmation,
        basis="minimum of return_1d_pctile and one minus return_5d_pctile",
    )

    large_positive = return_1d.gt(0) & return_1d_pctile.ge(0.8)
    large_negative = return_1d.lt(0) & return_1d_pctile.le(0.2)
    close_combinations = {
        "large_positive_strong_close": (
            large_positive & close_location.ge(0.8),
            pd.concat([return_1d_pctile, close_location_pctile], axis=1).min(
                axis=1, skipna=False
            ),
            "positive top-quintile 1D return and close_location >= 0.8",
        ),
        "large_positive_weak_close": (
            large_positive & close_location.lt(0.4),
            pd.concat([return_1d_pctile, 1 - close_location_pctile], axis=1).min(
                axis=1, skipna=False
            ),
            "positive top-quintile 1D return and close_location < 0.4",
        ),
        "large_negative_strong_close": (
            large_negative & close_location.gt(0.7),
            pd.concat([1 - return_1d_pctile, close_location_pctile], axis=1).min(
                axis=1, skipna=False
            ),
            "negative bottom-quintile 1D return and close_location > 0.7",
        ),
        "large_negative_weak_close": (
            large_negative & close_location.lt(0.2),
            pd.concat(
                [1 - return_1d_pctile, 1 - close_location_pctile], axis=1
            ).min(axis=1, skipna=False),
            "negative bottom-quintile 1D return and close_location < 0.2",
        ),
    }
    for screen_name, (eligibility, confirmation, definition) in close_combinations.items():
        screens[screen_name] = _screen_rows(
            frame,
            "return_close_confirmation_pctile",
            ascending=False,
            limit=screen_limit,
            definition=f"{definition}; confirmation percentile descending",
            eligibility=eligibility,
            derived_values=confirmation,
            basis="joint cross-sectional return and close-location extremity",
        )

    gap = _numeric_series(frame, "gap_pct")
    intraday_range_pctile = frame["intraday_range_pctile"]
    screens["gap_up_strong_close"] = _screen_rows(
        frame,
        "gap_pct",
        ascending=False,
        limit=screen_limit,
        definition="gap_pct > 0 and close_location >= 0.8; gap descending",
        eligibility=gap.gt(0) & close_location.ge(0.8),
    )
    screens["gap_up_weak_close"] = _screen_rows(
        frame,
        "gap_pct",
        ascending=False,
        limit=screen_limit,
        definition="gap_pct > 0 and close_location < 0.4; gap descending",
        eligibility=gap.gt(0) & close_location.lt(0.4),
    )
    screens["gap_down_strong_close"] = _screen_rows(
        frame,
        "gap_pct",
        ascending=True,
        limit=screen_limit,
        definition="gap_pct < 0 and close_location > 0.7; gap ascending",
        eligibility=gap.lt(0) & close_location.gt(0.7),
    )
    screens["large_intraday_range_strong_close"] = _screen_rows(
        frame,
        "intraday_range_pct",
        ascending=False,
        limit=screen_limit,
        definition=(
            "intraday_range_pctile >= 0.9 and close_location >= 0.8; "
            "range descending"
        ),
        eligibility=intraday_range_pctile.ge(0.9) & close_location.ge(0.8),
    )
    screens["large_intraday_range_weak_close"] = _screen_rows(
        frame,
        "intraday_range_pct",
        ascending=False,
        limit=screen_limit,
        definition=(
            "intraday_range_pctile >= 0.9 and close_location <= 0.2; "
            "range descending"
        ),
        eligibility=intraday_range_pctile.ge(0.9) & close_location.le(0.2),
    )

    absolute_return_1d = frame["absolute_return_1d_pct"]
    board_values = frame.get("board", pd.Series(pd.NA, index=frame.index))
    for board, slug in BOARD_GROUPS:
        screens[f"board_neutral_absolute_move__{slug}"] = _screen_rows(
            frame,
            "absolute_return_1d_pct",
            ascending=False,
            limit=screen_limit,
            definition=(
                f"board == {board} and absolute raw_return_1d_pct > 0; "
                "absolute move descending"
            ),
            predicate=lambda values: values.gt(0),
            eligibility=board_values.astype("string").eq(board),
            derived_values=absolute_return_1d,
            scope={"field": "board", "value": board},
        )
    cap_values = frame["market_cap_bucket"]
    for bucket in MARKET_CAP_BUCKETS:
        screens[f"market_cap_neutral_absolute_move__{bucket}"] = _screen_rows(
            frame,
            "absolute_return_1d_pct",
            ascending=False,
            limit=screen_limit,
            definition=(
                f"market_cap_bucket == {bucket} and absolute "
                "raw_return_1d_pct > 0; absolute move descending"
            ),
            predicate=lambda values: values.gt(0),
            eligibility=cap_values.astype("string").eq(bucket),
            derived_values=absolute_return_1d,
            scope={"field": "market_cap_bucket", "value": bucket},
        )

    candidate_union, unique_candidate_count = _build_candidate_union(
        frame, screens, limit=candidate_union_limit
    )

    payload = {
        "schema_version": OPPORTUNITY_INPUTS_SCHEMA_VERSION,
        "document_type": "a_share_opportunity_inputs",
        "trade_date": str(manifest.get("trade_date") or ""),
        "generated_at": pit_timing["collection_completed_at"],
        "source_snapshot_sha256": source_snapshot_sha256,
        "pit_timing": pit_timing,
        "data_mode": {
            "mode": "facts_and_deterministic_features",
            "raw_ifind_payload_persisted": False,
            "subjective_view_produced": False,
            "composite_score_produced": False,
            "trade_advice_produced": False,
        },
        "readiness": readiness,
        "field_coverage": {
            field: _coverage(frame, field) for field in field_names
        },
        "market": {
            "breadth": {
                "advancers": market_summary.get("advancers"),
                "decliners": market_summary.get("decliners"),
                "unchanged": market_summary.get("unchanged"),
                "equal_weight_change_pct": market_summary.get(
                    "equal_weight_change_pct"
                ),
                "median_change_pct": market_summary.get("median_change_pct"),
            },
            "turnover": {
                "total_amount": market_summary.get("total_amount"),
                "quoted_securities": market_summary.get("quoted_securities"),
            },
            "extreme_moves": {
                "moves_ge_9_5pct": market_summary.get("moves_ge_9_5pct"),
                "moves_le_minus_9_5pct": market_summary.get(
                    "moves_le_minus_9_5pct"
                ),
            },
            "changes": market_changes,
            "observation_state_counts": market_summary.get(
                "observation_state_counts", {}
            ),
        },
        "board_summary": _group_summary(frame, "board"),
        "market_cap_bucket_summary": {
            "basis": "total_market_cap_cny",
            "buckets": cap_summary,
            "unknown_count": unknown_cap_count,
        },
        "data_quality_drift": (
            ((manifest.get("quality") or {}).get("metrics") or {}).get("drift")
        ),
        "cross_sectional_features": {
            "percentile_method": (
                "daily cross-section, average rank for ties, ascending values, "
                "range (0, 1]"
            ),
            "percentile_fields": PERCENTILE_SOURCE_FIELDS,
            "amount_rank": "descending amount; rank 1 is the highest amount",
            "turnover_rank": (
                "descending turnover_ratio; rank 1 is the highest turnover_ratio"
            ),
            "amount_to_float_market_cap": (
                "amount divided by float_market_cap when both are observed and "
                "float_market_cap > 0"
            ),
        },
        "deterministic_screens": screens,
        "candidate_union": candidate_union,
        "candidate_union_metadata": {
            "definition": (
                "deduplicated union of deterministic screen rows; no security "
                "selection opinion is applied"
            ),
            "unique_security_count_before_limit": unique_candidate_count,
            "returned_count": len(candidate_union),
            "limit": candidate_union_limit,
            "ordering": (
                "screen_count descending, best_screen_rank ascending, "
                "thscode ascending"
            ),
            "union_order_semantics": (
                "deterministic transport order only; not a relative "
                "attractiveness ordering and no subjective weighting is applied"
            ),
            "screen_count_semantics": (
                "raw count of deterministic filters that captured the security"
            ),
        },
        "contradiction_flag_definitions": CONTRADICTION_FLAG_DEFINITIONS,
        "drilldown": {
            "stock_state": (
                "features/stock_state/"
                f"trade_date={manifest.get('trade_date')}/stock_state.parquet"
            ),
            "market_facts": (
                "facts/market/"
                f"trade_date={manifest.get('trade_date')}/daily_quotes.parquet"
            ),
            "reference_facts": (
                "facts/reference/"
                f"as_of_date={manifest.get('trade_date')}/security_reference.parquet"
            ),
        },
    }
    payload = _json_value(payload)
    encoded = serialize_json(payload, compact=True).encode("utf-8")
    if len(encoded) > MAX_OPPORTUNITY_INPUTS_BYTES:
        raise ValueError(
            "opportunity_inputs_latest.json is "
            f"{len(encoded)} bytes; limit is {MAX_OPPORTUNITY_INPUTS_BYTES}"
        )
    return payload


__all__ = [
    "DEFAULT_CANDIDATE_UNION_LIMIT",
    "DEFAULT_SCREEN_LIMIT",
    "MAX_OPPORTUNITY_INPUTS_BYTES",
    "OPPORTUNITY_INPUTS_SCHEMA_VERSION",
    "build_opportunity_inputs",
]
