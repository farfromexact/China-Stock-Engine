"""Atomic artifact storage and bounded Git-friendly history retention."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any

import pandas as pd


SNAPSHOT_DIR_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    text = json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    )
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.write("\n")
    os.replace(temporary, path)


def atomic_write_parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    frame.to_parquet(temporary, index=False, compression="zstd")
    os.replace(temporary, path)


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, target)


def write_run_status(data_dir: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(data_dir / "last_run_status.json", payload)
    latest_dir = data_dir / "latest"
    if (latest_dir / "manifest.json").exists():
        atomic_write_json(latest_dir / "last_attempt_status.json", payload)


def json_sha256(payload: Any) -> str:
    """Hash a JSON-compatible payload using the writer's canonical ordering."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_fact_partitions(
    data_dir: Path,
    trade_date: str,
    universe: pd.DataFrame,
    security_reference: pd.DataFrame,
    quotes: pd.DataFrame,
    trading_calendar: pd.DataFrame,
    daily_status: pd.DataFrame,
) -> None:
    """Materialize append-only facts that survive snapshot pruning."""

    market_dir = data_dir / "facts" / "market" / f"trade_date={trade_date}"
    reference_dir = data_dir / "facts" / "reference" / f"as_of_date={trade_date}"
    atomic_write_parquet(market_dir / "daily_quotes.parquet", quotes)
    atomic_write_parquet(market_dir / "daily_security_status.parquet", daily_status)
    atomic_write_parquet(reference_dir / "universe.parquet", universe)
    atomic_write_parquet(
        reference_dir / "security_reference.parquet", security_reference
    )
    atomic_write_parquet(reference_dir / "trading_calendar.parquet", trading_calendar)


