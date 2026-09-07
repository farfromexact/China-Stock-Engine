"""PIT research facts, explicit missingness, and bounded connector shards.

This layer accepts normalized, source-attributed observations, not raw vendor
responses. It never fetches data or infers entitlement from an empty result.
"""

from __future__ import annotations

from datetime import date
import hashlib
import math
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qsl, urlparse

import pandas as pd

from .research_metrics import financial_quality, forecast_features, peer_valuation_percentiles

from .storage import (
    ArtifactContractError,
    atomic_write_json,
    json_sha256,
    load_json_object,
    serialize_json,
)

RESEARCH_SCHEMA_VERSION = 1
HARD_MAX_BYTES = 300 * 1024
SHARD_TARGET_BYTES = 240 * 1024
FINANCIAL_FIELDS = (
    "revenue",
    "net_profit_parent",
    "net_profit_ex_nonrecurring",
    "operating_cash_flow",
    "equity_parent",
    "cash",
    "interest_bearing_debt",
    "capex",
)
FINANCIAL_V2_FIELDS = FINANCIAL_FIELDS + (
    "operating_costs", "accounts_receivable", "inventory", "total_assets", "total_liabilities",
)
STOCK_FIELDS = {
    "equity_parent", "cash", "interest_bearing_debt", "accounts_receivable",
    "inventory", "total_assets", "total_liabilities",
}
FLOW_FIELDS = set(FINANCIAL_V2_FIELDS) - STOCK_FIELDS
FORECAST_FIELDS = {"revenue", "net_profit_parent", "eps"}
EVENT_DETAIL_FIELDS = {
    "announced_amount_cny", "executed_amount_cny", "share_count",
    "float_share_ratio_pct", "profit_lower_cny", "profit_upper_cny",
    "profit_yoy_lower_pct", "profit_yoy_upper_pct", "cash_dividend_per_share_cny",
}
EVENT_TYPES = {
    "filing",
    "earnings_preview",
    "buyback",
    "dividend",
    "shareholder_reduction",
    "share_unlock",
    "control_transfer",
    "earnings_flash", "shareholder_increase", "major_contract", "restructuring",
}
CODE_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
COMMON_FIELDS = {
    "thscode",
    "published_at",
    "first_seen_at",
    "known_at",
    "revision",
    "source_url",
    "document_sha256",
}
OPTIONAL_SOURCE_FIELDS = {"source_url_kind", "document_access"}
ENVELOPE_FIELDS = {
    "schema_version",
    "module",
    "collection_started_at",
    "collection_completed_at",
    "coverage",
    "records",
}


def _time(value: Any) -> pd.Timestamp:
    try:
        result = pd.Timestamp(value)
        if pd.isna(result) or result.tzinfo is None:
            raise ValueError()
        return result.tz_convert("UTC")
    except (TypeError, ValueError) as exc:
        raise ArtifactContractError(
            "research timestamp must be explicit and timezone-aware"
        ) from exc


def _date(value: Any) -> str:
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError()
        return value
    except (TypeError, ValueError) as exc:
        raise ArtifactContractError("research date must be YYYY-MM-DD") from exc


def _number(value: Any) -> float | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, bool):
        raise ArtifactContractError("boolean is not a financial amount")
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ArtifactContractError("invalid financial amount") from exc
    if not math.isfinite(value):
        raise ArtifactContractError("financial amount must be finite or null")
    return value


