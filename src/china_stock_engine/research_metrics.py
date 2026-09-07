"""Deterministic financial/forecast comparisons, never opinions or scores."""

from __future__ import annotations

import math

import pandas as pd


def positive_ratio(numerator, denominator):
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def financial_quality(by_period: dict, quarter: dict, ttm: dict) -> dict:
    period = max(by_period)
    values = by_period[period]["values"]
    year = int(period[:4])
    prior = by_period.get(f"{year - 1}{period[4:]}", {}).get("values", {})
    preceding = {"06-30": "03-31", "09-30": "06-30", "12-31": "09-30"}.get(period[5:])
    prior_preceding = by_period.get(f"{year - 1}-{preceding}", {}).get("values", {})
    quarter_yoy = {}
    for field, value in quarter.items():
        base = prior.get(field)
        if preceding:
            prev = prior_preceding.get(field)
            base = base - prev if base is not None and prev is not None else None
        ratio = positive_ratio(value, base)
        quarter_yoy[field] = None if ratio is None else (ratio - 1) * 100
    revenue, costs = values.get("revenue"), values.get("operating_costs")
    return {
        "quarter_yoy_pct": quarter_yoy,
        "statement_periods_available": len(by_period),
        "statement_periods": sorted(by_period),
        "gross_margin_ytd_pct": (None if revenue is None or revenue <= 0 or costs is None
                                  else (1 - costs / revenue) * 100),
        "operating_cashflow_to_parent_profit_ytd": positive_ratio(
            values.get("operating_cash_flow"), values.get("net_profit_parent")),
        "liabilities_to_assets": positive_ratio(values.get("total_liabilities"), values.get("total_assets")),
        "cash_to_interest_bearing_debt": positive_ratio(values.get("cash"), values.get("interest_bearing_debt")),
        "cash_definition": "reported_monetary_funds_not_confirmed_unrestricted_cash",
        "inventory_yoy_pct": _growth(values.get("inventory"), prior.get("inventory")),
        "accounts_receivable_yoy_pct": _growth(values.get("accounts_receivable"), prior.get("accounts_receivable")),
        "industry_applicability": "industrial_statement_ratios_require_industry_validation; not a bank/insurer model",
    }


def _growth(value, base):
    ratio = positive_ratio(value, base)
    return None if ratio is None else round((ratio - 1) * 100, 8)


def forecast_features(rows: list[dict], cutoff: str, sessions: list[str]) -> dict | None:
    """Compare fixed fiscal year and matched institutions, not rolling FY labels."""
    cutoff_stamp = pd.Timestamp(cutoff)
    rows = [row for row in rows if pd.Timestamp(row["known_at"]) <= cutoff_stamp]
    if not rows:
        return None

    def at(boundary):
        selected = {}
        for row in sorted(rows, key=lambda item: (pd.Timestamp(item["known_at"]), item["revision"])):
            if pd.Timestamp(row["known_at"]) <= boundary:
                selected[(row["forecast_year"], row["institution_id"], row["estimate_basis"])] = row
        return selected

    latest = at(cutoff_stamp)
    comparisons = {}
    local = cutoff_stamp.tz_convert("Asia/Shanghai")
    for horizon in (5, 20):
        if len(sessions) <= horizon:
            comparisons[f"{horizon}D"] = {"state": "not_ready", "reason": "insufficient_session_dates"}
            continue
        boundary = pd.Timestamp(sessions[-horizon - 1], tz="Asia/Shanghai") + (local - local.normalize())
        base = at(boundary)
        changes = []
        for key in sorted(latest.keys() & base.keys()):
            current, prior = latest[key], base[key]
            values = {}
            for metric in ("revenue", "net_profit_parent", "eps"):
                before, after = prior["values"].get(metric), current["values"].get(metric)
                values[metric] = {
                    "previous": before, "current": after,
                    "change": after - before if after is not None and before is not None else None,
                    "change_pct": _growth(after, before),
                }
            changes.append({"forecast_year": key[0], "institution_id": key[1], "estimate_basis": key[2],
                            "previous_known_at": prior["known_at"], "current_known_at": current["known_at"],
                            "values": values})
        individual = [item["values"]["net_profit_parent"]["change"] for item in changes
                      if item["estimate_basis"] == "individual_institution"
                      and item["values"]["net_profit_parent"]["change"] is not None]
        comparisons[f"{horizon}D"] = {
            "state": "observed" if changes else "not_ready", "comparison_cutoff": boundary.isoformat(),
            "matched_estimates": changes, "matched_individual_count": len(individual),
            "individual_up_count": sum(value > 0 for value in individual) if individual else None,
            "individual_down_count": sum(value < 0 for value in individual) if individual else None,
            "individual_unchanged_count": sum(value == 0 for value in individual) if individual else None,
        }
    return {"state": "observed", "is_forecast_not_actual": True,
            "latest_estimates": [latest[key] for key in sorted(latest)], "revisions": comparisons,
            "policy": "fixed fiscal year; matched provider identity and estimate basis; no subjective weights"}


def peer_valuation_percentiles(securities: list[dict], groups: dict[str, str]) -> None:
    """Only label complete peer-group comparisons; Top100 is not the sector."""
    peers = {}
    for security in securities:
        group = groups.get(security["thscode"])
        if group:
            peers.setdefault(group, []).append(security)
    for group, members in peers.items():
        for metric in ("pe_ttm", "pb", "ps_ttm"):
            valued = [(item, ((item.get("financials") or {}).get("valuation") or {}).get(metric)) for item in members]
            observed = [(item, value) for item, value in valued if value is not None and math.isfinite(value)]
            # Missing peers and non-positive denominators are reported, never hidden by a percentile.
            complete = len(observed) == len(members) and len(members) >= 5
            for item, value in valued:
                financial = item.get("financials")
                if financial:
                    financial["valuation"].setdefault("peer_comparison", {})[metric] = {
                        "industry_code": group, "peer_count": len(members), "observed_count": len(observed),
                        "state": "observed" if complete else "not_ready",
                        "percentile": (sum(other <= value for _, other in observed) / len(observed)
                                       if complete else None),
                        "policy": "empirical CDF, ascending, ties equal; minimum 5, all PIT peers observed",
                    }