def _update_market_history(
    data_dir: Path, summary: dict[str, Any], history_limit: int
) -> None:
    history_path = data_dir / "market_history.json"
    history: list[dict[str, Any]] = []
    if history_path.exists():
        loaded = json.loads(history_path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            history = [item for item in loaded if isinstance(item, dict)]
    by_date = {
        str(item.get("trade_date")): item for item in history if item.get("trade_date")
    }
    by_date[str(summary["trade_date"])] = summary
    retained = [by_date[key] for key in sorted(by_date)[-history_limit:]]
    atomic_write_json(history_path, retained)


def _prune_snapshots(data_dir: Path, snapshot_limit: int) -> None:
    snapshots_dir = data_dir / "snapshots"
    if not snapshots_dir.exists():
        return
    snapshots = sorted(
        path
        for path in snapshots_dir.iterdir()
        if path.is_dir() and SNAPSHOT_DIR_PATTERN.fullmatch(path.name)
    )
    for path in snapshots[:-snapshot_limit]:
        resolved = path.resolve()
        if resolved.parent != snapshots_dir.resolve():
            raise RuntimeError(f"refusing to prune unexpected snapshot path: {resolved}")
        datetime.strptime(path.name, "%Y-%m-%d")
        shutil.rmtree(path)


def promote_snapshot(
    data_dir: Path,
    trade_date: str,
    universe: pd.DataFrame,
    security_reference: pd.DataFrame,
    quotes: pd.DataFrame,
    trading_calendar: pd.DataFrame,
    daily_status: pd.DataFrame,
    market_summary: dict[str, Any],
    manifest: dict[str, Any],
    *,
    history_limit: int = 252,
    snapshot_limit: int = 60,
    promote_latest: bool = True,
) -> dict[str, Any]:
    snapshot_dir = data_dir / "snapshots" / trade_date
    latest_dir = data_dir / "latest"
    frames = {
        "universe.parquet": universe,
        "security_reference.parquet": security_reference,
        "daily_quotes.parquet": quotes,
        "trading_calendar.parquet": trading_calendar,
        "daily_security_status.parquet": daily_status,
    }
    summary_path = snapshot_dir / "market_summary.json"

    for name, frame in frames.items():
        atomic_write_parquet(snapshot_dir / name, frame)
    atomic_write_json(summary_path, market_summary)

    final_manifest = dict(manifest)
    final_manifest["artifacts"] = {
        name: {
            "rows": int(len(frame)),
            "sha256": sha256_file(snapshot_dir / name),
        }
        for name, frame in frames.items()
    }
    final_manifest["artifacts"]["market_summary.json"] = {
        "sha256": sha256_file(summary_path),
    }
    atomic_write_json(snapshot_dir / "manifest.json", final_manifest)

    _write_fact_partitions(
        data_dir,
        trade_date,
        universe,
        security_reference,
        quotes,
        trading_calendar,
        daily_status,
    )

    if promote_latest:
        for stale_name in (
            "stock_state.parquet",
            "market_dashboard.html",
            "data_reference_latest.json",
            "data_reference.html",
        ):
            stale_path = latest_dir / stale_name
            if stale_path.exists() and stale_path.is_file():
                stale_path.unlink()
        for name in frames:
            atomic_copy(snapshot_dir / name, latest_dir / name)
        atomic_copy(summary_path, latest_dir / "market_summary.json")
        atomic_write_json(latest_dir / "manifest.json", final_manifest)

    _update_market_history(data_dir, market_summary, history_limit)
    _prune_snapshots(data_dir, snapshot_limit)
    return final_manifest


def _remove_file(path: Path) -> None:
    if path.exists() and path.is_file():
        path.unlink()


def publish_data_reference_artifacts(
    data_dir: Path,
    trade_date: str,
    stock_state: pd.DataFrame,
    data_reference: dict[str, Any],
    reference_metadata: dict[str, Any],
    *,
    publish_latest: bool = True,
) -> dict[str, Any]:
    """Atomically attach deterministic, opinion-free reference data."""

    snapshot_dir = data_dir / "snapshots" / trade_date
    latest_dir = data_dir / "latest"
    manifest_path = snapshot_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"snapshot manifest does not exist: {manifest_path}")

    artifacts = {"stock_state.parquet": stock_state}
    for name, frame in artifacts.items():
        atomic_write_parquet(snapshot_dir / name, frame)
    for item in data_reference.get("data_catalog") or []:
        if isinstance(item, dict) and item.get("name") in artifacts:
            item["sha256"] = sha256_file(snapshot_dir / str(item["name"]))
    atomic_write_json(
        snapshot_dir / "data_reference_latest.json", data_reference
    )

    atomic_write_parquet(
        data_dir
        / "features"
        / "stock_state"
        / f"trade_date={trade_date}"
        / "stock_state.parquet",
        stock_state,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_artifacts = dict(manifest.get("artifacts") or {})
    for name, frame in artifacts.items():
        manifest_artifacts[name] = {
            "rows": int(len(frame)),
            "sha256": sha256_file(snapshot_dir / name),
        }
    manifest_artifacts["data_reference_latest.json"] = {
        "sha256": sha256_file(snapshot_dir / "data_reference_latest.json")
    }
    manifest["artifacts"] = manifest_artifacts
    manifest["data_reference"] = reference_metadata
    atomic_write_json(manifest_path, manifest)

    if publish_latest:
        for stale_name in ("market_dashboard.html",):
            _remove_file(latest_dir / stale_name)
        for name in artifacts:
            atomic_copy(snapshot_dir / name, latest_dir / name)
        atomic_copy(
            snapshot_dir / "data_reference_latest.json",
            latest_dir / "data_reference_latest.json",
        )
        atomic_write_json(latest_dir / "manifest.json", manifest)
    return manifest


def verify_latest_artifacts(data_dir: Path) -> tuple[dict[str, Any], list[str]]:
    latest_dir = data_dir / "latest"
    manifest_path = latest_dir / "manifest.json"
    if not manifest_path.exists():
        return {}, ["latest manifest does not exist"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    artifacts = manifest.get("artifacts") or {}
    for name, metadata in artifacts.items():
        path = latest_dir / str(name)
        if not path.exists():
            errors.append(f"missing latest artifact: {name}")
            continue
        expected = str((metadata or {}).get("sha256") or "")
        actual = sha256_file(path)
        if not expected or actual != expected:
            errors.append(f"sha256 mismatch for latest artifact: {name}")
    return manifest, errors


__all__ = [
    "atomic_copy",
    "atomic_write_parquet",
    "atomic_write_json",
    "json_sha256",
    "promote_snapshot",
    "publish_data_reference_artifacts",
    "sha256_file",
    "verify_latest_artifacts",
    "write_run_status",
]
