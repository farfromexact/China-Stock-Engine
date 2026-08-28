"""Bounded, deterministic LLM-facing opportunity radar inputs."""

from __future__ import annotations

import math
from typing import Any

from .storage import (
    ArtifactContractError,
    MAX_OPPORTUNITY_RADAR_BYTES,
    serialize_json,
)


OPPORTUNITY_RADAR_SCHEMA_VERSION = 1
DEFAULT_RADAR_CANDIDATE_LIMIT = 100
RADAR_SOFT_TARGET_MIN_BYTES = 220 * 1024
RADAR_SOFT_TARGET_MAX_BYTES = 250 * 1024

RADAR_FACT_FIELDS = (
    "exchange",
    "board",
    "raw_close",
    "change_ratio",
    "amount",
    "turnover_ratio",
    "amount_rank",
    "turnover_rank",
    "amount_change_pct",
    "turnover_change_pct",
    "close_location",
    "gap_pct",
    "intraday_range_pct",
    "raw_return_1d_pct",
    "raw_return_3d_pct",
    "raw_return_5d_pct",
    "raw_return_20d_pct",
    "float_market_cap",
    "market_cap_bucket",
    "amount_to_float_market_cap",
    "amount_z20",
    "turnover_z20",
    "volume_z20",
    "adt20",
    "daily_price_limit_pct",
)

RADAR_PERCENTILE_FIELDS = (
    "return_1d_pctile",
    "return_3d_pctile",
    "return_5d_pctile",
    "return_20d_pctile",
    "amount_change_pctile",
    "turnover_change_pctile",
    "close_location_pctile",
)

AVAILABILITY_STATE_CONTRACT = {
    "not_ready": (
        "required source module is missing, stale, or not entitled and no "
        "security-level value is observed"
    ),
    "unknown": (
        "source module is ready or partial but this security-level value is "
        "not known; unknown is never false"
    ),
    "confirmed_false": "the source fact explicitly observed boolean false",
    "confirmed_true": "the source fact explicitly observed boolean true",
    "confirmed_value": "the source fact explicitly observed a non-boolean value",
    "confirmed_clear": "tradability_state explicitly observed clear",
    "confirmed_restricted": "tradability_state explicitly observed restricted",
}

UNREADY_MODULE_STATES = {"missing", "not_entitled", "stale"}


def _screen_family(screen: str) -> str:
    if screen.startswith("board_neutral_absolute_move__"):
        return "board_neutral_move"
    if screen.startswith("market_cap_neutral_absolute_move__"):
        return "market_cap_neutral_move"
    if screen in {"amount_expansion", "turnover_expansion", "highest_amount"}:
        return "liquidity_activity"
    if screen.startswith("gap_") or screen.startswith("large_intraday_range_"):
        return "gap_intraday_structure"
    if screen.startswith("large_positive_") or screen.startswith("large_negative_"):
        return "return_close_location"
    if screen in {"largest_positive_moves", "largest_negative_moves"}:
        return "directional_price_move"
    if screen in {
        "momentum_acceleration_1d_vs_3d_5d",
        "positive_momentum_1d_3d_5d",
        "strong_5d_negative_1d_pullback",
        "weak_5d_positive_1d_reversal",
    }:
        return "multi_horizon_momentum"
    if screen.startswith("price_up_activity_") or screen.startswith(
        "price_down_activity_"
    ):
        return "price_activity_confirmation"
    if screen.startswith("relative_strength_"):
        return "multi_horizon_relative_strength"
    if screen in {"strong_close_location", "weak_close_location"}:
        return "close_location"
    raise ArtifactContractError(
        f"opportunity_radar_latest.json has unmapped screen family: {screen}"
    )