def normalize_research_batch(payload: dict) -> dict:
    version = payload.get("schema_version")
    if set(payload) - ENVELOPE_FIELDS or type(version) is not int or version not in {1, 2}:
        raise ArtifactContractError(
            "unsupported research envelope schema or fields; raw payloads are forbidden"
        )
    module = payload.get("module")
    if module not in {"financials", "events", "forecasts"} or (module == "forecasts" and version != 2):
        raise ArtifactContractError("unsupported research module/version")
    started, completed = (
        _time(payload.get(key))
        for key in ("collection_started_at", "collection_completed_at")
    )
    if started > completed:
        raise ArtifactContractError("research collection timing is reversed")
    coverage = payload.get("coverage")
    if not isinstance(coverage, dict) or set(coverage) != {
        "thscodes",
        "complete",
        "period_start",
        "period_end",
    }:
        raise ArtifactContractError(
            "research coverage requires explicit codes, complete, period_start/end"
        )
    codes = coverage.get("thscodes")
    if not isinstance(codes, list) or not all(
        isinstance(code, str) and CODE_PATTERN.fullmatch(code) for code in codes
    ):
        raise ArtifactContractError("invalid research coverage codes")
    if not isinstance(coverage["complete"], bool):
        raise ArtifactContractError("coverage complete must be an observed boolean")
    if _date(coverage["period_start"]) > _date(coverage["period_end"]):
        raise ArtifactContractError("research coverage period is reversed")
    rows = payload.get("records")
    if not isinstance(rows, list):
        raise ArtifactContractError("research records must be a list")
    normalized = []
    for source in rows:
        specific = (
            {
                "report_period",
                "period_start",
                "period_basis",
                "accounting_scope",
                "currency",
                "unit",
                "values",
            }
            if module == "financials"
            else ({"forecast_year", "institution_id", "estimate_basis", "contributor_count", "values", "unit"}
                  if module == "forecasts" else {"event_id", "event_type", "event_date", "status", "title"})
        )
        if module == "events" and version == 2:
            specific |= {"details", "details_source", "report_period"}
        if (not isinstance(source, dict)
                or set(source) - OPTIONAL_SOURCE_FIELDS != COMMON_FIELDS | specific):
            raise ArtifactContractError(
                "research record must contain only the documented normalized fields"
            )
        row = dict(source)
        if "source_url_kind" in row and row["source_url_kind"] not in {
            "public_document", "provider_query_documentation"
        }:
            raise ArtifactContractError("unknown source URL kind")
        if "document_access" in row and row["document_access"] not in {
            "public_link_not_downloaded", "authenticated_link_omitted", "not_resolved"
        }:
            raise ArtifactContractError("unknown document access state")
        if row["thscode"] not in codes:
            raise ArtifactContractError("research record is outside declared coverage")
        published, seen, known = (
            _time(row[key]) for key in ("published_at", "first_seen_at", "known_at")
        )
        if not published <= seen <= known <= completed:
            raise ArtifactContractError(
                "research PIT requires published_at <= first_seen_at <= known_at <= collection_completed_at"
            )
        for key, stamp in (
            ("published_at", published),
            ("first_seen_at", seen),
            ("known_at", known),
        ):
            row[key] = stamp.isoformat()
        if (
            not isinstance(row["revision"], str)
            or not row["revision"]
            or len(row["revision"]) > 64
        ):
            raise ArtifactContractError(
                "explicit bounded revision identifier is required"
            )
        parsed = urlparse(str(row["source_url"]))
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise ArtifactContractError(
                "source_url must be an attributed HTTPS link without credentials"
            )
        if any(
            re.search(r"token|secret|password|authorization|api.?key", key, re.I)
            for key, _ in parse_qsl(parsed.query)
        ):
            raise ArtifactContractError(
                "source_url must not persist authentication parameters"
            )
        if row["document_sha256"] is not None and not re.fullmatch(
            r"[a-f0-9]{64}", str(row["document_sha256"])
        ):
            raise ArtifactContractError(
                "document_sha256 must be a hash or explicit null"
            )
        if module == "financials":
            period = _date(row["report_period"])
            if not coverage["period_start"] <= period <= coverage["period_end"]:
                raise ArtifactContractError(
                    "financial report period is outside coverage"
                )
            if (
                row["period_basis"] != "YTD"
                or row["accounting_scope"] != "consolidated"
                or row["currency"] != "CNY"
            ):
                raise ArtifactContractError(
                    "financial v1 supports consolidated CNY calendar-year YTD only"
                )
            if (
                period[5:] not in {"03-31", "06-30", "09-30", "12-31"}
                or row["period_start"] != period[:4] + "-01-01"
            ):
                raise ArtifactContractError("unsupported financial fiscal period")
            if period > published.tz_convert("Asia/Shanghai").date().isoformat():
                raise ArtifactContractError(
                    "financial statement cannot precede report period end"
                )
            scales = {"CNY": 1, "CNY_1e4": 10_000, "CNY_1e8": 100_000_000}
            if (
                row["unit"] not in scales
                or not isinstance(row["values"], dict)
                or set(row["values"]) - set(FINANCIAL_FIELDS if version == 1 else FINANCIAL_V2_FIELDS)
            ):
                raise ArtifactContractError("unsupported financial unit or metric")
            fields = FINANCIAL_FIELDS if version == 1 else FINANCIAL_V2_FIELDS
            values = {key: _number(row["values"].get(key)) for key in fields}
            row["values"] = {
                key: value * scales[row["unit"]] if value is not None else None
                for key, value in values.items()
            }
            row["unit"] = "CNY"
        elif module == "forecasts":
            if (type(row["forecast_year"]) is not int or not 2000 <= row["forecast_year"] <= 2100
                    or row["estimate_basis"] not in {"individual_institution", "provider_consensus"}
                    or not isinstance(row["institution_id"], str) or not 1 <= len(row["institution_id"]) <= 100
                    or row["unit"] != "CNY" or not isinstance(row["values"], dict)
                    or set(row["values"]) - FORECAST_FIELDS):
                raise ArtifactContractError("invalid forecast definition, fiscal year, identity or unit")
            count = row["contributor_count"]
            if count is not None and (type(count) is not int or count < 1):
                raise ArtifactContractError("forecast contributor count must be positive or null")
            if not coverage["period_start"] <= published.tz_convert("Asia/Shanghai").date().isoformat() <= coverage["period_end"]:
                raise ArtifactContractError("forecast publication outside coverage")
            row["values"] = {key: _number(row["values"].get(key)) for key in sorted(FORECAST_FIELDS)}
        else:
            _date(row["event_date"])
            if (
                not coverage["period_start"]
                <= published.tz_convert("Asia/Shanghai").date().isoformat()
                <= coverage["period_end"]
            ):
                raise ArtifactContractError(
                    "event publication date is outside coverage"
                )
            if row["event_type"] not in EVENT_TYPES or row["status"] not in {
                "announced",
                "approved",
                "in_progress",
                "completed",
                "cancelled",
                "unknown",
            }:
                raise ArtifactContractError("unsupported event type or status")
            if (
                not isinstance(row["event_id"], str)
                or not row["event_id"]
                or len(row["event_id"]) > 128
                or not isinstance(row["title"], str)
                or len(row["title"]) > 500
            ):
                raise ArtifactContractError(
                    "event identity/title is missing or oversized"
                )
            if version == 2:
                if (not isinstance(row["details"], dict) or set(row["details"]) - EVENT_DETAIL_FIELDS
                        or row["details_source"] != "provider_structured_fields"):
                    raise ArtifactContractError("event details require explicit structured provider fields")
                row["details"] = {key: _number(value) for key, value in sorted(row["details"].items())}
                if row["report_period"] is not None:
                    _date(row["report_period"])
                for field in ("announced_amount_cny", "executed_amount_cny", "share_count", "cash_dividend_per_share_cny"):
                    if row["details"].get(field) is not None and row["details"][field] < 0:
                        raise ArtifactContractError("event amount/share count cannot be negative")
                lo, hi = row["details"].get("profit_lower_cny"), row["details"].get("profit_upper_cny")
                if lo is not None and hi is not None and lo > hi:
                    raise ArtifactContractError("earnings preview interval is reversed")
        normalized.append(row)
    result = {
        "schema_version": version,
        "module": module,
        "collection_started_at": started.isoformat(),
        "collection_completed_at": completed.isoformat(),
        "coverage": {**coverage, "thscodes": sorted(set(codes))},
        "records": sorted(
            {json_sha256(row): row for row in normalized}.values(),
            key=lambda row: (row["thscode"], row["known_at"], json_sha256(row)),
        ),
    }
    # Validate all revisions, including mutually inconsistent duplicates.
    _latest_revisions(result["records"], module)
    return result


