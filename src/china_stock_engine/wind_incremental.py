"""Reuse one published Wind vintage; fetch gaps and audit vendor revisions."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import math
from pathlib import Path
import re

import pandas as pd

from .storage import (json_sha256, load_json_object, load_manifest,
                      read_parquet_file, sha256_file, verify_latest_artifacts)
from .wind_collect import collect_candidate
from .wind_provider import adjustment_pair
from .wind_session import WindError

RAW = ["trade_date", "thscode", "open", "high", "low", "close", "pre_close",
       "avg_price", "volume", "amount", "turnover_ratio", "change_ratio",
       "source_provider", "source_endpoint"]
FACTOR = ["trade_date", "thscode", "adj_factor", "raw_close", "known_at", "adjustment_method"]
OHLC = ["open", "high", "low", "close"]
NUMERIC = RAW[2:-2]
AUDIT_BUCKETS = 20


def same(a, b):
    if pd.isna(a) or pd.isna(b):
        return bool(pd.isna(a) and pd.isna(b))
    return math.isclose(float(a), float(b), rel_tol=1e-10, abs_tol=1e-6)


def audit_bucket(code: str, buckets: int = AUDIT_BUCKETS) -> int:
    return int(hashlib.sha256(code.encode("ascii")).hexdigest(), 16) % buckets


def missing_runs(days, missing):
    """Join only adjacent certified sessions, never download intervening cached days."""
    runs = []
    previous = -2
    for index, day in enumerate(days):
        if day not in missing:
            continue
        if index != previous + 1:
            runs.append([day, day])
        else:
            runs[-1][1] = day
        previous = index
    return runs


def load_seed(data: Path, profile_hash: str):
    manifest = load_manifest(data / "latest/manifest.json")
    if not manifest:
        return None
    if (manifest.get("provider") != "windpy" or manifest.get("verified") is not True
            or json_sha256(manifest.get("provenance", {}).get("profile")) != profile_hash):
        raise WindError("Incremental update requires the published Wind profile; explicitly rebuild a baseline after profile changes")
    _, errors = verify_latest_artifacts(data)
    if errors:
        raise WindError("Incremental source latest failed artifact verification")
    vintage = manifest.get("wind_vintage", "")
    if not re.fullmatch(r"[0-9a-f]{24}", vintage):
        raise WindError("Incremental source has no valid Wind vintage")
    source = data / "facts/wind" / f"vintage={vintage}"
    receipt = load_json_object(source / "candidate_receipt.json", missing_ok=False)
    if receipt.get("profile_hash") != profile_hash or receipt.get("units_verified") is not True:
        raise WindError("Incremental source unit/profile receipt is not verified")
    raw = []
    for directory in sorted((source / "market").glob("trade_date=*")):
        metadata = load_json_object(directory / "market_receipt.json")
        good = metadata.get("verified") is True and metadata.get("provider") == "windpy"
        for name in ("daily_quotes.parquet", "daily_security_status.parquet"):
            path = directory / name
            good = good and path.exists() and metadata.get("artifacts", {}).get(name, {}).get("sha256") == sha256_file(path)
        if not good:
            continue  # Missing/corrupt partitions are gaps, not trusted seed bars.
        frame = read_parquet_file(directory / "daily_quotes.parquet", columns=RAW)
        if not frame.trade_date.eq(directory.name.removeprefix("trade_date=")).all():
            raise WindError("Incremental source partition contains a different date")
        raw.append(frame)
    raw = pd.concat(raw, ignore_index=True) if raw else pd.DataFrame(columns=RAW)
    factors = []
    for path in sorted((source / "adjustment").glob("**/adjustment_factors.parquet")):
        try:
            frame = read_parquet_file(path, columns=FACTOR)
        except (ValueError, OSError):
            continue  # Codes without a usable basis get native full-window repair.
        factors.append(frame)
    factors = pd.concat(factors, ignore_index=True) if factors else pd.DataFrame(columns=FACTOR)
    if raw.duplicated(["thscode", "trade_date"]).any() or factors.duplicated(["thscode", "trade_date"]).any():
        raise WindError("Incremental source contains duplicate observations")
    return {"vintage": vintage, "raw": dict(tuple(raw.groupby("thscode"))),
            "factors": dict(tuple(factors.groupby("thscode"))), "receipt": receipt}


class IncrementalWind:
    """Present a complete candidate to existing quality gates, without full downloads."""
    provider = "windpy"

    def __init__(self, provider, seed, *, audit_all=False):
        self.base, self.seed, self.profile = provider, seed, provider.profile
        self.source_endpoints = provider.source_endpoints
        self.cursor = int((seed["receipt"].get("incremental") or {}).get("next_audit_bucket", 0)) % AUDIT_BUCKETS
        self.audit_all = audit_all
        self.records, self.pending = {}, {}
        self.actual_downloaded_rows = 0
        self.provenance = {**provider.provenance, "incremental_source_vintage": seed["vintage"],
            "revision_policy": "all_window_raw_and_adjusted_close_plus_20_bucket_full_field_audit"}

    def fetch_trade_calendar(self, end, **kwargs):
        result = self.base.fetch_trade_calendar(end, **kwargs)
        self.days = sorted(result.loc[result.is_open & result.trade_date.le(end), "trade_date"].unique())[-300:]
        return result

    def fetch_universe(self, end):
        return self.base.fetch_universe(end)

    def fetch_security_reference(self, codes, end, **kwargs):
        result = self.base.fetch_security_reference(codes, end, **kwargs)
        self.listings = result.set_index("thscode").listing_date.to_dict()
        return result

    @property
    def incremental_status(self):
        records = list(self.records.values())
        return {"mode": "incremental", "source_vintage": self.seed["vintage"],
            "audit_bucket": self.cursor, "audit_buckets": AUDIT_BUCKETS,
            "next_audit_bucket": (self.cursor + 1) % AUDIT_BUCKETS,
            "audit_all": self.audit_all, "close_probe_sessions": len(self.days),
            "completed_codes": len(records), "raw_rows_downloaded_this_attempt": self.actual_downloaded_rows,
            "gap_rows_downloaded": sum(r["gap_rows"] for r in records),
            "reused_rows": sum(r["reused_rows"] for r in records),
            "audited_codes": sorted(code for code, r in self.records.items() if r["audited"]),
            "repairs": {code: r["reasons"] for code, r in sorted(self.records.items()) if r["reasons"]},
            "checkpoint_reused_codes": sum(r.get("checkpoint_reused", False) for r in records)}

    def checkpoint_metadata(self, code):
        return self.records[code]

    def reuse_checkpoint(self, code, metadata):
        if code not in self.records:
            if not isinstance(metadata, dict):
                raise WindError("Incremental checkpoint lacks revision-audit metadata")
            self.records[code] = {**metadata, "checkpoint_reused": True}

    def history(self, code, start, end, *, adjusted=False):
        return self.history_many([code], start, end, adjusted=adjusted)[code]

    def history_many(self, codes, start, end, *, adjusted=False):
        key = tuple(codes)
        if adjusted:
            if key not in self.pending:
                raise WindError("Incremental adjusted history requested without paired raw observations")
            return self.pending.pop(key)
        raw, adj = self._prepare(codes, start, end)
        self.pending[key] = adj
        return raw

    def _prepare(self, codes, start, end):
        if not self.days or self.days[0] != start or self.days[-1] != end:
            raise WindError("Incremental range differs from its certified 300-session window")
        raw, adjusted, old_factors, repairs, gaps = {}, {}, {}, {}, {}
        for code in codes:
            listing = self.listings.get(code)
            if not listing or pd.isna(listing):
                raise WindError("Incremental source lacks a unique listing date")
            original = self.seed["raw"].get(code, pd.DataFrame(columns=RAW))
            original = original.loc[original.trade_date.isin(self.days)].copy().reset_index(drop=True)
            factors = self.seed["factors"].get(code, pd.DataFrame(columns=FACTOR))
            factors = factors.loc[factors.trade_date.isin(original.trade_date)].copy().set_index("trade_date")
            raw[code], old_factors[code] = original, factors
            gaps[code] = set(d for d in self.days if d >= str(listing)) - set(original.trade_date)
            repairs[code] = []
            if original.empty:
                repairs[code].append("new_security_or_missing_basis")
            elif (set(original.trade_date) != set(factors.index)
                    or not factors.adjustment_method.eq("wind_native_adjusted_ohlc_validated_ratio").all()
                    or not pd.to_numeric(factors.adj_factor, errors="coerce").between(0, float("inf"), inclusive="neither").all()):
                repairs[code].append("missing_or_invalid_adjustment_basis")

        eligible = [code for code in codes if not repairs[code]]
        closes = self.base.close_many(eligible, start, end) if eligible else {}
        adj_closes = self.base.close_many(eligible, start, end, adjusted=True) if eligible else {}
        for code in eligible:
            current = closes[code].set_index("trade_date").close
            adjusted_close = adj_closes[code].set_index("trade_date").close
            if set(current.index) != set(adjusted_close.index):
                raise WindError("Wind revision close probes disagree on observed dates")
            prior = raw[code].set_index("trade_date").close
            factor = old_factors[code]
            if not set(prior.index) <= set(current.index):
                repairs[code].append("historical_observation_removed")
            else:
                if any(not same(current[d], prior[d]) for d in prior.index):
                    repairs[code].append("historical_close_revised")
                if any(not same(factor.loc[d, "raw_close"], prior[d])
                       or not same(adjusted_close[d], current[d] * factor.loc[d, "adj_factor"])
                       for d in prior.index):
                    repairs[code].append("adjustment_basis_revised")

        audited = [code for code in eligible if not repairs[code]
                   and (self.audit_all or audit_bucket(code) == self.cursor)]
        audit_raw = self.base.history_many(audited, start, end) if audited else {}
        audit_adj = self.base.history_many(audited, start, end, adjusted=True) if audited else {}
        self.actual_downloaded_rows += sum(len(frame) for frame in audit_raw.values())
        for code in audited:
            adjustment_pair(audit_raw[code], audit_adj[code])
            prior = raw[code].set_index("trade_date")
            current = audit_raw[code].set_index("trade_date")
            if (not set(prior.index) <= set(current.index)
                    or any(not same(prior.loc[day, field], current.loc[day, field])
                           for day in prior.index if day in current.index for field in NUMERIC)):
                repairs[code].append("historical_full_fields_revised")

        full = [code for code in codes if repairs[code]]
        need_raw = [code for code in full if code not in audit_raw]
        full_raw, full_adj = {}, {}
        full_groups = {}
        for code in full:
            first = max(start, str(self.listings[code]))
            full_groups.setdefault(first, []).append(code)
        for first, group in full_groups.items():
            download = [code for code in group if code in need_raw]
            fetched = self.base.history_many(download, first, end) if download else {}
            full_raw.update(fetched)
            self.actual_downloaded_rows += sum(len(frame) for frame in fetched.values())
            need_adj = [code for code in group if code not in audit_adj]
            if need_adj:
                full_adj.update(self.base.history_many(need_adj, first, end, adjusted=True))
            full_adj.update({code: audit_adj[code] for code in group if code in audit_adj})
        for code in full:
            raw[code] = audit_raw[code] if code in audit_raw else full_raw[code]
            adjusted[code] = full_adj[code]
            adjustment_pair(raw[code], adjusted[code])  # real native OHLC, never a scale guess

        # Group by missing-session set within this bounded code batch. IPOs and
        # isolated holes do not force a whole-market or whole-window download.
        groups = {}
        for code in eligible:
            if repairs[code]:
                continue
            if code in audit_raw:
                raw[code], adjusted[code] = audit_raw[code], audit_adj[code]
                continue
            factors = old_factors[code]
            original = raw[code]
            adj = original[["trade_date", "thscode", *OHLC]].copy()
            for field in OHLC:
                adj[field] = adj[field] * adj.trade_date.map(factors.adj_factor)
            adjusted[code] = adj
            groups.setdefault(tuple(sorted(gaps[code])), []).append(code)
        gap_counts = {code: 0 for code in codes}
        for dates, group in groups.items():
            for first, last in missing_runs(self.days, set(dates)):
                download = [code for code in group if code not in audit_raw]
                fetched = self.base.history_many(download, first, last) if download else {}
                self.actual_downloaded_rows += sum(len(frame) for frame in fetched.values())
                adj = self.base.history_many(group, first, last, adjusted=True)
                for code in group:
                    frame = (audit_raw[code].loc[audit_raw[code].trade_date.between(first, last)].copy()
                             if code in audit_raw else fetched[code])
                    adjustment_pair(frame, adj[code])
                    raw[code] = pd.concat([raw[code], frame], ignore_index=True)
                    adjusted[code] = pd.concat([adjusted[code], adj[code]], ignore_index=True)
                    gap_counts[code] += len(frame)
        for code in codes:
            # Native full-field audit observations supersede cached observations.
            if code in audit_raw and not repairs[code]:
                raw[code] = audit_raw[code]
            raw[code] = raw[code][RAW].sort_values("trade_date").reset_index(drop=True)
            adjusted[code] = adjusted[code][["trade_date", "thscode", *OHLC]].sort_values("trade_date").reset_index(drop=True)
            if not set(raw[code].trade_date) <= set(self.days):
                raise WindError("Incremental result has bars outside certified sessions")
            if code in closes:
                # Detect a vendor changing the snapshot during the paired fetch.
                for frame, probe in ((raw[code], closes[code]), (adjusted[code], adj_closes[code])):
                    series = frame.set_index("trade_date").close
                    expected = probe.set_index("trade_date").close
                    if set(series.index) != set(expected.index) or any(not same(series[d], expected[d]) for d in series.index):
                        raise WindError("Wind observations changed during revision verification; retry later")
            if repairs[code] or code in audited:
                gap_counts[code] = sum(day in gaps[code] for day in raw[code].trade_date)
            self.records[code] = {"gap_rows": gap_counts[code], "audited": code in audited,
                "reused_rows": 0 if repairs[code] or code in audited else len(raw[code]) - gap_counts[code],
                "reasons": repairs[code]}
        return raw, adjusted


def collect_incremental(provider, workspace: Path, *, data_dir: Path, end: str,
                        batch_size=100, min_universe=5000, build_report=True, audit_all=False,
                        codes: list[str] | None = None):
    if not provider.profile.units_verified:
        raise WindError("Incremental collection requires a reviewed local unit profile")
    profile_hash = json_sha256(asdict(provider.profile))
    seed = load_seed(data_dir, profile_hash)
    if seed is None:
        # A genuinely empty installation has nothing to reuse. Other-provider or
        # changed-profile installations require an explicit reviewed baseline.
        return collect_candidate(provider, workspace, end=end, sessions=300,
            batch_size=batch_size, min_universe=min_universe, build_report=build_report, codes=codes)
    adapter = IncrementalWind(provider, seed, audit_all=audit_all)
    context = json_sha256({"source_vintage": seed["vintage"], "end": end,
                          "audit_bucket": adapter.cursor, "audit_all": audit_all, "version": 1})
    return collect_candidate(adapter, workspace, end=end, sessions=300, batch_size=batch_size,
        min_universe=min_universe, build_report=build_report, cache_context=context, refresh=audit_all,
        codes=codes)