def _finite(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return int(value) if isinstance(value, int) else round(number, 6)
    return None


def _module_state(source: dict[str, Any], module: str) -> str:
    readiness = source.get("readiness") or {}
    value = str(((readiness.get(module) or {}).get("state")) or "missing")
    return value


def _missing_observation(module_state: str) -> dict[str, Any]:
    return {
        "state": "not_ready"
        if module_state in UNREADY_MODULE_STATES
        else "unknown",
        "value": None,
    }


def _boolean_observation(value: Any, module_state: str) -> dict[str, Any]:
    if isinstance(value, bool):
        return {
            "state": "confirmed_true" if value else "confirmed_false",
            "value": value,
        }
    return _missing_observation(module_state)


def _numeric_observation(value: Any, module_state: str) -> dict[str, Any]:
    number = _finite(value)
    if number is not None:
        return {"state": "confirmed_value", "value": number}
    return _missing_observation(module_state)


def _tradability_observation(
    value: Any, module_state: str
) -> dict[str, Any]:
    normalized = str(value or "unknown")
    if normalized == "clear":
        return {"state": "confirmed_clear", "value": "clear"}
    if normalized == "restricted":
        return {"state": "confirmed_restricted", "value": "restricted"}
    if normalized != "unknown":
        raise ArtifactContractError(
            "opportunity_radar_latest.json has unsupported tradability_state: "
            f"{normalized}"
        )
    observation = _missing_observation(module_state)
    observation["value"] = None
    return observation


def _candidate_order_key(row: dict[str, Any]) -> tuple[int, int, str]:
    thscode = str(row.get("thscode") or "")
    if not thscode:
        raise ArtifactContractError(
            "opportunity_radar_latest.json candidate is missing thscode"
        )
    try:
        screen_count = int(row.get("screen_count"))
        best_screen_rank = int(row.get("best_screen_rank"))
    except (TypeError, ValueError) as exc:
        raise ArtifactContractError(
            f"opportunity_radar_latest.json candidate ordering is invalid: {thscode}"
        ) from exc
    if screen_count < 1 or best_screen_rank < 1:
        raise ArtifactContractError(
            f"opportunity_radar_latest.json candidate ordering is invalid: {thscode}"
        )
    return (-screen_count, best_screen_rank, thscode)


def _candidate_availability(
    facts: dict[str, Any], tradability_module_state: str
) -> dict[str, Any]:
    return {
        "tradability": _tradability_observation(
            facts.get("tradability_state"), tradability_module_state
        )["state"],
        "is_st": _boolean_observation(
            facts.get("is_st"), tradability_module_state
        )["state"],
        "is_suspended": _boolean_observation(
            facts.get("is_suspended"), tradability_module_state
        )["state"],
        "daily_price_limit_pct": _numeric_observation(
            facts.get("daily_price_limit_pct"), tradability_module_state
        )["state"],
        "limit_up": _boolean_observation(
            facts.get("limit_up"), tradability_module_state
        )["state"],
        "limit_down": _boolean_observation(
            facts.get("limit_down"), tradability_module_state
        )["state"],
        "one_word_limit": _boolean_observation(
            facts.get("one_word_limit"), tradability_module_state
        )["state"],
    }


def _compact_candidate(
    row: dict[str, Any], *, union_order: int, tradability_module_state: str
) -> dict[str, Any]:
    triggered = sorted({str(name) for name in row.get("triggered_screens") or []})
    if not triggered:
        raise ArtifactContractError(
            "opportunity_radar_latest.json candidate has no triggered screens: "
            f"{row.get('thscode')}"
        )
    ranks = row.get("screen_ranks") or {}
    percentiles = row.get("screen_percentiles") or {}
    screen_evidence: list[dict[str, Any]] = []
    evidence_families: set[str] = set()
    for screen in triggered:
        family = _screen_family(screen)
        evidence_families.add(family)
        try:
            screen_rank = int(ranks.get(screen))
        except (TypeError, ValueError) as exc:
            raise ArtifactContractError(
                "opportunity_radar_latest.json screen rank is invalid: "
                f"{row.get('thscode')} {screen}"
            ) from exc
        screen_evidence.append(
            {
                "screen": screen,
                "rank": screen_rank,
                "percentile": _finite(percentiles.get(screen)),
            }
        )
    facts = dict(row.get("facts") or {})
    compact_facts = {field: facts.get(field) for field in RADAR_FACT_FIELDS}
    source_percentiles = row.get("percentiles") or {}
    compact_percentiles = {
        field: source_percentiles.get(field) for field in RADAR_PERCENTILE_FIELDS
    }
    return {
        "union_order": union_order,
        "thscode": str(row.get("thscode")),
        "security_name": row.get("security_name"),
        "screen_count": int(row.get("screen_count")),
        "best_screen_rank": int(row.get("best_screen_rank")),
        "evidence_families": sorted(evidence_families),
        "screen_evidence": screen_evidence,
        "percentiles": compact_percentiles,
        "facts": compact_facts,
        "availability": _candidate_availability(
            facts, tradability_module_state
        ),
        "contradiction_flags": sorted(
            {str(flag) for flag in row.get("contradiction_flags") or []}
        ),
    }


def _screen_catalog(
    source: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    screens = source.get("deterministic_screens") or {}
    triggered = sorted(
        {
            evidence["screen"]
            for candidate in candidates
            for evidence in candidate["screen_evidence"]
        }
    )
    catalog: dict[str, dict[str, Any]] = {}
    for name in triggered:
        screen = screens.get(name)
        if not isinstance(screen, dict):
            raise ArtifactContractError(
                f"opportunity_radar_latest.json is missing screen metadata: {name}"
            )
        catalog[name] = {
            "evidence_family": _screen_family(name),
            "definition": screen.get("definition"),
            "metric": screen.get("metric"),
            "scope": screen.get("scope"),
        }
    return catalog


def build_opportunity_radar_inputs(
    source: dict[str, Any], *, candidate_limit: int = DEFAULT_RADAR_CANDIDATE_LIMIT
) -> dict[str, Any]:
    """Build a bounded factual interface without subjective scoring or advice."""

    if candidate_limit < 1 or candidate_limit > DEFAULT_RADAR_CANDIDATE_LIMIT:
        raise ArtifactContractError(
            "opportunity radar candidate_limit must be between 1 and 100"
        )
    generated_at = str(source.get("generated_at") or "")
    pit_timing = source.get("pit_timing") or {}
    collection_completed_at = str(pit_timing.get("collection_completed_at") or "")
    if not generated_at or generated_at != collection_completed_at:
        raise ArtifactContractError(
            "opportunity radar generated_at must equal source collection_completed_at"
        )
    source_snapshot_sha256 = str(source.get("source_snapshot_sha256") or "")
    if len(source_snapshot_sha256) != 64:
        raise ArtifactContractError(
            "opportunity radar source_snapshot_sha256 must be a 64-character hash"
        )
    source_candidates = source.get("candidate_union") or []
    if not isinstance(source_candidates, list):
        raise ArtifactContractError(
            "opportunity radar source candidate_union must be a list"
        )
    codes = [str(row.get("thscode") or "") for row in source_candidates]
    if len(codes) != len(set(codes)):
        raise ArtifactContractError(
            "opportunity radar source candidate_union contains duplicate thscode"
        )
    ordered = sorted(source_candidates, key=_candidate_order_key)
    tradability_module_state = _module_state(source, "tradability")
    candidates = [
        _compact_candidate(
            row,
            union_order=union_order,
            tradability_module_state=tradability_module_state,
        )
        for union_order, row in enumerate(ordered[:candidate_limit], start=1)
    ]
    payload = {
        "schema_version": OPPORTUNITY_RADAR_SCHEMA_VERSION,
        "document_type": "a_share_opportunity_radar_inputs",
        "trade_date": str(source.get("trade_date") or ""),
        "generated_at": generated_at,
        "generated_at_semantics": "source_collection_completed_at",
        "source_snapshot_sha256": source_snapshot_sha256,
        "pit_timing": pit_timing,
        "data_mode": source.get("data_mode") or {},
        "readiness": source.get("readiness") or {},
        "field_coverage": source.get("field_coverage") or {},
        "market": source.get("market") or {},
        "board_summary": source.get("board_summary") or [],
        "market_cap_bucket_summary": source.get("market_cap_bucket_summary") or {},
        "data_quality_drift": source.get("data_quality_drift"),
        "cross_sectional_features": source.get("cross_sectional_features") or {},
        "availability_state_contract": AVAILABILITY_STATE_CONTRACT,
        "screen_catalog": _screen_catalog(source, candidates),
        "candidate_union_metadata": {
            "source_candidate_count": len(source_candidates),
            "returned_count": len(candidates),
            "limit": candidate_limit,
            "truncation_applied": len(source_candidates) > candidate_limit,
            "ordering_policy": [
                "screen_count descending",
                "best_screen_rank ascending",
                "thscode ascending",
            ],
            "union_order_semantics": (
                "deterministic transport order only; not a relative "
                "attractiveness ordering and no subjective weighting is applied"
            ),
            "screen_count_semantics": (
                "raw count of deterministic filters that captured the security"
            ),
        },
        "candidate_union": candidates,
        "contradiction_flag_definitions": source.get(
            "contradiction_flag_definitions"
        )
        or {},
        "artifact_contract": {
            "soft_target_min_bytes": RADAR_SOFT_TARGET_MIN_BYTES,
            "soft_target_max_bytes": RADAR_SOFT_TARGET_MAX_BYTES,
            "hard_max_bytes": MAX_OPPORTUNITY_RADAR_BYTES,
            "oversize_policy": (
                "fail build and preserve the last valid latest; never truncate"
            ),
        },
        "full_inputs": {
            "path": "opportunity_inputs_latest.json",
            "schema_version": source.get("schema_version"),
        },
        "drilldown": source.get("drilldown") or {},
    }
    encoded = serialize_json(payload, compact=True).encode("utf-8")
    if len(encoded) > MAX_OPPORTUNITY_RADAR_BYTES:
        raise ArtifactContractError(
            "opportunity_radar_latest.json is "
            f"{len(encoded)} bytes; hard limit is {MAX_OPPORTUNITY_RADAR_BYTES}"
        )
    return payload


__all__ = [
    "AVAILABILITY_STATE_CONTRACT",
    "DEFAULT_RADAR_CANDIDATE_LIMIT",
    "OPPORTUNITY_RADAR_SCHEMA_VERSION",
    "RADAR_SOFT_TARGET_MAX_BYTES",
    "RADAR_SOFT_TARGET_MIN_BYTES",
    "build_opportunity_radar_inputs",
]
