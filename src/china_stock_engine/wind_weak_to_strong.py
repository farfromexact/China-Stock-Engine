"""Collect, validate and promote the private Wind reversal warehouse."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .retention import tree_bytes
from .storage import (ArtifactContractError, atomic_copy, atomic_write_json, atomic_write_parquet,
                      json_sha256, load_json_object, read_parquet_file, sha256_file,
                      verify_latest_artifacts)
from .weak_to_strong import (ACTIVE, HISTORY_SESSIONS, MAX_CANDIDATES, MAX_FAILURES,
                             MAX_PACKAGE_BYTES, PRODUCT, RECENT_SESSIONS, RULE, RULE_VERSION,
                             STATES, scan)
from .wind_incremental import IncrementalWind, OHLC
from .wind_provider import CODE, LEAN_QUOTE_FIELDS, adjustment_pair
from .wind_session import WindError

SHANGHAI = ZoneInfo("Asia/Shanghai")
PACKAGE = f"{PRODUCT}/latest.json"
KEYS = ["trade_date", "thscode"]
RAW_COLUMNS = [*KEYS, *LEAN_QUOTE_FIELDS]
BAR_COLUMNS = [*RAW_COLUMNS, "adj_factor", "known_at"]
UNIVERSE_COLUMNS = ["thscode", "security_name", "listing_date"]
EXCHANGES = {"SH", "SZ", "BJ"}
MIN_UNIVERSE = 5000
MIN_OBSERVED_PCT = 98
MAX_REPO_DATA_BYTES = 900_000_000
RETENTION = "local_daily_rolling"
ADJUSTMENT = "wind_native_adjusted_ohlc_validated_ratio"
INPUT_FILES = ("bars.parquet", "universe.parquet", "calendar.parquet")
VINTAGE_FILES = (*INPUT_FILES, "observations.parquet", "events.json", "package.json")
HEX_24 = re.compile(r"[0-9a-f]{24}")
HEX_64 = re.compile(r"[0-9a-f]{64}")


def completed_bound(at: datetime) -> str:
    local = at.astimezone(SHANGHAI)
    day = local.date() - timedelta(days=local.time() < time(15, 30))
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


def trade_date(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Expected YYYY-MM-DD trading date")
    return value


def _observed_at(value):
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ValueError("Wind observation time requires a timezone")
    return at


def sessions(calendar: pd.DataFrame, end: str) -> list[str]:
    days = sorted(calendar.loc[calendar.is_open & calendar.trade_date.le(end), "trade_date"].unique())
    if len(days) < HISTORY_SESSIONS:
        raise WindError("Wind has not certified a complete 30-session reversal window")
    return days[-HISTORY_SESSIONS:]


def _valid_inputs(bars, universe, days, observed_at, *, full_market=True):
    if len(days) != HISTORY_SESSIONS or days != sorted({trade_date(day) for day in days}):
        raise ArtifactContractError("Reversal input does not contain 30 ordered sessions")
    if (observed_at > datetime.now(timezone.utc)
            or days[-1] > completed_bound(observed_at)):
        raise ArtifactContractError("Reversal source time is before market finality or in the future")
    if (universe.thscode.duplicated().any() or not universe.thscode.map(
            lambda code: isinstance(code, str) and bool(CODE.fullmatch(code))).all()
            or not universe.security_name.map(lambda name: isinstance(name, str) and bool(name.strip())).all()
            or not universe.listing_date.map(lambda listed: trade_date(listed) <= days[-1]).all()):
        raise ArtifactContractError("Reversal stock pool has invalid codes, names or listing dates")
    exchanges = set(universe.thscode.str[-2:])
    if full_market and (len(universe) < MIN_UNIVERSE or exchanges != EXCHANGES):
        raise ArtifactContractError("Reversal publication requires the full SH/SZ/BJ stock pool")

    if bars.duplicated(KEYS).any() or not bars.trade_date.isin(days).all():
        raise ArtifactContractError("Reversal price keys are duplicated or outside the certified window")
    listing = bars.thscode.map(universe.set_index("thscode").listing_date)
    if listing.isna().any() or bars.trade_date.lt(listing).any():
        raise ArtifactContractError("Reversal input contains unknown or pre-listing bars")
    prices = bars[[*OHLC, "adj_factor"]]
    adjusted = bars[OHLC].mul(bars.adj_factor, axis=0)
    if (prices.isna().any().any() or not (np.isfinite(prices) & prices.gt(0)).all().all()
            or not (np.isfinite(adjusted) & adjusted.gt(0)).all().all()
            or bars.low.gt(bars[["open", "close"]].min(axis=1)).any()
            or bars.high.lt(bars[["open", "close"]].max(axis=1)).any()):
        raise ArtifactContractError("Reversal OHLC or adjustment basis is invalid")
    turnover = bars[["volume", "amount"]]
    if (turnover.lt(0) | np.isinf(turnover)).any().any():
        raise ArtifactContractError("Reversal turnover has negative or infinite observations")
    timestamps = pd.to_datetime(bars.known_at, utc=True, format="mixed", errors="coerce")
    earliest = pd.to_datetime(bars.trade_date + "T15:30:00+08:00", utc=True)
    if timestamps.isna().any() or timestamps.gt(observed_at).any() or timestamps.lt(earliest).any():
        raise ArtifactContractError("A reversal bar has an invalid or future observation time")


def observe(bars, universe, days, observed_at, *, full_market=True):
    _valid_inputs(bars, universe, days, _observed_at(observed_at), full_market=full_market)
    expected = pd.DataFrame({"trade_date": days}).merge(universe[["thscode", "listing_date"]], how="cross")
    expected = expected.loc[expected.trade_date.ge(expected.listing_date), KEYS]
    frame = expected.merge(bars[[*KEYS, "volume", "amount"]], on=KEYS, how="left",
                           validate="one_to_one", indicator=True)
    traded = (frame.volume.gt(0) & frame.amount.gt(0)).fillna(False).to_numpy(dtype=bool)
    no_turnover = (frame.volume.eq(0) & frame.amount.eq(0)).fillna(False).to_numpy(dtype=bool)
    frame["state"] = np.select([traded, no_turnover, frame["_merge"].eq("left_only")],
                               ["traded", "no_turnover", "missing"], default="turnover_unknown")
    frame["exchange"] = frame.thscode.str[-2:]
    frame["observed"] = traded | no_turnover
    by_day = frame.groupby(["exchange", "trade_date"], sort=True)["observed"].agg(["size", "sum"])
    percentages = by_day["sum"] / by_day["size"] * 100
    if full_market and (len(percentages) != len(EXCHANGES) * HISTORY_SESSIONS
                        or percentages.lt(MIN_OBSERVED_PCT).any()):
        bad = percentages[percentages.lt(MIN_OBSERVED_PCT)].head(6)
        raise ArtifactContractError(f"Reversal observation coverage below {MIN_OBSERVED_PCT}%: {bad.to_dict()}")
    minimum = percentages.groupby(level="exchange").min()
    latest = by_day.xs(days[-1], level="trade_date")
    summary = {
        "universe": len(universe),
        "minimum_pct_by_exchange": {exchange: round(float(value), 6) for exchange, value in minimum.items()},
        "latest_by_exchange": {exchange: {"expected": int(row["size"]), "observed": int(row["sum"])}
                               for exchange, row in latest.iterrows()},
        "latest_states": {state: int(count) for state, count in
                          frame.loc[frame.trade_date.eq(days[-1]), "state"].value_counts().sort_index().items()},
    }
    return frame[[*KEYS, "state"]], summary


def _package(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PACKAGE_BYTES:
        raise ArtifactContractError("Reversal package is missing, a symlink or above 64 KiB")
    pack = load_json_object(path, missing_ok=False)
    digest = pack.pop("content_sha256", None)
    if digest != json_sha256(pack):
        raise ArtifactContractError("Reversal package content hash mismatch")
    if (pack["schema_version"] != 1 or pack["provider"] != "windpy"
            or pack["rule_version"] != RULE_VERSION or pack["rule"] != RULE
            or not HEX_64.fullmatch(pack["source_sha256"])):
        raise ArtifactContractError("Invalid reversal source or rule version")
    at = _observed_at(pack["observed_at"])
    market_dates = [trade_date(item["trade_date"]) for item in pack["market"]]
    if (len(market_dates) != RECENT_SESSIONS or market_dates != sorted(set(market_dates))
            or trade_date(pack["trade_date"]) != market_dates[-1] or pack["trade_date"] > completed_bound(at)
            or at > datetime.now(timezone.utc)):
        raise ArtifactContractError("Reversal package trade date or finality mismatch")
    coverage = pack["coverage"]
    venues = coverage["minimum_pct_by_exchange"]
    if (coverage["universe"] < MIN_UNIVERSE or set(venues) != EXCHANGES
            or not all(MIN_OBSERVED_PCT <= pct <= 100 for pct in venues.values())):
        raise ArtifactContractError("Reversal package does not have full-market coverage")
    counts = pack["counts"]
    candidates, failures = pack["candidates"], pack["recent_failures"]
    active = sum(counts[state] for state in ACTIVE)
    events = [*candidates, *failures]
    if (counts["matched"] != sum(counts[state] for state in STATES)
            or counts["total_triggers"] != sum(day["triggered"] for day in pack["market"])
            or counts["matched"] > counts["total_triggers"]
            or len(candidates) != min(active, MAX_CANDIDATES)
            or counts["omitted_candidates"] != active - len(candidates)
            or len(failures) != min(counts["failed"], MAX_FAILURES)
            or counts["omitted_failures"] != counts["failed"] - len(failures)
            or len({event["code"] for event in events}) != len(events)
            or any(event["state"] not in ACTIVE for event in candidates)
            or any(event["state"] != "failed" for event in failures)
            or any(not CODE.fullmatch(event["code"]) or event["trigger_date"] not in market_dates
                   for event in events)):
        raise ArtifactContractError("Reversal package has inconsistent selections or totals")
    return {**pack, "content_sha256": digest}


def validate_publication(data_dir: Path, workspace: Path | None = None) -> tuple[bool, dict]:
    """Validate the consumer package, and its private input receipt when supplied."""
    try:
        path = data_dir / PACKAGE
        pack = _package(path)
        if workspace is not None:
            store = Warehouse(workspace)
            current = store.current()
            if not current or current["package_sha256"] != sha256_file(path):
                raise ArtifactContractError("Public reversal package differs from the verified private vintage")
        return True, {"verified": True, "product": PRODUCT, "trade_date": pack["trade_date"],
                      "source_sha256": pack["source_sha256"], "package_bytes": path.stat().st_size,
                      "counts": pack["counts"]}
    except (ValueError, OSError, TypeError, KeyError) as exc:
        return False, {"verified": False, "product": PRODUCT, "error": str(exc)}


class Warehouse:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.facts = self.workspace / "facts/wind"
        self.pointer = self.workspace / "current.json"
        self.pending = self.workspace / "pending.json"

    def directory(self, vintage: str) -> Path:
        if not HEX_24.fullmatch(vintage):
            raise ArtifactContractError("Invalid private Wind vintage")
        path = self.facts / f"vintage={vintage}"
        if not path.resolve().is_relative_to(self.workspace) or path.is_symlink():
            raise ArtifactContractError("Wind vintage is outside its private warehouse")
        return path

    def receipt(self, vintage: str) -> dict:
        directory = self.directory(vintage)
        receipt = load_json_object(directory / "receipt.json", missing_ok=False)
        files = receipt["artifacts"]
        if (receipt["retention_class"] != RETENTION or receipt["vintage"] != vintage
                or not HEX_64.fullmatch(receipt["profile_hash"]) or set(files) != set(VINTAGE_FILES)
                or any((directory / name).is_symlink() or sha256_file(directory / name) != expected
                       for name, expected in files.items())):
            raise ArtifactContractError("Private reversal vintage failed artifact validation")
        fingerprint = _source_hash(files, receipt["profile_hash"], receipt["observed_at"])
        pack = _package(directory / "package.json")
        if (vintage != fingerprint[:24] or pack["source_sha256"] != fingerprint
                or pack["trade_date"] != receipt["trade_date"]
                or pack["observed_at"] != receipt["observed_at"]
                or files["package.json"] != receipt["package_sha256"]):
            raise ArtifactContractError("Private reversal receipt differs from the candidate package")
        return receipt

    def current(self) -> dict:
        pointer = load_json_object(self.pointer)
        return self.receipt(pointer["vintage"]) if pointer else {}

    def stage(self, bars, universe, days, *, observed_at, profile_hash, incremental) -> dict:
        observations, coverage = observe(bars, universe, days, observed_at)
        result = scan(bars, universe, days)
        self.facts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".stage-", dir=self.facts) as staging:
            stage = Path(staging) / "vintage"
            atomic_write_parquet(stage / "bars.parquet", bars[BAR_COLUMNS].sort_values(KEYS))
            atomic_write_parquet(stage / "universe.parquet", universe[UNIVERSE_COLUMNS].sort_values("thscode"))
            atomic_write_parquet(stage / "calendar.parquet", pd.DataFrame({"trade_date": days}))
            atomic_write_parquet(stage / "observations.parquet", observations)
            atomic_write_json(stage / "events.json", {"trade_date": days[-1], "events": result.pop("events")})
            artifacts = {name: sha256_file(stage / name) for name in VINTAGE_FILES if name != "package.json"}
            source = _source_hash(artifacts, profile_hash, observed_at)
            pack = {"schema_version": 1, "provider": "windpy", "rule_version": RULE_VERSION, "rule": RULE,
                    "trade_date": days[-1], "observed_at": observed_at, "source_sha256": source,
                    "coverage": coverage, **result}
            pack["content_sha256"] = json_sha256(pack)
            atomic_write_json(stage / "package.json", pack, compact=True)
            _package(stage / "package.json")
            artifacts["package.json"] = sha256_file(stage / "package.json")
            vintage = source[:24]
            receipt = {"vintage": vintage, "trade_date": days[-1], "observed_at": observed_at,
                       "profile_hash": profile_hash, "retention_class": RETENTION, "sessions": len(days),
                       "rows": len(bars), "incremental": incremental, "artifacts": artifacts,
                       "package_sha256": artifacts["package.json"]}
            atomic_write_json(stage / "receipt.json", receipt)
            destination = self.directory(vintage)
            if not destination.exists():
                stage.rename(destination)
        receipt = self.receipt(vintage)
        atomic_write_json(self.pending, {"vintage": vintage})
        return receipt

    def promote(self, data_dir: Path, profile_hash: str, bound: str) -> dict:
        pending = load_json_object(self.pending)
        if not pending:
            return {}
        receipt = self.receipt(pending["vintage"])
        current = self.current()
        destination = data_dir / PACKAGE
        published = _package(destination) if destination.exists() else {}
        if (receipt["profile_hash"] != profile_hash
                or not max(current.get("trade_date", ""), published.get("trade_date", ""))
                <= receipt["trade_date"] <= bound):
            raise ArtifactContractError("Pending reversal date or profile differs from current publication")
        source = self.directory(receipt["vintage"]) / "package.json"
        if destination.parent.is_symlink() or destination.is_symlink():
            raise ArtifactContractError("Reversal publication refuses symlinks")
        before_bytes = destination.stat().st_size if destination.exists() else 0
        if tree_bytes(data_dir) - before_bytes + source.stat().st_size >= MAX_REPO_DATA_BYTES:
            raise ArtifactContractError("Reversal publication would exceed the 900 MB data gate")
        atomic_copy(source, destination)
        atomic_write_json(self.pointer, {"vintage": receipt["vintage"]})
        valid, details = validate_publication(data_dir, self.workspace)
        if not valid:
            raise ArtifactContractError(details["error"])
        self.pending.unlink()
        for directory in self.facts.glob("vintage=*"):
            vintage = directory.name.removeprefix("vintage=")
            if vintage != receipt["vintage"] and HEX_24.fullmatch(vintage) and directory == self.directory(vintage):
                old = load_json_object(directory / "receipt.json")
                if old.get("retention_class") == RETENTION and old.get("vintage") == vintage:
                    shutil.rmtree(directory, ignore_errors=True)
        return receipt

    def load_basis(self, data_dir, days, profile_hash, current):
        if current and current["profile_hash"] == profile_hash:
            directory = self.directory(current["vintage"])
            bars = read_parquet_file(directory / "bars.parquet")
            return (bars.loc[bars.trade_date.isin(days)].copy(),
                    read_parquet_file(directory / "universe.parquet"), current)
        legacy = _legacy_receipt(data_dir, profile_hash)
        return _legacy_basis(data_dir, days, legacy) if legacy else _empty_basis()


def _source_hash(artifacts, profile_hash, observed_at):
    return json_sha256({"files": {key: artifacts[key] for key in INPUT_FILES},
                        "profile_hash": profile_hash, "observed_at": observed_at})


def _empty_basis():
    return pd.DataFrame(columns=BAR_COLUMNS), pd.DataFrame(columns=UNIVERSE_COLUMNS), {}


def _legacy_receipt(data_dir: Path, profile_hash: str | None = None) -> dict:
    status = load_json_object(data_dir / "wind_publication_status.json")
    if not status or status.get("state") != "published" or status.get("provider") != "windpy":
        return {}
    vintage = status["vintage"]
    if not HEX_24.fullmatch(vintage):
        raise ArtifactContractError("Invalid published legacy Wind vintage")
    root = data_dir / "facts/wind" / f"vintage={vintage}"
    receipt = load_json_object(root / "candidate_receipt.json", missing_ok=False)
    if (receipt["units_verified"] is not True or receipt.get("canary_only") is not False
            or (profile_hash is not None and receipt["profile_hash"] != profile_hash)):
        return {}
    manifest, errors = verify_latest_artifacts(data_dir)
    if (errors or manifest.get("provider") != "windpy" or manifest.get("verified") is not True
            or manifest.get("trade_date") != status["trade_date"] or manifest.get("wind_vintage") != vintage
            or json_sha256(manifest.get("provenance", {}).get("profile")) != receipt["profile_hash"]):
        raise ArtifactContractError("Published legacy Wind seed failed latest validation")
    return {**receipt, **status}


def _legacy_basis(data_dir, days, legacy):
    root = data_dir / "facts/wind" / f"vintage={legacy['vintage']}"
    raw = []
    for day in days:
        directory = root / "market" / f"trade_date={day}"
        receipt = load_json_object(directory / "market_receipt.json")
        if (receipt.get("verified") is not True or receipt.get("provider") != "windpy"
                or receipt.get("trade_date") != day):
            continue
        path = directory / "daily_quotes.parquet"
        if not path.is_file() or sha256_file(path) != receipt["artifacts"][path.name]["sha256"]:
            continue  # Treat the partition as a live gap.
        part = read_parquet_file(path, columns=RAW_COLUMNS)
        if not part.trade_date.eq(day).all():
            raise ArtifactContractError("Published seed partition contains a different trading date")
        raw.append(part.assign(price_observed_at=receipt["collection_completed_at"]))
    if not raw:
        bars = pd.DataFrame(columns=BAR_COLUMNS)
    else:
        factors = []
        for path in sorted((root / "adjustment").rglob("adjustment_factors.parquet")):
            table = read_parquet_file(path, columns=[*KEYS, "adj_factor", "raw_close",
                                                     "known_at", "adjustment_method"])
            factors.append(table.loc[table.trade_date.isin(days) & table.adjustment_method.eq(ADJUSTMENT)])
        bars = pd.DataFrame(columns=BAR_COLUMNS)
        if factors:
            merged = pd.concat(raw, ignore_index=True).merge(
                pd.concat(factors, ignore_index=True), on=KEYS, validate="one_to_one")
            cutoff = _observed_at(legacy["completed_at"])
            factor_time = pd.to_datetime(merged.known_at, utc=True, format="mixed", errors="coerce")
            price_time = pd.to_datetime(merged.price_observed_at, utc=True, format="mixed", errors="coerce")
            usable = (factor_time.le(cutoff) & price_time.le(cutoff)
                      & np.isclose(merged.close, merged.raw_close, rtol=1e-10, atol=1e-6)
                      & merged.adj_factor.between(0, float("inf"), inclusive="neither"))
            actual = factor_time.where(factor_time.ge(price_time), price_time)
            merged["known_at"] = actual.map(lambda stamp: stamp.isoformat())
            bars = merged.loc[usable, BAR_COLUMNS].copy()
    names = read_parquet_file(data_dir / "latest/universe.parquet", columns=["thscode", "security_name"])
    listing = read_parquet_file(data_dir / "latest/security_reference.parquet", columns=["thscode", "listing_date"])
    return bars, names.merge(listing, on="thscode", validate="one_to_one"), legacy


def collect(provider, days, basis, *, batch_size=100, audit_all=False, progress=None):
    if not provider.profile.units_verified:
        raise WindError("Reversal collection requires a reviewed Wind unit profile")
    bars, basis_universe, source = basis
    end = days[-1]
    universe = provider.fetch_universe(end)[["thscode", "security_name"]].sort_values("thscode")
    if len(universe) < MIN_UNIVERSE or set(universe.thscode.str[-2:]) != EXCHANGES:
        raise WindError("Wind did not return a full reversal stock pool")
    universe = universe.merge(basis_universe[["thscode", "listing_date"]], on="thscode", how="left",
                              validate="one_to_one")
    new_codes = universe.loc[universe.listing_date.isna(), "thscode"].tolist()
    if new_codes:
        new = provider.fetch_security_reference(new_codes, end, batch_size=batch_size)
        lookup = new.set_index("thscode").listing_date
        universe["listing_date"] = universe.listing_date.fillna(universe.thscode.map(lookup))
    if universe.listing_date.isna().any() or not universe.listing_date.map(lambda value: trade_date(value) <= end).all():
        raise WindError("Wind reversal universe lacks a valid listing date")

    bars = bars.loc[bars.trade_date.ge(bars.thscode.map(universe.set_index("thscode").listing_date))]
    factors = bars[[*KEYS, "adj_factor", "close", "known_at"]].rename(columns={"close": "raw_close"})
    factors["adjustment_method"] = ADJUSTMENT
    seed = {"raw": dict(tuple(bars[RAW_COLUMNS].groupby("thscode"))),
            "factors": dict(tuple(factors.groupby("thscode"))), "receipt": source, "vintage": source.get("vintage")}
    incremental = IncrementalWind(provider, seed, audit_all=audit_all,
                                  sessions=HISTORY_SESSIONS, raw_columns=RAW_COLUMNS)
    incremental.days = days
    incremental.listings = universe.set_index("thscode").listing_date.to_dict()
    codes = universe.thscode.tolist()
    frames = []
    for start in range(0, len(codes), batch_size):
        batch = codes[start:start + batch_size]
        raw = incremental.history_many(batch, days[0], end)
        adjusted = incremental.history_many(batch, days[0], end, adjusted=True)
        paired_raw = pd.concat(raw.values(), ignore_index=True)
        paired_adjusted = pd.concat(adjusted.values(), ignore_index=True)
        pair = adjustment_pair(paired_raw, paired_adjusted)[[*KEYS, "adj_factor", "known_at"]]
        reused = [code for code in batch if incremental.records[code]["reused_rows"]]
        if reused:
            prior = factors.loc[factors.thscode.isin(reused), [*KEYS, "known_at"]]
            pair = pair.merge(prior.rename(columns={"known_at": "prior_known_at"}), on=KEYS,
                              how="left", validate="one_to_one")
            pair["known_at"] = pair.pop("prior_known_at").fillna(pair.known_at)
        frames.append(paired_raw.merge(pair, on=KEYS, validate="one_to_one"))
        if progress is not None:
            progress(start + len(batch), len(codes))
    return pd.concat(frames, ignore_index=True), universe, incremental.incremental_status



def canary(provider, end, codes, *, batch_size=5):
    if trade_date(end) > completed_bound(datetime.now(timezone.utc)):
        raise WindError("Reversal canary requires a completed trading date")
    if not 1 <= batch_size <= 100 or not 1 <= len(codes) <= 20 or len(set(codes)) != len(codes):
        raise WindError("Reversal canary requires 1-20 distinct codes and a valid batch size")
    days = sessions(provider.fetch_trade_calendar(end, offset=-30), end)[-5:]
    if days[-1] != end:
        raise WindError("Reversal canary end is not a certified trading date")
    universe = provider.fetch_universe(end)
    if not set(codes) <= set(universe.thscode):
        raise WindError("A canary code is absent from the Wind stock pool")
    refs = provider.fetch_security_reference(codes, end, batch_size=batch_size).set_index("thscode")
    samples = []
    for start in range(0, len(codes), batch_size):
        batch = codes[start:start + batch_size]
        raw = provider.history_many(batch, days[0], end)
        adjusted = provider.history_many(batch, days[0], end, adjusted=True)
        final_close = provider.close_many(batch, end, end)
        final_adjusted = provider.close_many(batch, end, end, adjusted=True)
        for code in batch:
            listing = trade_date(refs.loc[code, "listing_date"])
            pair = adjustment_pair(raw[code], adjusted[code])
            single = provider.history(code, days[0], end)
            single_adj = provider.history(code, days[0], end, adjusted=True)
            for actual, expected in (
                (raw[code], single), (adjusted[code], single_adj),
                (final_close[code], single.loc[single.trade_date.eq(end)]),
                (final_adjusted[code], single_adj.loc[single_adj.trade_date.eq(end)]),
            ):
                try:
                    pd.testing.assert_frame_equal(
                        actual[expected.columns.intersection(actual.columns)].reset_index(drop=True),
                        expected[expected.columns.intersection(actual.columns)].reset_index(drop=True),
                        check_dtype=False,
                    )
                except AssertionError as exc:
                    raise WindError(f"Wind single-code and matrix data differ for {code}") from exc
            if not set(single.trade_date) <= {day for day in days if day >= listing}:
                raise WindError("Wind sample has an observation outside its certified listing window")
            traded = single.loc[single.volume.gt(0) & single.amount.gt(0)]
            implied = traded.amount / traded.volume
            if not implied.between(traded.low - 0.02, traded.high + 0.02).all():
                raise WindError(f"Wind amount/volume is inconsistent with the price range for {code}")
            samples.append({"code": code, "listing_date": listing, "observed_days": single.trade_date.tolist(),
                            "traded_days": traded.trade_date.tolist(),
                            "adjustment_factor_changed": bool(pair.adj_factor.round(8).nunique() > 1),
                            "amount_per_share_last": None if implied.empty else round(float(implied.iloc[-1]), 4)})
    return {"state": "canary_passed", "canary_only": True, "production_changed": False,
            "trade_date": end, "calendar": days, "reviewed_profile": provider.profile.units_verified,
            "samples": samples}


def replay(data_dir: Path, end: str | None = None) -> dict:
    legacy = _legacy_receipt(data_dir)
    if not legacy:
        raise ArtifactContractError("Replay requires a verified published Wind vintage")
    end = trade_date(end) if end is not None else legacy["trade_date"]
    calendar = read_parquet_file(data_dir / "latest/trading_calendar.parquet")
    days = sessions(calendar, end)
    if end > legacy["trade_date"] or days[-1] != end:
        raise ArtifactContractError("Replay end is outside the published Wind trading dates")
    bars, universe, _ = _legacy_basis(data_dir, days, legacy)
    universe = universe.loc[universe.listing_date.le(end)]
    bars = bars.loc[bars.thscode.isin(universe.thscode)]
    _, coverage = observe(bars, universe, days, legacy["completed_at"])
    result = scan(bars, universe, days, include_history=True)
    return {"state": "offline_replay", "trade_date": end, "observed_at": legacy["completed_at"],
            "wind_vintage": legacy["vintage"], "rows": len(bars), "coverage": coverage,
            **{key: result[key] for key in ("market", "counts")}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("replay",))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--end")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(replay(args.data_dir, args.end), ensure_ascii=False, indent=2))
        return 0
    except (WindError, ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"state": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
