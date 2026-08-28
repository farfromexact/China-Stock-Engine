"""Compact factual inputs and deterministic screens for downstream consumers."""

from __future__ import annotations

import json
import math
from typing import Any, Callable

import pandas as pd


OPPORTUNITY_INPUTS_SCHEMA_VERSION = 1
MAX_OPPORTUNITY_INPUTS_BYTES = 2 * 1024 * 1024
DEFAULT_SCREEN_LIMIT = 25
HORIZONS = (1, 3, 5, 20)


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
    if isinstance(value, (list, tuple, set)):
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
    "tradability_state",
    "limit_up",
    "limit_down",
    "one_word_limit",
    "effective_pit_cutoff",
)


def _screen_rows(
    frame: pd.DataFrame,
    metric: str,
    *,
    ascending: bool,
    limit: int,
    definition: str,
    predicate: Callable[[pd.Series], pd.Series] | None = None,
    derived_values: pd.Series | None = None,
    basis: str | None = None,
) -> dict[str, Any]:
    values = (
        pd.to_numeric(derived_values, errors="coerce")
        if derived_values is not None
        else pd.to_numeric(frame.get(metric), errors="coerce")
    )
    eligible = values.notna()
    if predicate is not None:
        eligible &= predicate(values).fillna(False)
    ranked = frame.loc[eligible].copy()
    ranked["_screen_metric"] = values.loc[eligible]
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
            "rule": definition,
        }
        if basis:
            facts["trigger"]["basis"] = basis
        rows.append(facts)
    return {
        "state": "ready" if rows else "unavailable",
        "definition": definition,
        "metric": metric,
        "eligible_count": int(eligible.sum()),
        "limit": limit,
        "rows": rows,
    }


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
) -> dict[str, Any]:
    """Build opinion-free, deterministic screen inputs for downstream use."""

    if screen_limit < 1 or screen_limit > 30:
        raise ValueError("screen_limit must be between 1 and 30")
    quote_fields = [
        field
        for field in ("thscode", "change_ratio", "amount", "volume", "turnover_ratio")
        if field in quotes.columns
    ]
    current_quotes = quotes.loc[:, quote_fields].drop_duplicates(
        "thscode", keep="last"
    )
    frame = stock_state.merge(
        current_quotes,
        how="left",
        on="thscode",
        validate="one_to_one",
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
        *(f"raw_return_{periods}d_pct" for periods in HORIZONS),
        *(f"return_{periods}d_pct" for periods in HORIZONS),
    ]

    cap_frame = frame.copy()
    cap_frame["market_cap_bucket"] = cap_frame.get(
        "total_market_cap", pd.Series(pd.NA, index=cap_frame.index)
    ).map(_market_cap_bucket)
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
        "deterministic_screens": screens,
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
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_OPPORTUNITY_INPUTS_BYTES:
        raise ValueError(
            "opportunity_inputs_latest.json is "
            f"{len(encoded)} bytes; limit is {MAX_OPPORTUNITY_INPUTS_BYTES}"
        )
    return payload


__all__ = [
    "DEFAULT_SCREEN_LIMIT",
    "MAX_OPPORTUNITY_INPUTS_BYTES",
    "OPPORTUNITY_INPUTS_SCHEMA_VERSION",
    "build_opportunity_inputs",
]