def import_research_batch(data_dir: Path, source_path: Path) -> dict:
    batch = normalize_research_batch(load_json_object(source_path, missing_ok=False))
    digest = json_sha256(batch)
    path = data_dir / "facts" / "research" / batch["module"] / f"{digest}.json"
    existed = path.exists()
    if existed:
        if load_json_object(path, missing_ok=False) != batch:
            raise ArtifactContractError("immutable research batch hash collision")
    else:
        atomic_write_json(path, batch, compact=True)
    return {
        "ok": True,
        "module": batch["module"],
        "records": len(batch["records"]),
        "reused": existed,
        "batch_sha256": digest,
    }


def _latest_revisions(rows: list[dict], module: str) -> list[dict]:
    identities = {}
    for row in sorted(
        rows, key=lambda row: (row["known_at"], row["published_at"], row["revision"])
    ):
        key = (row["thscode"], row["report_period"]) if module == "financials" else (
            (row["thscode"], row["forecast_year"], row["institution_id"], row["estimate_basis"])
            if module == "forecasts" else (row["thscode"], row["event_id"])
        )
        previous = identities.get(key)
        if previous and previous["known_at"] == row["known_at"] and previous != row:
            raise ArtifactContractError(
                "ambiguous research revisions at the same known_at"
            )
        identities[key] = row
    return list(identities.values())


