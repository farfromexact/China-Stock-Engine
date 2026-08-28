"""Daily A-share collection, quality-gated promotion, and validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .ifind_http import IFindHTTPClient
from .quality import (
    build_daily_security_status,
    build_market_summary,
    frame_hash,
    normalize_quotes,
    normalize_security_reference,
    normalize_trade_calendar,
    normalize_universe,
    schema_signature,
    validate_data,
)
from .data_reference import (
    build_data_reference_outputs,
    data_cutoff_time_for_date,
)
from .storage import (
    ArtifactContractError,
    load_json_object,
    load_manifest,
    promote_snapshot,
    promote_verified_snapshot_to_latest,
    verify_latest_artifacts,
    write_run_status,
)


ProgressCallback = Callable[[str, int, int], None]


@dataclass(frozen=True)
class CollectionConfig:
    data_dir: Path = Path("data")
    min_universe_size: int = 5000
    min_quote_coverage: float = 0.98
    min_reference_coverage: float = 0.98
    min_extended_field_coverage: float = 0.95
    reference_batch_size: int = 100
    quote_batch_size: int = 300
    request_interval_seconds: float = 0.15
    trade_calendar_offset: int = -10
    history_limit: int = 20
    snapshot_limit: int = 60
    build_data_reference: bool = True


@dataclass(frozen=True)
class CollectionResult:
    ok: bool
    status: dict[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path) -> dict[str, Any]:
    return load_json_object(path)


def _latest_manifest(data_dir: Path) -> dict[str, Any]:
    return load_manifest(data_dir / "latest" / "manifest.json")


def _last_run_status(data_dir: Path) -> dict[str, Any]:
    return _load_json(data_dir / "last_run_status.json")


def _last_valid_trade_date(data_dir: Path) -> str | None:
    value = _latest_manifest(data_dir).get("trade_date")
    return None if value is None else str(value)


def _safe_error(exc: Exception, client: IFindHTTPClient) -> str:
    message = f"{type(exc).__name__}: {exc}"
    for secret in (
        getattr(client, "refresh_token", None),
        getattr(client, "access_token", None),
    ):
        if secret:
            message = message.replace(str(secret), "[REDACTED]")
    return message[:500]


def _quality_thresholds(config: CollectionConfig) -> dict[str, Any]:
    return {
        "min_universe_size": config.min_universe_size,
        "min_quote_coverage": config.min_quote_coverage,
        "min_reference_coverage": config.min_reference_coverage,
        "min_extended_field_coverage": config.min_extended_field_coverage,
    }


def _pit_timing(
    trade_date: str, started_at: str, completed_at: str
) -> dict[str, str]:
    configured = data_cutoff_time_for_date(trade_date)
    completed_ts = pd.Timestamp(completed_at)
    if completed_ts.tzinfo is None:
        completed_ts = completed_ts.tz_localize("UTC")
    configured_ts = pd.Timestamp(configured).tz_convert("UTC")
    effective = min(completed_ts.tz_convert("UTC"), configured_ts)
    return {
        "collection_started_at": pd.Timestamp(started_at).isoformat(),
        "collection_completed_at": completed_ts.isoformat(),
        "configured_decision_cutoff": pd.Timestamp(configured).isoformat(),
        "effective_pit_cutoff": effective.isoformat(),
    }


def _previous_quality_context(
    data_dir: Path, trade_date: str
) -> dict[str, pd.DataFrame] | None:
    market_root = data_dir / "facts" / "market"
    dates = sorted(
        path.name.split("=", 1)[1]
        for path in market_root.glob("trade_date=*")
        if path.is_dir()
        and "=" in path.name
        and path.name.split("=", 1)[1] < trade_date
    )
    if not dates:
        return None
    previous_date = dates[-1]
    market_dir = market_root / f"trade_date={previous_date}"
    reference_dir = (
        data_dir / "facts" / "reference" / f"as_of_date={previous_date}"
    )
    paths = {
        "universe": reference_dir / "universe.parquet",
        "security_reference": reference_dir / "security_reference.parquet",
        "quotes": market_dir / "daily_quotes.parquet",
        "daily_status": market_dir / "daily_security_status.parquet",
    }
    if not all(path.exists() for path in paths.values()):
        return None
    return {name: pd.read_parquet(path) for name, path in paths.items()}


def collect_and_publish(
    client: IFindHTTPClient,
    trade_date: str,
    *,
    config: CollectionConfig = CollectionConfig(),
    progress: ProgressCallback | None = None,
) -> CollectionResult:
    started_at = str(
        getattr(client, "collection_started_at", None) or _utc_now()
    )
    previous_trade_date: str | None = None
    try:
        previous_trade_date = _last_valid_trade_date(config.data_dir)
        raw_calendar = client.fetch_trade_calendar(
            trade_date, offset=config.trade_calendar_offset
        )
        trading_calendar = normalize_trade_calendar(raw_calendar)
        open_dates = sorted(
            trading_calendar.loc[
                trading_calendar["is_open"].fillna(False).astype(bool), "trade_date"
            ]
            .dropna()
            .astype(str)
            .unique()
            .tolist()
        )
        if trade_date not in open_dates:
            status = {
                "schema_version": 2,
                "state": "market_closed",
                "data_fresh": False,
                "requested_trade_date": trade_date,
                "last_valid_trade_date": previous_trade_date,
                "provider": "ifind_http",
                "calendar": "SSE",
                "calendar_open_dates": open_dates,
                "started_at_utc": started_at,
                "completed_at_utc": _utc_now(),
                "raw_payload_persisted": False,
            }
            write_run_status(config.data_dir, status)
            return CollectionResult(True, status)

        raw_universe = client.fetch_universe(trade_date)
        universe = normalize_universe(raw_universe)
        universe_codes = universe["thscode"].astype(str).tolist()

        reference_progress = None
        quote_progress = None
        if progress:
            reference_progress = lambda done, total: progress(
                "security_reference", done, total
            )
            quote_progress = lambda done, total: progress("daily_quotes", done, total)

        raw_reference = client.fetch_security_reference(
            universe_codes,
            trade_date,
            batch_size=config.reference_batch_size,
            request_interval_seconds=config.request_interval_seconds,
            progress=reference_progress,
        )
        security_reference = normalize_security_reference(raw_reference, universe)
        raw_quotes = client.fetch_daily_quotes(
            universe_codes,
            trade_date,
            batch_size=config.quote_batch_size,
            request_interval_seconds=config.request_interval_seconds,
            progress=quote_progress,
        )
        quotes = normalize_quotes(raw_quotes, universe)
        daily_status = build_daily_security_status(universe, quotes, trade_date)
        quality = validate_data(
            universe,
            security_reference,
            quotes,
            trading_calendar,
            daily_status,
            trade_date,
            min_universe_size=config.min_universe_size,
            min_quote_coverage=config.min_quote_coverage,
            min_reference_coverage=config.min_reference_coverage,
            min_extended_field_coverage=config.min_extended_field_coverage,
            previous=_previous_quality_context(config.data_dir, trade_date),
        )
        if not quality.ok:
            status = {
                "schema_version": 2,
                "state": "failed_quality",
                "data_fresh": False,
                "requested_trade_date": trade_date,
                "last_valid_trade_date": previous_trade_date,
                "provider": "ifind_http",
                "started_at_utc": started_at,
                "completed_at_utc": _utc_now(),
                "raw_payload_persisted": False,
                "quality": quality.as_dict(),
            }
            write_run_status(config.data_dir, status)
            return CollectionResult(False, status)

        market_summary = build_market_summary(quotes, trade_date, daily_status)
        frames = {
            "universe": (universe, ["thscode"]),
            "security_reference": (security_reference, ["thscode"]),
            "quotes": (quotes, ["trade_date", "thscode"]),
            "trading_calendar": (trading_calendar, ["calendar", "trade_date"]),
            "daily_status": (daily_status, ["trade_date", "thscode"]),
        }
        frame_hashes = {
            f"{name}_frame_sha256": frame_hash(frame, sort_columns)
            for name, (frame, sort_columns) in frames.items()
        }
        latest_manifest = _latest_manifest(config.data_dir)
        is_current_latest = latest_manifest.get("trade_date") == trade_date
        existing_manifest = (
            latest_manifest
            if is_current_latest
            else load_manifest(
                config.data_dir / "snapshots" / trade_date / "manifest.json"
            )
        )
        existing_provenance = existing_manifest.get("provenance") or {}
        unchanged = existing_manifest.get("trade_date") == trade_date and all(
            existing_provenance.get(key) == value
            for key, value in frame_hashes.items()
        )
        existing_drift = (
            ((existing_manifest.get("quality") or {}).get("metrics") or {}).get(
                "drift"
            )
        )
        metadata_upgrade_needed = (
            int(existing_manifest.get("schema_version") or 0) != 3
            or not isinstance(existing_drift, dict)
        )
        if unchanged and not metadata_upgrade_needed:
            data_reference: dict[str, Any] = (
                existing_manifest.get("data_reference") or {}
            )
            data_reference_error: str | None = None
            data_reference_contract_error: str | None = None
            derived_missing = any(
                not (config.data_dir / "latest" / name).exists()
                for name in (
                    "data_reference_latest.json",
                    "opportunity_inputs_latest.json",
                    "opportunity_radar_latest.json",
                )
            )
            if config.build_data_reference and is_current_latest and derived_missing:
                try:
                    data_reference = build_data_reference_outputs(
                        config.data_dir, trade_date
                    )
                except ArtifactContractError as exc:
                    data_reference_contract_error = _safe_error(exc, client)
                except Exception as exc:
                    data_reference_error = _safe_error(exc, client)
            unchanged_state = (
                "success_unchanged"
                if is_current_latest
                else "success_historical_unchanged"
            )
            status = {
                "schema_version": 2,
                "state": (
                    "failed_derived_contract"
                    if data_reference_contract_error
                    else "success_partial"
                    if data_reference_error
                    else unchanged_state
                ),
                "data_fresh": not bool(
                    data_reference_error or data_reference_contract_error
                ),
                "requested_trade_date": trade_date,
                "last_valid_trade_date": (
                    trade_date if is_current_latest else previous_trade_date
                ),
                "provider": "ifind_http",
                "started_at_utc": started_at,
                "completed_at_utc": _utc_now(),
                "raw_payload_persisted": False,
                "quality": quality.as_dict(),
                "artifacts": existing_manifest.get("artifacts") or {},
                "data_reference": data_reference,
            }
            if data_reference_error:
                status["data_reference_error"] = data_reference_error
            if data_reference_contract_error:
                status["data_reference_error"] = data_reference_contract_error
            previous_status = _last_run_status(config.data_dir)
            if (
                data_reference_error
                or data_reference_contract_error
                or str(previous_status.get("state") or "")
                not in {"success", "success_unchanged"}
                or str(previous_status.get("requested_trade_date") or "") != trade_date
            ):
                write_run_status(config.data_dir, status)
            return CollectionResult(not bool(data_reference_contract_error), status)

        completed_at = str(
            getattr(client, "collection_completed_at", None) or _utc_now()
        )
        pit_timing = _pit_timing(trade_date, started_at, completed_at)
        manifest: dict[str, Any] = {
            "schema_version": 3,
            "trade_date": trade_date,
            "requested_trade_date": trade_date,
            "source_trade_date": trade_date,
            "collected_at_utc": completed_at,
            **pit_timing,
            "provider": "ifind_http",
            "source_endpoints": [
                "get_trade_dates",
                "data_pool",
                "basic_data_service",
                "cmd_history_quotation",
            ],
            "verified": True,
            "data_fresh": True,
            "raw_payload_persisted": False,
            "quality": quality.as_dict(),
            "quality_thresholds": _quality_thresholds(config),
            "provenance": {
                "universe_report": "p03291",
                "universe_block": "001005010",
                "calendar": "SSE",
                "calendar_market_code": "212001",
                **frame_hashes,
                **{
                    f"{name}_schema_signature": schema_signature(frame)
                    for name, (frame, _) in frames.items()
                },
            },
        }
        promote_latest = (
            previous_trade_date is None or trade_date >= previous_trade_date
        )
        final_manifest = promote_snapshot(
            config.data_dir,
            trade_date,
            universe,
            security_reference,
            quotes,
            trading_calendar,
            daily_status,
            market_summary,
            manifest,
            history_limit=config.history_limit,
            snapshot_limit=config.snapshot_limit,
            promote_latest=promote_latest and not config.build_data_reference,
        )
        data_reference: dict[str, Any] = {}
        data_reference_error: str | None = None
        data_reference_contract_error: str | None = None
        if promote_latest and config.build_data_reference:
            try:
                data_reference = build_data_reference_outputs(
                    config.data_dir, trade_date
                )
                final_manifest = promote_verified_snapshot_to_latest(
                    config.data_dir, trade_date
                )
            except ArtifactContractError as exc:
                data_reference_contract_error = _safe_error(exc, client)
            except Exception as exc:
                data_reference_error = _safe_error(exc, client)
        elif not promote_latest:
            data_reference = {
                "state": "deferred_for_historical_backfill",
                "reason": "historical partition persisted without regressing latest",
            }
        else:
            data_reference = {
                "state": "deferred_until_backfill_tail",
                "reason": (
                    "intermediate partition persisted; data reference builds at range end"
                ),
            }
        if not promote_latest:
            result_state = "success_historical"
        elif data_reference_contract_error:
            result_state = "failed_derived_contract"
        elif data_reference_error:
            result_state = "success_partial"
        else:
            result_state = "success"
        status = {
            "schema_version": 2,
            "state": result_state,
            "data_fresh": not bool(
                data_reference_error or data_reference_contract_error
            ),
            "requested_trade_date": trade_date,
            "last_valid_trade_date": (
                trade_date
                if promote_latest
                and not data_reference_error
                and not data_reference_contract_error
                else previous_trade_date
            ),
            "previous_valid_trade_date": previous_trade_date,
            "provider": "ifind_http",
            "started_at_utc": started_at,
            "completed_at_utc": _utc_now(),
            "raw_payload_persisted": False,
            "quality": quality.as_dict(),
            "artifacts": final_manifest["artifacts"],
            "data_reference": data_reference,
        }
        if data_reference_error:
            status["data_reference_error"] = data_reference_error
        if data_reference_contract_error:
            status["data_reference_error"] = data_reference_contract_error
        write_run_status(config.data_dir, status)
        return CollectionResult(not bool(data_reference_contract_error), status)
    except Exception as exc:
        status = {
            "schema_version": 2,
            "state": "failed_collection",
            "data_fresh": False,
            "requested_trade_date": trade_date,
            "last_valid_trade_date": previous_trade_date,
            "provider": "ifind_http",
            "started_at_utc": started_at,
            "completed_at_utc": _utc_now(),
            "raw_payload_persisted": False,
            "error": _safe_error(exc, client),
        }
        write_run_status(config.data_dir, status)
        return CollectionResult(False, status)


def validate_latest(
    *,
    data_dir: Path = Path("data"),
    policy_min_universe_size: int = 5000,
    policy_min_quote_coverage: float = 0.98,
    policy_min_reference_coverage: float = 0.98,
    policy_min_extended_field_coverage: float = 0.95,
) -> tuple[bool, dict[str, Any]]:
    try:
        manifest, artifact_errors = verify_latest_artifacts(data_dir)
    except Exception as exc:
        return False, {
            "ok": False,
            "errors": [f"{type(exc).__name__}: {str(exc)[:400]}"],
        }
    if not manifest:
        return False, {"ok": False, "errors": artifact_errors}
    errors = list(artifact_errors)
    try:
        latest = data_dir / "latest"
        universe = pd.read_parquet(latest / "universe.parquet")
        security_reference = pd.read_parquet(latest / "security_reference.parquet")
        quotes = pd.read_parquet(latest / "daily_quotes.parquet")
        trading_calendar = pd.read_parquet(latest / "trading_calendar.parquet")
        daily_status = pd.read_parquet(latest / "daily_security_status.parquet")
        trade_date = str(manifest.get("trade_date") or "")
        thresholds = manifest.get("quality_thresholds") or {}
        quality = validate_data(
            universe,
            security_reference,
            quotes,
            trading_calendar,
            daily_status,
            trade_date,
            min_universe_size=max(
                policy_min_universe_size,
                int(thresholds.get("min_universe_size", policy_min_universe_size)),
            ),
            min_quote_coverage=max(
                policy_min_quote_coverage,
                float(thresholds.get("min_quote_coverage", policy_min_quote_coverage)),
            ),
            min_reference_coverage=max(
                policy_min_reference_coverage,
                float(
                    thresholds.get(
                        "min_reference_coverage", policy_min_reference_coverage
                    )
                ),
            ),
            min_extended_field_coverage=max(
                policy_min_extended_field_coverage,
                float(
                    thresholds.get(
                        "min_extended_field_coverage",
                        policy_min_extended_field_coverage,
                    )
                ),
            ),
            previous=_previous_quality_context(data_dir, trade_date),
        )
        errors.extend(quality.errors)
        if int(manifest.get("schema_version") or 0) not in {2, 3}:
            errors.append("manifest schema_version is not supported")
        if manifest.get("verified") is not True:
            errors.append("manifest is not marked verified")
        if manifest.get("data_fresh") is not True:
            errors.append("manifest is not marked data_fresh")

        frames = {
            "universe": (universe, ["thscode"]),
            "security_reference": (security_reference, ["thscode"]),
            "quotes": (quotes, ["trade_date", "thscode"]),
            "trading_calendar": (trading_calendar, ["calendar", "trade_date"]),
            "daily_status": (daily_status, ["trade_date", "thscode"]),
        }
        provenance = manifest.get("provenance") or {}
        for name, (frame, sort_columns) in frames.items():
            if frame_hash(frame, sort_columns) != provenance.get(
                f"{name}_frame_sha256"
            ):
                errors.append(f"normalized {name} frame hash mismatch")
            if schema_signature(frame) != provenance.get(
                f"{name}_schema_signature"
            ):
                errors.append(f"normalized {name} schema signature mismatch")
        payload = {
            "ok": not errors,
            "trade_date": trade_date,
            "quality": quality.as_dict(),
            "errors": errors,
        }
        return not errors, payload
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {str(exc)[:400]}")
        return False, {"ok": False, "errors": errors}


__all__ = [
    "CollectionConfig",
    "CollectionResult",
    "collect_and_publish",
    "validate_latest",
]
