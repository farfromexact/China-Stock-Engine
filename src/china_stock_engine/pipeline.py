"""Daily A-share collection, quality-gated promotion, and validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
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
from .data_reference import build_data_reference_outputs
from .storage import promote_snapshot, verify_latest_artifacts, write_run_status


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
    history_limit: int = 252
    snapshot_limit: int = 60
    build_data_reference: bool = True


@dataclass(frozen=True)
class CollectionResult:
    ok: bool
    status: dict[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _latest_manifest(data_dir: Path) -> dict[str, Any]:
    return _load_json(data_dir / "latest" / "manifest.json")


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


def collect_and_publish(
    client: IFindHTTPClient,
    trade_date: str,
    *,
    config: CollectionConfig = CollectionConfig(),
    progress: ProgressCallback | None = None,
) -> CollectionResult:
    started_at = _utc_now()
    previous_trade_date = _last_valid_trade_date(config.data_dir)
    try:
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
            else _load_json(
                config.data_dir / "snapshots" / trade_date / "manifest.json"
            )
        )
        existing_provenance = existing_manifest.get("provenance") or {}
        unchanged = existing_manifest.get("trade_date") == trade_date and all(
            existing_provenance.get(key) == value
            for key, value in frame_hashes.items()
        )
        if unchanged:
            data_reference: dict[str, Any] = (
                existing_manifest.get("data_reference") or {}
            )
            data_reference_error: str | None = None
            if config.build_data_reference and is_current_latest and not (
                config.data_dir / "latest" / "data_reference_latest.json"
            ).exists():
                try:
                    data_reference = build_data_reference_outputs(
                        config.data_dir, trade_date
                    )
                except Exception as exc:
                    data_reference_error = _safe_error(exc, client)
            unchanged_state = (
                "success_unchanged"
                if is_current_latest
                else "success_historical_unchanged"
            )
            status = {
                "schema_version": 2,
                "state": "success_partial"
                if data_reference_error
                else unchanged_state,
                "data_fresh": True,
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
            previous_status = _last_run_status(config.data_dir)
            if (
                str(previous_status.get("state") or "")
                not in {"success", "success_unchanged"}
                or str(previous_status.get("requested_trade_date") or "") != trade_date
            ):
                write_run_status(config.data_dir, status)
            return CollectionResult(True, status)

        manifest: dict[str, Any] = {
            "schema_version": 2,
            "trade_date": trade_date,
            "requested_trade_date": trade_date,
            "source_trade_date": trade_date,
            "collected_at_utc": _utc_now(),
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
            promote_latest=promote_latest,
        )
        data_reference: dict[str, Any] = {}
        data_reference_error: str | None = None
        if promote_latest and config.build_data_reference:
            try:
                data_reference = build_data_reference_outputs(
                    config.data_dir, trade_date
                )
                final_manifest = _latest_manifest(config.data_dir)
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
        elif data_reference_error:
            result_state = "success_partial"
        else:
            result_state = "success"
        status = {
            "schema_version": 2,
            "state": result_state,
            "data_fresh": True,
            "requested_trade_date": trade_date,
            "last_valid_trade_date": (
                trade_date if promote_latest else previous_trade_date
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
        write_run_status(config.data_dir, status)
        return CollectionResult(True, status)
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
    manifest, artifact_errors = verify_latest_artifacts(data_dir)
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
        )
        errors.extend(quality.errors)
        if int(manifest.get("schema_version") or 0) < 2:
            errors.append("manifest schema_version is below 2")
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
