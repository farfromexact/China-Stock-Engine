"""Daily A-share collection, quality-gated promotion, and validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .ifind_http import IFindHTTPClient
from .quality import (
    build_market_summary,
    frame_hash,
    normalize_quotes,
    normalize_universe,
    schema_signature,
    validate_data,
)
from .storage import promote_snapshot, verify_latest_artifacts, write_run_status


@dataclass(frozen=True)
class CollectionConfig:
    data_dir: Path = Path("data")
    min_universe_size: int = 5000
    min_quote_coverage: float = 0.98
    quote_batch_size: int = 300
    request_interval_seconds: float = 0.15
    history_limit: int = 252
    snapshot_limit: int = 60


@dataclass(frozen=True)
class CollectionResult:
    ok: bool
    status: dict[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _latest_manifest(data_dir: Path) -> dict[str, Any]:
    manifest_path = data_dir / "latest" / "manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _last_run_status(data_dir: Path) -> dict[str, Any]:
    status_path = data_dir / "last_run_status.json"
    if not status_path.exists():
        return {}
    try:
        loaded = json.loads(status_path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _last_valid_trade_date(data_dir: Path) -> str | None:
    value = _latest_manifest(data_dir).get("trade_date")
    return None if value is None else str(value)


def _safe_error(exc: Exception, client: IFindHTTPClient) -> str:
    message = f"{type(exc).__name__}: {exc}"
    for secret in (client.refresh_token, client.access_token):
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message[:500]


def collect_and_publish(
    client: IFindHTTPClient,
    trade_date: str,
    *,
    config: CollectionConfig = CollectionConfig(),
    progress: Any = None,
) -> CollectionResult:
    started_at = _utc_now()
    previous_trade_date = _last_valid_trade_date(config.data_dir)
    try:
        raw_universe = client.fetch_universe(trade_date)
        universe = normalize_universe(raw_universe)
        raw_quotes = client.fetch_daily_quotes(
            universe["thscode"].astype(str).tolist(),
            trade_date,
            batch_size=config.quote_batch_size,
            request_interval_seconds=config.request_interval_seconds,
            progress=progress,
        )
        quotes = normalize_quotes(raw_quotes, universe)
        quality = validate_data(
            universe,
            quotes,
            trade_date,
            min_universe_size=config.min_universe_size,
            min_quote_coverage=config.min_quote_coverage,
        )
        if not quality.ok:
            status = {
                "schema_version": 1,
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

        market_summary = build_market_summary(quotes, trade_date)
        universe_frame_hash = frame_hash(universe, ["thscode"])
        quotes_frame_hash = frame_hash(quotes, ["trade_date", "thscode"])
        existing_manifest = _latest_manifest(config.data_dir)
        existing_provenance = existing_manifest.get("provenance") or {}
        if (
            existing_manifest.get("trade_date") == trade_date
            and existing_provenance.get("universe_frame_sha256")
            == universe_frame_hash
            and existing_provenance.get("quotes_frame_sha256") == quotes_frame_hash
        ):
            status = {
                "schema_version": 1,
                "state": "success_unchanged",
                "data_fresh": True,
                "requested_trade_date": trade_date,
                "last_valid_trade_date": trade_date,
                "provider": "ifind_http",
                "started_at_utc": started_at,
                "completed_at_utc": _utc_now(),
                "raw_payload_persisted": False,
                "quality": quality.as_dict(),
                "artifacts": existing_manifest.get("artifacts") or {},
            }
            previous_status = _last_run_status(config.data_dir)
            previous_state = str(previous_status.get("state") or "")
            previous_requested_date = str(
                previous_status.get("requested_trade_date") or ""
            )
            if (
                previous_state not in {"success", "success_unchanged"}
                or previous_requested_date != trade_date
            ):
                write_run_status(config.data_dir, status)
            return CollectionResult(True, status)

        collected_at = _utc_now()
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "trade_date": trade_date,
            "requested_trade_date": trade_date,
            "source_trade_date": trade_date,
            "collected_at_utc": collected_at,
            "provider": "ifind_http",
            "source_endpoints": ["data_pool", "cmd_history_quotation"],
            "verified": True,
            "data_fresh": True,
            "raw_payload_persisted": False,
            "quality": quality.as_dict(),
            "quality_thresholds": {
                "min_universe_size": config.min_universe_size,
                "min_quote_coverage": config.min_quote_coverage,
            },
            "provenance": {
                "universe_report": "p03291",
                "universe_block": "001005010",
                "universe_frame_sha256": universe_frame_hash,
                "quotes_frame_sha256": quotes_frame_hash,
                "universe_schema_signature": schema_signature(universe),
                "quotes_schema_signature": schema_signature(quotes),
            },
        }
        final_manifest = promote_snapshot(
            config.data_dir,
            trade_date,
            universe,
            quotes,
            market_summary,
            manifest,
            history_limit=config.history_limit,
            snapshot_limit=config.snapshot_limit,
        )
        status = {
            "schema_version": 1,
            "state": "success",
            "data_fresh": True,
            "requested_trade_date": trade_date,
            "last_valid_trade_date": trade_date,
            "previous_valid_trade_date": previous_trade_date,
            "provider": "ifind_http",
            "started_at_utc": started_at,
            "completed_at_utc": _utc_now(),
            "raw_payload_persisted": False,
            "quality": quality.as_dict(),
            "artifacts": final_manifest["artifacts"],
        }
        write_run_status(config.data_dir, status)
        return CollectionResult(True, status)
    except Exception as exc:
        status = {
            "schema_version": 1,
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
) -> tuple[bool, dict[str, Any]]:
    manifest, artifact_errors = verify_latest_artifacts(data_dir)
    if not manifest:
        return False, {"ok": False, "errors": artifact_errors}
    errors = list(artifact_errors)
    try:
        universe = pd.read_parquet(data_dir / "latest" / "universe.parquet")
        quotes = pd.read_parquet(data_dir / "latest" / "daily_quotes.parquet")
        trade_date = str(manifest.get("trade_date") or "")
        thresholds = manifest.get("quality_thresholds") or {}
        quality = validate_data(
            universe,
            quotes,
            trade_date,
            min_universe_size=max(
                policy_min_universe_size,
                int(thresholds.get("min_universe_size", policy_min_universe_size)),
            ),
            min_quote_coverage=max(
                policy_min_quote_coverage,
                float(
                    thresholds.get(
                        "min_quote_coverage", policy_min_quote_coverage
                    )
                ),
            ),
        )
        errors.extend(quality.errors)
        if manifest.get("verified") is not True:
            errors.append("manifest is not marked verified")
        if manifest.get("data_fresh") is not True:
            errors.append("manifest is not marked data_fresh")
        expected_universe_hash = (
            (manifest.get("provenance") or {}).get("universe_frame_sha256") or ""
        )
        expected_quotes_hash = (
            (manifest.get("provenance") or {}).get("quotes_frame_sha256") or ""
        )
        if frame_hash(universe, ["thscode"]) != expected_universe_hash:
            errors.append("normalized universe frame hash mismatch")
        if frame_hash(quotes, ["trade_date", "thscode"]) != expected_quotes_hash:
            errors.append("normalized quotes frame hash mismatch")
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
