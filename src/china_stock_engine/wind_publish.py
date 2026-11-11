"""Verify a complete Wind candidate, archive by source, then promote latest."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .data_reference import build_data_reference_outputs, load_market_history
from .pipeline import validate_latest
from .retention import tree_bytes
from .storage import (ArtifactContractError, atomic_write_json, json_sha256,
                      load_json_object, load_manifest, sha256_file, verify_latest_artifacts)
from .wind_session import collector_lock


def inventory(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ArtifactContractError("Wind publication refuses symlink artifacts")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = sha256_file(path)
    return result


def verify_candidate(candidate: Path) -> dict:
    status = load_json_object(candidate / "wind_candidate.json", missing_ok=False)
    if (status.get("state") != "candidate_quality_verified" or status.get("sessions") != 300
            or status.get("canary_only") is not False or status.get("units_verified") is not True
            or status.get("report_built") is not True or status.get("securities", 0) < 5000):
        raise ArtifactContractError("Wind publication requires a reviewed full-market 300-session candidate")
    manifest, errors = verify_latest_artifacts(candidate)
    if errors or manifest.get("provider") != "windpy" or manifest.get("trade_date") != status.get("trade_date"):
        raise ArtifactContractError("Wind candidate source/artifact contract failed")
    valid, details = validate_latest(data_dir=candidate)
    if not valid:
        raise ArtifactContractError("Wind candidate market quality failed")
    history = load_market_history(candidate, status["trade_date"], research_asof=status["completed_at"])
    if history.trade_date.nunique() != 300 or not history.history_verified.all():
        raise ArtifactContractError("Wind candidate does not contain 300 verified history sessions")
    return status


def rolling_vintages(data_dir: Path, keep: Path) -> list[Path]:
    """Only local-daily managed archives rotate; migration/legacy facts stay."""
    retired = []
    for directory in sorted((data_dir / "facts/wind").glob("vintage=*")):
        if directory == data_dir / keep:
            continue
        if not re.fullmatch(r"vintage=[0-9a-f]{24}", directory.name):
            continue
        receipt = load_json_object(directory / "candidate_receipt.json")
        if receipt.get("retention_class") == "local_daily_rolling":
            retired.append(directory.relative_to(data_dir))
    return retired


def publish(candidate: Path, data_dir: Path = Path("data"), *, profile: Path | None = None,
            rolling: bool = False) -> dict:
    candidate, data_dir = candidate.resolve(), data_dir.resolve()
    if candidate == data_dir or data_dir in candidate.parents or candidate in data_dir.parents:
        raise ValueError("Wind candidate must be separate from production data")
    status = verify_candidate(candidate)
    candidate_manifest = load_manifest(candidate / "latest/manifest.json")
    source_profile = (candidate_manifest.get("provenance") or {}).get("profile")
    if profile is not None:
        from .wind_collect import profile_from
        source_profile = asdict(profile_from(profile))
    if not source_profile or json_sha256(source_profile) != status["profile_hash"]:
        raise ArtifactContractError("Wind source profile is absent or differs from the collected candidate")
    source_inventory = inventory(candidate)
    vintage = json_sha256({"profile": status["profile_hash"], "files": source_inventory})[:24]
    relative = Path("facts/wind") / f"vintage={vintage}"
    previous = load_manifest(data_dir / "latest/manifest.json")
    if str(previous.get("trade_date", "")) > status["trade_date"]:
        raise ArtifactContractError("Wind publication would regress latest trade date")
    previous_receipt = load_json_object(data_dir / "wind_publication_status.json")
    if previous_receipt.get("vintage") == vintage:
        if previous.get("provider") != "windpy" or previous.get("wind_vintage") != vintage:
            raise ArtifactContractError("Latest no longer identifies this Wind vintage; create a fresh candidate")
        _, errors = verify_latest_artifacts(data_dir)
        if errors:
            raise ArtifactContractError("Previously published Wind artifacts were modified")
        return previous_receipt

    data_dir.parent.mkdir(parents=True, exist_ok=True)
    with collector_lock(data_dir.parent / ".private-stock-data/wind-publication.lock"):
        # This snapshot also prevents promoting over an unrelated local update.
        before = inventory(data_dir) if data_dir.exists() else {}
        with tempfile.TemporaryDirectory(prefix=".wind-publication-", dir=data_dir.parent) as temporary:
            staging = Path(temporary) / "data"
            if data_dir.exists():
                shutil.copytree(data_dir, staging)
            else:
                staging.mkdir()
            destination = staging / relative
            if destination.exists():
                raise ArtifactContractError("Wind vintage already exists without a publication receipt")
            destination.mkdir(parents=True)
            shutil.copytree(candidate / "facts", destination, dirs_exist_ok=True)
            for name in ("wind_coverage.json",):
                shutil.copy2(candidate / name, destination / name)
            archived_status = {key: value for key, value in status.items() if key != "candidate"}
            if rolling:
                archived_status["retention_class"] = "local_daily_rolling"
            atomic_write_json(destination / "candidate_receipt.json", archived_status)
            retired = rolling_vintages(staging, relative) if rolling else []
            for directory in retired:
                shutil.rmtree(staging / directory)
            day = status["trade_date"]
            snapshot_relative = relative / "snapshots" / f"trade_date={day}"
            snapshot = staging / snapshot_relative
            snapshot.mkdir(parents=True)
            core_names = {"universe.parquet", "security_reference.parquet", "daily_quotes.parquet",
                          "daily_security_status.parquet", "trading_calendar.parquet", "market_summary.json"}
            for name in core_names:
                shutil.copy2(candidate / "latest" / name, snapshot / name)
            source_manifest = load_manifest(candidate / "latest/manifest.json")
            source_manifest["artifacts"] = {name: value for name, value in source_manifest["artifacts"].items() if name in core_names}
            for name in ("data_reference", "feature_input_sha256", "feature_pit_timing", "research_context"):
                source_manifest.pop(name, None)
            source_manifest["source_snapshot_path"] = snapshot_relative.as_posix()
            source_manifest["wind_vintage"] = vintage
            source_manifest["provenance"] = {**source_manifest.get("provenance", {}), "profile": source_profile}
            atomic_write_json(staging / snapshot_relative / "manifest.json", source_manifest)
            # This is a private staging root. Nothing becomes live until both
            # report generation and all quality/hash gates pass.
            shutil.copytree(candidate / "latest", staging / "latest", dirs_exist_ok=True)
            atomic_write_json(staging / "latest/manifest.json", source_manifest)
            observed = datetime.now(timezone.utc).isoformat()
            build_data_reference_outputs(staging, day, research_asof=observed)
            valid, details = validate_latest(data_dir=staging)
            if not valid:
                raise ArtifactContractError("Wind staged publication failed final quality/hash validation")
            history = load_market_history(staging, day, research_asof=observed)
            if history.trade_date.nunique() != 300:
                raise ArtifactContractError("Wind staged history does not have 300 sessions")
            # Existing facts, daily snapshots and historical features are immutable.
            for name, digest in before.items():
                if name.startswith(("facts/", "snapshots/", "features/")):
                    if any(Path(name).is_relative_to(directory) for directory in retired):
                        continue
                    if sha256_file(staging / name) != digest:
                        raise ArtifactContractError(f"Wind would alter existing historical artifact: {name}")
            if inventory(candidate) != source_inventory or (data_dir.exists() and inventory(data_dir) != before):
                raise ArtifactContractError("Candidate or production changed during Wind publication")
            receipt = {"schema_version": 1, "state": "published", "provider": "windpy",
                "trade_date": day, "start": status["start"], "sessions": 300,
                "securities": status["securities"], "rows": status["rows"], "vintage": vintage,
                "source_snapshot_path": snapshot_relative.as_posix(), "research_asof": observed,
                "protected_historical_files": sum(name.startswith(("facts/", "snapshots/", "features/"))
                    and not any(Path(name).is_relative_to(directory) for directory in retired) for name in before),
                "original_history_preserved": True,
                "retention_class": "local_daily_rolling" if rolling else "immutable_migration",
                "retired_local_daily_vintages": [path.as_posix() for path in retired],
                "completed_at": datetime.now(timezone.utc).isoformat()}
            atomic_write_json(staging / "wind_publication_status.json", receipt)
            atomic_write_json(staging / "last_run_status.json", {
                "schema_version": 2, "state": "success_wind_publication", "data_fresh": True,
                "provider": "windpy", "requested_trade_date": day, "last_valid_trade_date": day,
                "raw_payload_persisted": False, "completed_at_utc": receipt["completed_at"]})
            atomic_write_json(staging / "latest/last_attempt_status.json", load_json_object(staging / "last_run_status.json"))
            # Check data separately from Git storage; do not prune historical
            # snapshots as a side effect of this source migration.
            size = tree_bytes(staging)
            if size >= 900_000_000:
                raise ArtifactContractError("Wind publication exceeds the 900 MB active data gate")
            atomic_write_json(staging / "storage_status.json", {
                "schema_version": 1, "state": "capacity_ok", "data_bytes": size,
                "max_data_bytes": 900_000_000, "snapshot_limit": 5,
                "historical_facts_pruned": False, "checked_at": receipt["completed_at"]})
            backup = Path(temporary) / "previous-data"
            if data_dir.exists():
                os.replace(data_dir, backup)
            try:
                os.replace(staging, data_dir)
            except BaseException:
                if backup.exists():
                    os.replace(backup, data_dir)
                raise
            return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--profile", type=Path, help="Required for candidates collected before profile provenance was embedded")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(publish(args.candidate, args.data_dir, profile=args.profile), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"state": "wind_not_published", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
