"""Capability-specific research contracts; no investment scores or recommendations."""

from __future__ import annotations

import hashlib
import math
import pandas as pd

from .storage import ArtifactContractError

MINIMUM_FINANCIAL_FIELDS = (
    "revenue", "net_profit_parent", "net_profit_ex_nonrecurring",
    "operating_cash_flow", "capex", "accounts_receivable", "inventory",
    "cash", "interest_bearing_debt",
)


def timestamp(value: str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ArtifactContractError("research_asof must be a timezone-aware timestamp")
    return stamp.tz_convert("UTC")


def availability(*, published_at, first_observed_at, known_at, research_asof,
                 historical_cutoff, verified=True) -> dict:
    """Missing publication time stays unknown; observation is never backdated."""
    def usable(cutoff):
        if not verified or not first_observed_at or not known_at:
            return False
        observed, known, end = map(timestamp, (first_observed_at, known_at, cutoff))
        return bool(observed <= known <= end and
                    (published_at is None or timestamp(published_at) <= observed))

    return {
        "source_published_at": published_at,
        "first_observed_at": first_observed_at,
        "known_at": known_at,
        "usable_for_current_research": usable(research_asof),
        "usable_for_historical_replay": usable(historical_cutoff),
    }


def minimum_financial_evidence(financial: dict | None) -> dict:
    statement = (financial or {}).get("latest_statement") or {}
    values = statement.get("values") or {}
    missing = [name for name in MINIMUM_FINANCIAL_FIELDS if values.get(name) is None]
    evidence = statement.get("field_evidence") or {}
    precisions = [evidence.get(field, {}).get("precision_cny") for field in ("interest_bearing_debt", "cash")]
    precision = max(map(float, precisions)) if all(value is not None for value in precisions) else None
    net_debt = (values["interest_bearing_debt"] - values["cash"]
                if values.get("interest_bearing_debt") is not None and values.get("cash") is not None else None)
    return {
        "state": "observed" if not missing else "partial" if statement else "missing",
        "missing_fields": missing,
        "report_period": statement.get("report_period"),
        "period_basis": statement.get("period_basis"),
        "published_at": statement.get("published_at"),
        "net_debt_cny": round(net_debt / precision) * precision if net_debt is not None and precision else net_debt,
        "net_debt_precision_cny": precision,
        "net_debt_basis": "interest_bearing_debt_minus_reported_cash; restricted_cash_unknown",
        "applicability": "business_model_review_required; not_a_bank_or_insurer_quality_score",
    }


def _coverage(count, denominator, **details):
    return {"state": "ready" if denominator and count == denominator else "partial" if count else "missing",
            "eligible_count": int(count), "denominator": int(denominator), **details}


def build_capabilities(state: pd.DataFrame, readiness: dict, research: dict | None = None) -> dict:
    research = research or {}
    rows = len(state)

    def count(field):
        return int(pd.to_numeric(state.get(field, pd.Series(dtype=float)), errors="coerce")
                   .map(lambda x: pd.notna(x) and math.isfinite(x)).sum())

    windows = {
        f"{n}D": {
            "raw": _coverage(count(f"raw_return_{n}d_pct"), rows,
                             basis="compounded_provider_daily_change_ratio"),
            "adjusted": _coverage(count(f"return_{n}d_pct"), rows,
                                  basis="point_in_time_adjustment_factors"),
        } for n in (1, 3, 5, 20)
    }
    counts = (research.get("field_coverage") or {}).get("financial_non_null_counts", {})
    denominator = research.get("universe_count", rows)
    supported = research.get("capability_counts") or {}
    return {
        "daily_price_volume": _coverage(count("raw_return_1d_pct"), rows,
            corporate_action_state=(readiness.get("corporate_actions") or {}).get("state", "missing"),
            limitation="raw_observation_only; corporate_action_checks_separate"),
        "multi_period_price": {"windows": windows,
            "unconfirmed_calendar_dates": (readiness.get("history") or {}).get("unconfirmed_calendar_dates", [])},
        "industry_relative_strength": _coverage(count("relative_return_industry_20d_pct"), rows,
            mapping_state=(readiness.get("industry") or {}).get("state", "missing")),
        "index_breadth": {"state": "missing", "membership_state": (readiness.get("index_membership") or {}).get("state", "missing"),
            "reason": "complete_point_in_time_constituent_denominator_not_certified",
            "proxy_policy": "market_cap_groups_are_not_index_members; selected_securities_are_not_market_breadth"},
        "earnings_cashflow": _coverage(supported.get("earnings_cashflow", 0), denominator,
            field_non_null_counts=counts, minimum_bundle_count=supported.get("minimum_financial_bundle", 0),
            limitation="same_statement_basis_required; industry_applicability_and_one_offs_require_review"),
        "forecast_revisions": _coverage(supported.get("forecast_revisions", 0), denominator,
            basis="matched_fiscal_year_institution_and_estimate_basis_with_distinct_versions"),
        "company_events": _coverage(supported.get("company_events", 0), denominator,
            limitation="announcement_presence_is_not_catalyst_strength"),
        "execution_checks": {"state": "not_ready",
            "eod_clear_count": int(state.get("tradability_state", pd.Series(dtype=str)).eq("clear").sum()),
            "reason": "eod_only; live_quote_costs_and_next_trading_window_not_verified"},
    }


def attach_research_links(radar: dict, artifacts: dict) -> None:
    index = artifacts["research_inputs_latest.json"]
    securities = {row["thscode"]: (name, row) for name, shard in artifacts.items()
                  if name != "research_inputs_latest.json" for row in shard["securities"]}
    radar["research_asof"] = index["research_asof"]
    radar["research_contract"] = {
        "read_order": ["last_run_status.json", "manifest.json", "data_reference_latest.json",
                       "opportunity_radar_latest.json", "research_inputs_latest.json", "selected_shards"],
        "capabilities_path": "data_reference_latest.json#research_capabilities",
        "selection_is_not_investment_edge": True,
        "research_inputs_ready_means": "index_and_shards_built; inspect_each_capability",
    }
    for row in radar["candidate_union"]:
        code = row["thscode"]
        if code not in securities:
            raise ArtifactContractError(f"selected security has no research shard: {code}")
        name, detail = securities[code]
        prefix = hashlib.sha256(code.encode("ascii")).hexdigest()[0]
        part = next(part for part in index["shards"][prefix] if part["path"] == name)
        routes = ["price_volume_observation"]
        if detail["financials"]:
            routes.append("earnings_change_review")
        if detail["events"]:
            routes.append("event_repricing")
        if detail.get("commodity_exposures"):
            routes.append("commodity_profit_transmission")
        row["research_context"] = {
            "part": name, "sha256": part["sha256"], "routes": routes,
            "missing_financial_fields": detail["minimum_financial_evidence"]["missing_fields"],
            "evidence_domains": ["TAPE"] + (["FINANCIAL"] if detail["financials"] else [])
                                + (["EVENT"] if detail["events"] else []),
        }