def financial_features(rows: list[dict], market_cap: float | None) -> dict | None:
    if not rows:
        return None
    by_period = {
        row["report_period"]: row for row in _latest_revisions(rows, "financials")
    }
    latest = by_period[max(by_period)]
    period = latest["report_period"]
    year = int(period[:4])
    values = latest["values"]
    prior = by_period.get(f"{year - 1}{period[4:]}", {}).get("values", {})
    annual = by_period.get(f"{year - 1}-12-31", {}).get("values", {})
    previous_quarter = {"06-30": "03-31", "09-30": "06-30", "12-31": "09-30"}.get(
        period[5:]
    )
    previous = by_period.get(f"{year}-{previous_quarter}", {}).get("values", {})
    yoy, quarter, ttm = {}, {}, {}
    for field in sorted(FLOW_FIELDS):
        value, base = values.get(field), prior.get(field)
        yoy[field] = (
            (value / base - 1) * 100
            if value is not None and base is not None and base > 0
            else None
        )
        preceding = previous.get(field)
        quarter[field] = (
            value
            if previous_quarter is None
            else (
                value - preceding
                if value is not None and preceding is not None
                else None
            )
        )
        full = annual.get(field)
        ttm[field] = (
            value
            if period.endswith("12-31")
            else (
                value + full - base
                if all(item is not None for item in (value, full, base))
                else None
            )
        )
    profit, ocf, equity = (
        values.get("net_profit_parent"),
        values.get("operating_cash_flow"),
        values.get("equity_parent"),
    )

    def ratio(denominator):
        return (
            market_cap / denominator
            if market_cap is not None and denominator is not None and denominator > 0
            else None
        )

    return {
        "latest_statement": latest,
        "quarter_flows": quarter,
        "quality_facts": financial_quality(by_period, quarter, ttm),
        "ttm_flows": ttm,
        "yoy_pct": yoy,
        "basis_statement_hashes": sorted(
            json_sha256(row) for row in by_period.values()
        ),
        "profit_positive_cashflow_negative": (
            None if profit is None or ocf is None else profit > 0 and ocf < 0
        ),
        "nonrecurring_profit_share": (
            (profit - values["net_profit_ex_nonrecurring"]) / profit
            if profit is not None
            and profit > 0
            and values.get("net_profit_ex_nonrecurring") is not None
            else None
        ),
        "valuation": {
            "pe_ttm": ratio(ttm.get("net_profit_parent")),
            "pb": ratio(equity),
            "ps_ttm": ratio(ttm.get("revenue")),
            "ps_denominator": "consolidated_revenue_ttm_cny",
            "pe_denominator": "consolidated_parent_net_profit_ttm_cny",
            "pb_denominator": "consolidated_parent_equity_cny",
            "pe_state": (
                "unknown"
                if market_cap is None or ttm.get("net_profit_parent") is None
                else "not_meaningful" if ttm["net_profit_parent"] <= 0 else "observed"
            ),
            "pb_state": (
                "unknown"
                if market_cap is None or equity is None
                else "not_meaningful" if equity <= 0 else "observed"
            ),
            "dividend_yield": None,
            "historical_percentile": None,
            "peer_percentile": None,
            "unavailable_reason": "dividend_basis_long_history_and_pit_peers_not_integrated",
        },
    }


