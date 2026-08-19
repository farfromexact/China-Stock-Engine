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
    candidates = sorted(
        path
        for path in snapshots_dir.iterdir()
        if path.is_dir() and SNAPSHOT_DIR_PATTERN.fullmatch(path.name)
    )
    for path in candidates[:-snapshot_limit]:
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

    for name in frames:
        atomic_copy(snapshot_dir / name, latest_dir / name)
    atomic_copy(summary_path, latest_dir / "market_summary.json")
    atomic_write_json(latest_dir / "manifest.json", final_manifest)

    _update_market_history(data_dir, market_summary, history_limit)
    _prune_snapshots(data_dir, snapshot_limit)
    return final_manifest


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
    "atomic_write_json",
    "promote_snapshot",
    "sha256_file",
    "verify_latest_artifacts",
    "write_run_status",
]
