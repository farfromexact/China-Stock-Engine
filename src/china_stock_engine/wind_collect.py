"""Isolated Wind canary/baseline collection; never writes production data/."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import uuid

import pandas as pd

from .data_reference import build_data_reference_outputs
from .pipeline import CollectionConfig, collect_and_publish, validate_latest
from .quality import normalize_universe, normalize_quotes, build_daily_security_status
from .storage import (atomic_write_json, atomic_write_parquet, json_sha256,
                      read_parquet_file, sha256_file)
from .wind_provider import WindProvider, WindProfile, adjustment_pair
from .wind_session import WindSession, WindError, dashboard_running


ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / ".private-stock-data" / "wind"


def now():
    return datetime.now(timezone.utc).isoformat()


def profile_from(path: Path | None) -> WindProfile:
    return WindProfile(**json.loads(path.read_text())) if path else WindProfile()


def doctor() -> dict:
    # find_spec locates the path without importing the native SDK.
    specification = importlib.util.find_spec("WindPy")
    busy = dashboard_running()
    return {"sdk_path_configured": specification is not None,
            "dashboard_running": busy,
            "live_session_started": False,
            "state": "blocked_dashboard_running" if busy else "exclusive_window_required",
            "login_verified": False}


class CachedWind:
    provider = "windpy"
    source_endpoints = WindProvider.source_endpoints

    def __init__(self, universe, reference, calendar, quotes, provenance, started):
        self.universe, self.reference, self.calendar, self.quotes = universe, reference, calendar, quotes
        self.provenance = provenance
        self.collection_started_at, self.collection_completed_at = started, now()

    def fetch_universe(self, _): return self.universe.copy()
    def fetch_trade_calendar(self, _, **kwargs): return self.calendar.copy()
    def fetch_security_reference(self, codes, _, **kwargs):
        return self.reference.loc[self.reference.thscode.isin(codes)].copy()
    def fetch_daily_quotes(self, codes, day, **kwargs):
        return self.quotes.loc[self.quotes.thscode.isin(codes) & self.quotes.trade_date.eq(day)].copy()


def collect_candidate(provider: WindProvider, workspace: Path, *, end: str,
                      sessions: int = 300, codes: list[str] | None = None,
                      min_universe: int = 5000, build_report: bool = True,
                      refresh: bool = False, batch_size: int = 1) -> dict:
    """Collect one normalization vintage. Partial samples can never be promoted."""
    workspace = workspace.resolve()
    if workspace == ROOT / "data" or (ROOT / "data") in workspace.parents:
        raise ValueError("Wind candidate workspace must be outside production data/")
    if sessions < 1 or sessions > 300:
        raise ValueError("Wind candidate requires 1..300 sessions")
    if not 1 <= batch_size <= 100:
        raise ValueError("Wind batch_size must be 1..100")
    if codes is None and not provider.profile.units_verified:
        raise WindError("Full-market collection requires a reviewed unit profile; run canary first")
    started = now()
    profile_hash = json_sha256(asdict(provider.profile))
    calendar = provider.fetch_trade_calendar(end, offset=-(sessions + 30))
    days = sorted(calendar.loc[calendar.is_open & calendar.trade_date.le(end), "trade_date"].unique())
    if len(days) < sessions or not days or days[-1] != end:
        raise WindError("Wind did not certify enough completed sessions ending on the requested date")
    days = days[-sessions:]
    universe = normalize_universe(provider.fetch_universe(end))
    full_universe_count = len(universe)
    if codes is not None:
        missing = set(codes) - set(universe.thscode)
        if missing:
            raise WindError("Canary codes are absent from the Wind universe")
        universe = universe.loc[universe.thscode.isin(codes)].copy()
    elif len(universe) < min_universe or not {"SSE", "SZSE", "BSE"} <= set(universe.exchange):
        raise WindError("Wind full universe does not cover the required size and all three exchanges")
    reference = provider.fetch_security_reference(universe.thscode.tolist(), end)
    cache = workspace / "cache" / profile_hash
    cache.mkdir(parents=True, exist_ok=True)
    if batch_size > 1:
        all_codes = universe.thscode.tolist()
        for start_index in range(0, len(all_codes), batch_size):
            batch = all_codes[start_index:start_index + batch_size]
            missing = []
            for code in batch:
                directory = cache / code
                receipt = directory / "receipt.json"
                metadata = json.loads(receipt.read_text()) if receipt.exists() else {}
                paths = {name: directory / filename for name, filename in
                         (("raw", "raw.parquet"), ("factors", "factors.parquet"))}
                reusable = (not refresh and metadata.get("start") == days[0]
                    and metadata.get("end") == end and metadata.get("profile_hash") == profile_hash
                    and all(p.exists() and metadata.get(name + "_sha256") == sha256_file(p)
                            for name, p in paths.items()))
                if not reusable:
                    missing.append(code)
            if missing:
                raw_batch = provider.history_many(missing, days[0], end)
                adjusted_batch = provider.history_many(missing, days[0], end, adjusted=True)
                for code in missing:
                    directory = cache / code
                    raw = raw_batch[code]
                    if not set(raw.trade_date) <= set(days):
                        raise WindError(f"Wind returned bars on uncertified sessions: {code}")
                    factors = adjustment_pair(raw, adjusted_batch[code])
                    atomic_write_parquet(directory / "raw.parquet", raw)
                    atomic_write_parquet(directory / "factors.parquet", factors)
                    atomic_write_json(directory / "receipt.json", {
                        "start": days[0], "end": end, "profile_hash": profile_hash,
                        "thscode": code, "observed_at": now(),
                        "raw_sha256": sha256_file(directory / "raw.parquet"),
                        "factors_sha256": sha256_file(directory / "factors.parquet")})
            print(json.dumps({"phase": "wind_batch_download", "done": min(start_index + batch_size, len(all_codes)),
                              "total": len(all_codes), "downloaded": len(missing)}), flush=True)
        refresh = False  # Every requested refresh is now in a verified checkpoint.
    raw_frames, factor_frames = [], []
    gaps = []
    for index, code in enumerate(universe.thscode, 1):
        directory = cache / code
        receipt = directory / "receipt.json"
        raw_path, factors_path = directory / "raw.parquet", directory / "factors.parquet"
        metadata = json.loads(receipt.read_text()) if receipt.exists() else {}
        reusable = (not refresh and metadata.get("start") == days[0] and metadata.get("end") == end
                    and metadata.get("profile_hash") == profile_hash
                    and raw_path.exists() and factors_path.exists()
                    and metadata.get("raw_sha256") == sha256_file(raw_path)
                    and metadata.get("factors_sha256") == sha256_file(factors_path))
        if reusable:
            raw, factors = read_parquet_file(raw_path), read_parquet_file(factors_path)
        else:
            raw = provider.history(code, days[0], end)
            adjusted = provider.history(code, days[0], end, adjusted=True)
            if not set(raw.trade_date) <= set(days):
                raise WindError("Wind returned bars on uncertified sessions")
            factors = adjustment_pair(raw, adjusted)
            atomic_write_parquet(raw_path, raw)
            atomic_write_parquet(factors_path, factors)
            atomic_write_json(receipt, {"start": days[0], "end": end,
                "profile_hash": profile_hash, "thscode": code, "observed_at": now(),
                "raw_sha256": sha256_file(raw_path), "factors_sha256": sha256_file(factors_path)})
        listing = reference.loc[reference.thscode.eq(code), "listing_date"]
        if len(listing) != 1 or pd.isna(listing.iloc[0]):
            raise WindError("Wind reference lacks a unique listing date")
        expected = [d for d in days if d >= str(listing.iloc[0])]
        gaps.append({"thscode": code, "observed_sessions": len(raw),
                     "unconfirmed_missing_dates": sorted(set(expected) - set(raw.trade_date)),
                     "listing_date": str(listing.iloc[0])})
        raw_frames.append(raw); factor_frames.append(factors)
        if index % 50 == 0 or index == len(universe):
            print(json.dumps({"phase": "wind_checkpoints", "done": index, "total": len(universe)}), flush=True)
    raw = pd.concat(raw_frames, ignore_index=True)
    factors = pd.concat(factor_frames, ignore_index=True)
    if raw.empty or factors.empty:
        raise WindError("Wind returned no usable raw/adjusted history")
    # Every build gets a private data root; neither failed builds nor samples can
    # replace latest or alter the original provider's facts.
    target = workspace / "candidates" / uuid.uuid4().hex
    target.mkdir(parents=True)
    observed = now()
    for day, frame in raw.groupby("trade_date", sort=True):
        normalized = normalize_quotes(frame, universe)
        directory = target / "facts/market" / f"trade_date={day}"
        atomic_write_parquet(directory / "daily_quotes.parquet", normalized)
        atomic_write_parquet(directory / "daily_security_status.parquet",
                             build_daily_security_status(universe, normalized, day))
        atomic_write_json(directory / "market_receipt.json", {
            "schema_version": 3, "trade_date": day, "provider": "windpy",
            "verified": True, "data_fresh": True, "collection_completed_at": observed,
            "artifacts": {name: {"sha256": sha256_file(directory / name)} for name in
                          ("daily_quotes.parquet", "daily_security_status.parquet")}})
    observation_date = observed[:10]
    # Partition by code prefix to stay below single-file Git limits.
    for prefix, frame in factors.groupby(factors.thscode.str[0]):
        path = target / "facts/adjustment" / f"as_of_date={observation_date}" / f"vintage={profile_hash}-{prefix}" / "adjustment_factors.parquet"
        atomic_write_parquet(path, frame)
    cached = CachedWind(universe, reference, calendar, raw, provider.provenance, started)
    config = CollectionConfig(data_dir=target, min_universe_size=min_universe if codes is None else len(codes),
                              build_data_reference=False)
    result = collect_and_publish(cached, end, config=config)
    if not result.ok or not result.status.get("data_fresh"):
        raise WindError("Wind candidate failed standard market quality gates")
    if build_report:
        build_data_reference_outputs(target, end, research_asof=now())
    valid, validation = validate_latest(data_dir=target, policy_min_universe_size=config.min_universe_size)
    if not valid:
        raise WindError("Wind candidate failed final artifact/quality validation")
    atomic_write_json(target / "wind_coverage.json", {"trade_date": end, "rows": gaps})
    status = {"schema_version": 1, "state": "candidate_quality_verified", "trade_date": end,
              "sessions": sessions, "start": days[0], "rows": len(raw),
              "securities": len(universe), "full_universe_count": full_universe_count,
              "profile_hash": profile_hash, "units_verified": provider.profile.units_verified,
              "canary_only": codes is not None or full_universe_count < 5000, "report_built": build_report,
              "production_changed": False, "ready_for_cutover": False,
              "candidate": str(target), "completed_at": now()}
    atomic_write_json(target / "wind_candidate.json", status)
    atomic_write_json(workspace / "latest_candidate.json", status)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("doctor", "canary", "collect"))
    parser.add_argument("--end")
    parser.add_argument("--sessions", type=int, default=300)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--workspace", type=Path, default=PRIVATE)
    parser.add_argument("--exclusive-window", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="Ignore checkpoints for revised provider observations")
    parser.add_argument("--codes", default="600000.SH,000001.SZ,920001.BJ")
    args = parser.parse_args(argv)
    try:
        if args.mode == "doctor":
            print(json.dumps(doctor(), ensure_ascii=False, indent=2))
            return 0
        if not args.end:
            raise ValueError("--end is required and must be a completed trade date")
        end = datetime.fromisoformat(args.end).date()
        if end.isoformat() != args.end:
            raise ValueError("--end must use YYYY-MM-DD")
        from .cli import default_trade_date
        if end.isoformat() > default_trade_date():
            raise ValueError("Wind collection cannot request an unfinished trade date")
        profile = profile_from(args.profile)
        if args.mode == "collect" and not profile.units_verified:
            raise ValueError("Full-market collection requires a reviewed unit profile; run canary first")
        with WindSession(exclusive_window=args.exclusive_window) as session:
            result = collect_candidate(WindProvider(session.request, profile), args.workspace,
                end=args.end, sessions=5 if args.mode == "canary" else args.sessions,
                codes=args.codes.split(",") if args.mode == "canary" else None,
                build_report=args.mode != "canary", refresh=args.refresh, batch_size=args.batch_size)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (WindError, ValueError, OSError) as exc:
        print(json.dumps({"state": "wind_not_promoted", "error": str(exc), "production_changed": False}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