def event_price_response(
    event: dict, history: pd.DataFrame, sessions: list[str]
) -> dict:
    stamp = _time(event["published_at"])
    eligible = [day for day in sessions if _time(f"{day}T09:30:00+08:00") > stamp]
    series = history.loc[history["thscode"].eq(event["thscode"])].set_index(
        "trade_date"
    )
    returns = pd.to_numeric(series["change_ratio"], errors="coerce").reindex(eligible)
    return {
        "basis": "first_full_session_after_publication; compounded_provider_change_ratio_not_abnormal_return",
        "first_full_session": eligible[0] if eligible else None,
        "elapsed_sessions": len(eligible),
        "return_pct": (
            float(((1 + returns / 100).prod() - 1) * 100)
            if len(returns) and returns.notna().all()
            else None
        ),
        "state": "observed" if len(returns) and returns.notna().all() else "not_ready",
    }


def _bounded(payload: dict, name: str) -> bytes:
    encoded = serialize_json(payload, compact=True).encode("utf-8")
    if len(encoded) > HARD_MAX_BYTES:
        raise ArtifactContractError(
            f"{name} exceeds research hard limit {HARD_MAX_BYTES}; no silent truncation"
        )
    return encoded


def build_research_artifacts(
    data_dir: Path,
    source: dict,
    state: pd.DataFrame,
    history: pd.DataFrame,
    reference: pd.DataFrame,
) -> dict[str, dict]:
    market_cutoff = _time(source["pit_timing"]["effective_pit_cutoff"])
    configured = _time(source["pit_timing"]["configured_decision_cutoff"])
    batches: dict[str, list[dict]] = {"financials": [], "events": [], "forecasts": []}
    hashes = {module: [] for module in batches}
    for module in batches:
        for path in sorted((data_dir / "facts" / "research" / module).glob("*.json")):
            batch = normalize_research_batch(load_json_object(path, missing_ok=False))
            if batch["module"] != module or json_sha256(batch) != path.stem:
                raise ArtifactContractError("research batch identity/hash mismatch")
            if _time(batch["collection_completed_at"]) <= configured:
                batches[module].append(batch)
                hashes[module].append(path.stem)
    completion = max(
        [market_cutoff]
        + [
            _time(batch["collection_completed_at"])
            for group in batches.values()
            for batch in group
        ]
    )
    sessions = history.attrs.get("session_dates") or sorted(
        history["trade_date"].unique()
    )
    forecast_sessions = [day for day in sessions if day <= source["trade_date"]][-21:]
    sessions = [day for day in sessions if day <= source["trade_date"]][-20:]
    window_start = sessions[0] if sessions else source["trade_date"]
    records = {
        module: (_latest_revisions(
            [
                row
                for batch in group
                for row in batch["records"]
                if _time(row["known_at"]) <= completion
            ],
            module,
        ) if module != "forecasts" else [row for batch in group for row in batch["records"]
                                         if _time(row["known_at"]) <= completion])
        for module, group in batches.items()
    }
    modules = {}
    universe = sorted(reference["thscode"].unique())
    entitlement_observations = {}
    for path in sorted(
        (data_dir / "facts" / "module_status").glob("as_of_date=*/module_status.json")
    ):
        for module, observation in (
            load_json_object(path, missing_ok=False).get("modules") or {}
        ).items():
            if (
                module in batches
                and observation.get("checked_at_utc")
                and _time(observation["checked_at_utc"]) <= completion
            ):
                previous = entitlement_observations.get(module)
                if previous is None or _time(previous["checked_at_utc"]) < _time(
                    observation["checked_at_utc"]
                ):
                    entitlement_observations[module] = observation
    for module in batches:
        queried = {
            code for batch in batches[module] for code in batch["coverage"]["thscodes"]
        }
        observed = {row["thscode"] for row in records[module]}
        modules[module] = {
            "state": (
                "missing"
                if not batches[module]
                else (
                    "partial"
                    if len(observed & set(universe)) < len(universe)
                    else "ready"
                )
            ),
            "provider_adapter": "source_attributed_normalized_batches; live_scope_in_supplemental_manifest",
            "queried_codes": len(queried & set(universe)),
            "observed_codes": len(observed & set(universe)),
            "universe_count": len(universe),
            "source_batch_hashes": hashes[module],
            "collection_completed_at": max(
                (batch["collection_completed_at"] for batch in batches[module]),
                default=None,
            ),
        }
        entitlement = entitlement_observations.get(module)
        if entitlement:
            modules[module]["entitlement_check"] = entitlement
            if not batches[module] and entitlement.get("state") == "not_entitled":
                modules[module]["state"] = "not_entitled"
    outputs: dict[str, dict] = {}
    index: dict[str, list[dict]] = {}
    grouped_rows: dict[str, list[dict]] = {}
    route_counts: dict[str, int] = {}
    field_counts = {field: 0 for field in FINANCIAL_V2_FIELDS}
    valuation_counts = {"pe_ttm": 0, "pb": 0, "ps_ttm": 0}
    stock_by_code = state.set_index("thscode")
    names = reference.drop_duplicates("thscode").set_index("thscode")["security_name"]
    financial_by_code: dict[str, list[dict]] = {}
    event_by_code: dict[str, list[dict]] = {}
    forecast_by_code: dict[str, list[dict]] = {}
    for row in records["financials"]:
        financial_by_code.setdefault(row["thscode"], []).append(row)
    for row in records["events"]:
        if row["published_at"] >= _time(f"{window_start}T00:00:00+08:00").isoformat():
            event_by_code.setdefault(row["thscode"], []).append(row)
    for row in records["forecasts"]:
        forecast_by_code.setdefault(row["thscode"], []).append(row)
    for code in universe:
        stock = (
            stock_by_code.loc[code]
            if code in stock_by_code.index
            else pd.Series(dtype=object)
        )

        def observed(field):
            value = stock.get(field)
            return None if value is None or pd.isna(value) else round(float(value), 6)

        financial = financial_features(
            financial_by_code.get(code, []), observed("total_market_cap")
        )
        if financial:
            for field in field_counts:
                field_counts[field] += (
                    financial["latest_statement"]["values"].get(field) is not None
                )
            for field in valuation_counts:
                valuation_counts[field] += financial["valuation"][field] is not None
        events = [
            {**row, "price_response": event_price_response(row, history, sessions)}
            for row in sorted(
                event_by_code.get(code, []),
                key=lambda row: (row["published_at"], row["event_id"]),
            )
        ]
        event_complete = any(
            batch["coverage"]["complete"]
            and code in batch["coverage"]["thscodes"]
            and batch["coverage"]["period_start"] <= window_start
            and batch["coverage"]["period_end"] >= source["trade_date"]
            and _time(batch["collection_completed_at"]) >= completion
            for batch in batches["events"]
        )
        routes = []
        if financial:
            if (
                financial["yoy_pct"].get("net_profit_parent") is not None
                and financial["yoy_pct"]["net_profit_parent"] > 0
            ):
                routes.append("parent_profit_yoy_positive")
            if financial["profit_positive_cashflow_negative"] is True:
                routes.append("positive_profit_negative_operating_cashflow")
        routes.extend(sorted({"event_" + row["event_type"] for row in events}))
        for route in routes:
            route_counts[route] = route_counts.get(route, 0) + 1
        row = {
            "thscode": code,
            "security_name": str(names.loc[code]),
            "tape": {
                field: observed(field)
                for field in (
                    "raw_close",
                    "raw_return_1d_pct",
                    "raw_return_3d_pct",
                    "raw_return_5d_pct",
                    "raw_return_20d_pct",
                    "amount_z20",
                    "adt20",
                    "total_market_cap",
                )
            },
            "financials": financial,
            "events": events,
            "forecasts": forecast_features(forecast_by_code.get(code, []), completion.isoformat(), forecast_sessions),
            "availability": {
                "financials": (
                    "observed"
                    if financial
                    else "unknown" if batches["financials"] else "not_ready"
                ),
                "events": (
                    "observed"
                    if events
                    else (
                        "confirmed_no_events_in_window"
                        if event_complete
                        else "unknown" if batches["events"] else "not_ready"
                    )
                ),
                "forecasts": "observed" if forecast_by_code.get(code) else "unknown" if batches["forecasts"] else "not_ready",
            },
            "discovery_routes": routes,
        }
        prefix = hashlib.sha256(code.encode("ascii")).hexdigest()[0]
        grouped_rows.setdefault(prefix, []).append(row)
    if "sw1_code" in state:
        groups = {str(row["thscode"]): str(row["sw1_code"]) for row in state.to_dict("records")
                  if pd.notna(row.get("sw1_code"))}
        # A partial classification universe is not an all-industry peer sample.
        if set(groups) >= set(universe):
            peer_valuation_percentiles([row for group in grouped_rows.values() for row in group], groups)
    for prefix, rows in sorted(grouped_rows.items()):
        pages: list[list[dict]] = [[]]
        size = 0
        for row in rows:
            row_size = len(serialize_json(row, compact=True).encode("utf-8"))
            if pages[-1] and size + row_size > SHARD_TARGET_BYTES:
                pages.append([])
                size = 0
            pages[-1].append(row)
            size += row_size
        index[prefix] = []
        for page, items in enumerate(pages, 1):
            name = f"research_{prefix}_{page:03d}.json"
            shard = {
                "schema_version": 1,
                "trade_date": source["trade_date"],
                "source_snapshot_sha256": source["source_snapshot_sha256"],
                "research_effective_pit_cutoff": completion.isoformat(),
                "securities": items,
            }
            encoded = _bounded(shard, name)
            outputs[name] = shard
            index[prefix].append(
                {
                    "path": name,
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "bytes": len(encoded),
                    "rows": len(items),
                    "first_code": items[0]["thscode"],
                    "last_code": items[-1]["thscode"],
                }
            )
    root = {
        "schema_version": 1,
        "document_type": "a_share_research_facts_index",
        "trade_date": source["trade_date"],
        "generated_at": completion.isoformat(),
        "generated_at_semantics": "maximum_actual_source_completion_not_build_wall_clock",
        "source_snapshot_sha256": source["source_snapshot_sha256"],
        "feature_input_sha256": source.get("feature_input_sha256"),
        "market_effective_pit_cutoff": market_cutoff.isoformat(),
        "research_effective_pit_cutoff": completion.isoformat(),
        "configured_decision_cutoff": configured.isoformat(),
        "modules": modules,
        "field_coverage": {
            "financial_non_null_counts": field_counts,
            "valuation_non_null_counts": valuation_counts,
            "denominator": len(universe),
        },
        "event_window": {
            "start": window_start,
            "end": source["trade_date"],
            "basis": "publication_date; last_20_market_sessions",
        },
        "discovery_routes": route_counts,
        "universe_count": len(universe),
        "lookup_policy": "sha256(ASCII thscode) first hex character; pages sorted by thscode, size bounded; all PIT reference securities, not only tape Top100",
        "shards": index,
        "data_mode": "facts_and_deterministic_features_only",
        "evidence_domains": {
            "TAPE": "price/liquidity subfamilies are not independent evidence",
            "FINANCIAL": "reported statements; YoY is not an expectations beat",
            "EVENT": "published event facts; planned is not completed",
            "EXPECTATIONS": "attributed forecasts, not realized financial results",
        },
        "valuation_policy": "PE/PB use positive observed denominators only; no invented dividend yield or short-history valuation percentile",
        "unimplemented_modules": [
            "full_financial_statement_indicator_mapping",
            "industry_operating_data",
            "historical_valuation_percentiles",
        ],
        "hard_max_bytes_per_file": HARD_MAX_BYTES,
    }
    _bounded(root, "research_inputs_latest.json")
    outputs["research_inputs_latest.json"] = root
    return outputs
