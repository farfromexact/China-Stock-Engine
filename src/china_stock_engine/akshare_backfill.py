"""Resumable AKShare full-current-universe history, with normalized checkpoints.

Tencent transport follows AKShare's stock_zh_a_hist_tx endpoint/decoder/units,
using one bounded window instead of downloading overlapping annual windows.
No raw vendor responses or credentials are persisted.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time
from typing import Any
import pandas as pd
from .storage import (ArtifactContractError, atomic_write_json, atomic_write_parquet,
                      json_sha256, load_json_object, load_manifest, sha256_file)

ADAPTER_VERSION = "akshare-tencent-v2"
TX_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def symbol(code: str) -> str:
    value, exchange = code.upper().split(".")
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}[exchange] + value


def thscode(value: Any) -> str:
    value = str(value).strip().zfill(6)
    return value + (".SH" if value.startswith("6") else ".SZ" if value.startswith(("0", "3")) else ".BJ")


class TencentTransport:
    def __init__(self, interval: float = .15, timeout: float = 20):
        self.interval, self.timeout = interval, timeout
        self.lock, self.local = threading.Lock(), threading.local()
        self.last_request = 0.0

    def fetch(self, code: str, start: str, end: str, adjust: str) -> tuple[pd.DataFrame, dict]:
        import requests
        from akshare.utils import demjson
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
        with self.lock:
            pause = self.interval - (time.monotonic() - self.last_request)
            if pause > 0:
                time.sleep(pause)
            self.last_request = time.monotonic()
        response = self.local.session.get(TX_URL, params={
            "_var": "kline_day" + adjust,
            "param": f"{symbol(code)},day,{start},{end},640,{adjust}",
        }, timeout=self.timeout)
        response.raise_for_status()
        text = response.text
        decoded = demjson.decode(text[text.find("={") + 1:])
        data = (decoded.get("data") or {}).get(symbol(code)) or {}
        key = "hfqday" if adjust == "hfq" and "hfqday" in data else "day"
        rows = data.get(key) or []
        if not rows:
            raise ArtifactContractError(f"empty Tencent {adjust or 'raw'} history: {code}")
        if any(len(row) < 9 for row in rows):
            raise ArtifactContractError(f"Tencent schema changed: {code}")
        frame = pd.DataFrame([[row[i] for i in (0, 1, 2, 3, 4, 5, 7, 8)] for row in rows],
                             columns=["trade_date", "open", "close", "high", "low", "volume", "turnover_ratio", "amount"])
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="raise").dt.strftime("%Y-%m-%d")
        for field in frame.columns.difference(["trade_date"]):
            frame[field] = pd.to_numeric(frame[field], errors="raise")
        # Known A-share identity distinguishes sz000xxx stocks from sh000xxx
        # indices; do not inherit the library's sz000 volume exception.
        if not code.startswith(("688", "689")):
            frame["volume"] *= 100
        frame["amount"] *= 10_000
        # AKShare's public Tencent turnover is fraction, our contract is %.
        frame = frame.loc[frame["trade_date"].between(start, end)].sort_values("trade_date")
        if frame["trade_date"].duplicated().any():
            raise ArtifactContractError(f"duplicate Tencent history dates: {code}")
        return frame.reset_index(drop=True), {"key": key, "quote": (data.get("qt") or {}).get(symbol(code))}


def normalize_pair(raw: pd.DataFrame, adjusted: pd.DataFrame, code: str,
                   sessions: list[str], observed_at: str, qt: list | None = None) -> pd.DataFrame:
    """Retain vendor-adjusted OHLC and validate an affine inverse price map.

    Some providers add accumulated cash dividends in hfq prices. A close-only
    ratio must not be applied to high/low as if that affine offset were zero.
    """
    if set(raw["trade_date"]) != set(adjusted["trade_date"]):
        raise ArtifactContractError(f"raw/adjusted date mismatch: {code}")
    joined = raw.merge(adjusted[["trade_date", "open", "high", "low", "close"]],
                       on="trade_date", validate="one_to_one", suffixes=("", "_hfq"))
    if joined.empty or not joined["close"].gt(0).all():
        raise ArtifactContractError(f"invalid raw close: {code}")
    prices = joined[[f + suffix for suffix in ("", "_hfq") for f in ("open", "high", "low", "close")]]
    if not prices.map(lambda v: math.isfinite(float(v)) and float(v) > 0).all().all():
        raise ArtifactContractError(f"invalid paired OHLC: {code}")
    traded = joined["volume"].gt(0) & joined["amount"].gt(0)
    vwap = joined["amount"] / joined["volume"].where(traded)
    # A broad physical bound catches 100x hand/share or currency-unit errors
    # while allowing rounding, auctions and off-book trade differences.
    if (traded & (vwap.lt(joined["low"] / 3) | vwap.gt(joined["high"] * 3))).any():
        raise ArtifactContractError(f"trade amount/volume unit mismatch: {code}")
    # Keep the entire hfq series in one normalization vintage; today's close
    # stays in today's raw-price units, with a validated map for all OHLC.
    base = float(joined.iloc[-1]["close_hfq"] / joined.iloc[-1]["close"])
    for field in ("open", "high", "low", "close"):
        joined[f"provider_adj_{field}"] = joined[f"{field}_hfq"] / base
    spread = joined["high"] - joined["low"]
    multiplier = (joined["provider_adj_high"] - joined["provider_adj_low"]) / spread.where(spread.gt(0))
    # Flat bars cannot determine an affine map: the previous/next observed
    # non-flat bar is valid only when it maps this bar's adjusted price too.
    multiplier = multiplier.ffill().bfill()
    multiplier = multiplier.fillna(joined["provider_adj_close"] / joined["close"])
    offset = joined["provider_adj_close"] - multiplier * joined["close"]
    for field in ("open", "high", "low", "close"):
        residual = (joined[field] * multiplier + offset - joined[f"provider_adj_{field}"]).abs()
        # hfq source rounds independently. Mapping must agree within 1 raw tick
        # converted to the adjustment base, or 0.03 normalized price units.
        tolerance = pd.concat([multiplier.abs() * .011, pd.Series(.03, index=joined.index)], axis=1).max(axis=1)
        if residual.gt(tolerance).any():
            raise ArtifactContractError(f"inconsistent adjusted OHLC map: {code} {field}")
    joined["adj_multiplier"], joined["adj_offset"] = multiplier, offset
    joined["adj_factor"] = joined["provider_adj_close"] / joined["close"]
    if not joined["adj_multiplier"].gt(0).all():
        raise ArtifactContractError(f"invalid adjustment multiplier: {code}")
    joined["pre_close"] = joined["close"].shift(1)
    joined["change_ratio"] = (joined["close"] / joined["pre_close"] - 1) * 100
    joined["avg_price"] = joined["amount"] / joined["volume"].where(joined["volume"].gt(0))
    joined["pre_close_basis"] = "previous_observed_raw_close"
    if qt and len(qt) > 30 and str(qt[30])[:8] == sessions[-1].replace("-", ""):
        mask = joined["trade_date"].eq(sessions[-1])
        previous = float(qt[4])
        if previous > 0:
            joined.loc[mask, "pre_close"] = previous
            joined.loc[mask, "change_ratio"] = (joined.loc[mask, "close"] / previous - 1) * 100
            joined.loc[mask, "pre_close_basis"] = "Tencent_current_quote_previous_close"
    # Calendar membership is independent of price availability.
    joined = joined.loc[joined["trade_date"].isin(sessions)].copy()
    joined["thscode"] = code
    joined["known_at"] = observed_at
    joined["collection_completed_at"] = observed_at
    joined["base_date"] = sessions[-1]
    joined["adjustment_method"] = "Tencent_hfq_OHLC_affine_map_single_vintage"
    joined["source_provider"] = "akshare_tencent"
    joined["source_endpoint"] = "stock_zh_a_hist_tx/newfqkline/get_single_window"
    return joined.drop(columns=[f"{field}_hfq" for field in ("open", "high", "low", "close")]).reset_index(drop=True)


def _bounded_akshare():
    import akshare as ak
    import requests
    original = requests.sessions.Session.request
    if not getattr(original, "_akshare_bounded", False):
        def bounded(self, method, url, **kwargs):
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = 20
            return original(self, method, url, **kwargs)
        bounded._akshare_bounded = True
        requests.sessions.Session.request = bounded
    return ak


def bootstrap(data_dir: Path, cache: Path, end: str, session_count: int) -> dict:
    ak = _bounded_akshare()
    requested_end = end
    path = cache / "bootstrap.json"
    if path.exists():
        meta = load_json_object(path, missing_ok=False)
        if meta["requested_end"] == end and meta["session_count"] == session_count and meta.get("adapter_version") == ADAPTER_VERSION:
            for name, digest in meta["artifacts"].items():
                if sha256_file(cache / name) != digest:
                    raise ArtifactContractError("AKShare bootstrap hash mismatch")
            return meta
    print("AKShare bootstrap: exchange calendar and current stock identities", flush=True)
    calendar = ak.tool_trade_date_hist_sina()
    opens = sorted(set(pd.to_datetime(calendar["trade_date"]).dt.strftime("%Y-%m-%d")))
    if max(opens) < end:
        raise ArtifactContractError("AKShare calendar does not cover requested end")
    selected = [day for day in opens if day <= end][-session_count:]
    if len(selected) != session_count:
        raise ArtifactContractError("insufficient certified calendar sessions")
    end = selected[-1]
    # Exchange lists include listing dates; source name registry independently
    # checks combined current-universe completeness, including Beijing.
    listings = []
    errors = []
    sources = [("SSE_MAIN", lambda: ak.stock_info_sh_name_code(symbol="主板A股")),
               ("STAR", lambda: ak.stock_info_sh_name_code(symbol="科创板")),
               ("SZSE", lambda: ak.stock_info_sz_name_code(symbol="A股列表")),
               ("BSE", lambda: ak.stock_info_bj_name_code())]
    for label, call in sources:
        for attempt in range(2):
            try:
                df = call()
                code_col = next(c for c in ("证券代码", "A股代码") if c in df)
                name_col = next(c for c in ("证券简称", "A股简称") if c in df)
                listing_col = next((c for c in ("上市日期", "A股上市日期") if c in df), None)
                for row in df.to_dict("records"):
                    listings.append({"thscode": thscode(row[code_col]), "security_name": str(row[name_col]),
                        "listing_date": str(row[listing_col]) if listing_col else None})
                print(f"AKShare identity {label}: {len(df)}", flush=True)
                break
            except Exception as exc:
                if attempt:
                    errors.append({"module": label, "error_type": type(exc).__name__})
                time.sleep(1 + attempt)
    # Always verify all three exchanges through AKShare's aggregate registry.
    registry = ak.stock_info_a_code_name()
    universe = pd.DataFrame({"thscode": registry["code"].map(thscode), "security_name": registry["name"].astype(str)})
    if len(universe) < 5000 or universe["thscode"].duplicated().any() or set(universe["thscode"].str[-2:]) != {"SH", "SZ", "BJ"}:
        raise ArtifactContractError("incomplete AKShare current universe")
    listing = pd.DataFrame(listings).drop_duplicates("thscode", keep="last").set_index("thscode") if listings else pd.DataFrame()
    old_path = data_dir / "latest/security_reference.parquet"
    old = pd.read_parquet(old_path).set_index("thscode") if old_path.exists() else pd.DataFrame()
    refs = []
    for row in universe.to_dict("records"):
        code = row["thscode"]
        previous = old.loc[code] if code in old.index else pd.Series(dtype=object)
        official = listing.loc[code] if code in listing.index else pd.Series(dtype=object)
        value = official.get("listing_date") or previous.get("listing_date")
        if pd.isna(value):
            value = None
        refs.append({**row, "listing_date": value, "as_of_date": end,
            "total_shares": previous.get("total_shares"), "float_a_shares": previous.get("float_a_shares"),
            "share_count_reference_date": previous.get("as_of_date"),
            "listing_date_basis": "current_exchange_list" if official.get("listing_date") else "previous_verified_reference" if value else "unknown",
            "source_provider": "akshare_exchange_identity_with_prior_share_reference", "source_endpoint": "stock_info_a_code_name"})
    universe["as_of_date"] = end
    universe["security_name_in_time"] = universe["security_name"]
    # Full calendar includes explicit closed dates and an actual source interval.
    days = pd.date_range(selected[0], end).strftime("%Y-%m-%d")
    cal = pd.DataFrame({"as_of_date": end, "trade_date": days, "calendar": "SSE", "market_code": "212001",
        "is_open": [day in opens for day in days], "source_provider": "akshare_sina", "source_endpoint": "tool_trade_date_hist_sina",
        "calendar_window_start": selected[0], "calendar_window_end": end, "calendar_window_complete": True})
    frames = {"universe.parquet": universe, "reference.parquet": pd.DataFrame(refs), "calendar.parquet": cal}
    for name, frame in frames.items():
        atomic_write_parquet(cache / name, frame)
    meta = {"schema_version": 1, "adapter_version": ADAPTER_VERSION, "akshare_version": ak.__version__,
        "requested_end": requested_end, "session_count": session_count, "sessions": selected,
        "collection_completed_at": now(), "universe_count": len(universe), "identity_warnings": errors,
        "universe_basis": "current_listed_A_shares; not_historical_membership_or_survivorship_free_backtest",
        "artifacts": {name: sha256_file(cache / name) for name in frames}}
    atomic_write_json(path, meta)
    return meta


def collect(data_dir: Path, cache: Path, end: str, session_count: int, workers: int, limit: int | None = None) -> dict:
    meta = bootstrap(data_dir, cache, end, session_count)
    universe = pd.read_parquet(cache / "universe.parquet")
    codes = universe["thscode"].tolist()
    if limit:
        codes = codes[:limit]
    transport = TencentTransport()
    start = (pd.Timestamp(meta["sessions"][0]) - pd.Timedelta(days=14)).date().isoformat()
    failures, completed, reused = [], 0, 0

    def one(code):
        path = cache / "securities" / f"{code}.parquet"
        receipt_path = path.with_suffix(".json")
        previous = None
        previous_receipt = None
        if receipt_path.exists():
            previous_receipt = load_json_object(receipt_path, missing_ok=False)
            if sha256_file(path) != previous_receipt.get("sha256"):
                raise ArtifactContractError(f"cached raw checkpoint hash mismatch: {code}")
            if previous_receipt.get("bootstrap_sha256") == json_sha256(meta):
                return code, previous_receipt, True
            previous = pd.read_parquet(path)
        error = None
        for attempt in range(3):
            try:
                raw_reused = False
                if previous is not None and not previous.empty and str(previous["trade_date"].min()) <= meta["sessions"][0] and str(previous["trade_date"].max()) <= meta["sessions"][-1]:
                    raw = previous[["trade_date", "open", "close", "high", "low", "volume", "turnover_ratio", "amount"]].copy()
                    # v1 inherited the SDK's CDR unit exception; fix the prior
                    # normalized volume before reusing these raw observations.
                    if code.startswith("689") and previous_receipt.get("volume_multiplier", 100) == 100:
                        raw["volume"] /= 100
                    overlap = max(start, (pd.Timestamp(raw["trade_date"].max()) - pd.Timedelta(days=14)).date().isoformat())
                    fresh_raw, native = transport.fetch(code, overlap, meta["sessions"][-1], "")
                    raw = pd.concat([raw, fresh_raw], ignore_index=True).drop_duplicates("trade_date", keep="last").sort_values("trade_date").reset_index(drop=True)
                    raw_reused = True
                else:
                    raw, native = transport.fetch(code, start, meta["sessions"][-1], "")
                hfq, _ = transport.fetch(code, start, meta["sessions"][-1], "hfq")
                if raw_reused:
                    hfq = hfq.loc[hfq["trade_date"].ge(str(raw["trade_date"].min()))].reset_index(drop=True)
                    if set(raw["trade_date"]) != set(hfq["trade_date"]):
                        # Repair newly observed or revised historical dates via
                        # a full raw window instead of silently dropping them.
                        raw, native = transport.fetch(code, start, meta["sessions"][-1], "")
                        hfq, _ = transport.fetch(code, start, meta["sessions"][-1], "hfq")
                        raw_reused = False
                frame = normalize_pair(raw, hfq, code, meta["sessions"], now(), native.get("quote"))
                if frame.empty:
                    raise ArtifactContractError("no observations in selected window")
                atomic_write_parquet(path, frame)
                receipt = {"schema_version": 1, "bootstrap_sha256": json_sha256(meta), "thscode": code,
                    "rows": len(frame), "sha256": sha256_file(path), "known_at": str(frame["known_at"].max()),
                    "adapter_version": ADAPTER_VERSION, "raw_history_reused": raw_reused,
                    "previous_raw_checkpoint_sha256": previous_receipt.get("sha256") if raw_reused else None,
                    "volume_multiplier": 1 if code.startswith(("688", "689")) else 100,
                    "missing_session_dates": sorted(set(meta["sessions"]) - set(frame["trade_date"]))}
                atomic_write_json(receipt_path, receipt)
                return code, receipt, False
            except Exception as exc:
                error = {"thscode": code, "error_type": type(exc).__name__, "error": str(exc)[:160] if isinstance(exc, ArtifactContractError) else "source_request_failed"}
                time.sleep(min(2 ** attempt, 4))
        return code, error, False

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, code): code for code in codes}
        for future in as_completed(futures):
            code, receipt, was_cached = future.result()
            completed += 1
            reused += int(was_cached)
            if "sha256" not in receipt:
                failures.append(receipt)
            if completed % 25 == 0 or completed == len(codes):
                print(json.dumps({"phase": "AKShare history", "completed": completed, "total": len(codes),
                    "failures": len(failures), "reused": reused, "elapsed_seconds": round(time.monotonic()-started)}, ensure_ascii=False), flush=True)
                atomic_write_json(cache / "progress.json", {"completed": completed, "total": len(codes), "failures": failures, "updated_at": now()})
    result = {"schema_version": 1, "ok": not failures, "phase": "collected", "securities": len(codes),
              "session_count": session_count, "failures": failures, "completed_at": now(), "bootstrap": meta}
    atomic_write_json(cache / "collection_result.json", result)
    return result


def publish(data_dir: Path, cache: Path, result: dict) -> dict:
    """Verify all normalized checkpoints, publish history, then promote radar."""
    from .quality import (normalize_universe, normalize_security_reference, normalize_quotes,
                          normalize_trade_calendar, build_daily_security_status, build_market_summary,
                          validate_data, frame_hash, schema_signature)
    from .storage import promote_snapshot
    from .data_reference import build_data_reference_outputs, data_cutoff_time_for_date
    from .pipeline import validate_latest
    meta = result["bootstrap"]
    universe = normalize_universe(pd.read_parquet(cache / "universe.parquet"))
    if result["securities"] != len(universe) or result["failures"]:
        raise ArtifactContractError("all-market collection incomplete; refusing latest promotion")
    original_ref = pd.read_parquet(cache / "reference.parquet")
    reference = normalize_security_reference(original_ref, universe)
    reference = reference.merge(original_ref[["thscode", "listing_date_basis", "share_count_reference_date"]],
                                on="thscode", validate="one_to_one")
    calendar = normalize_trade_calendar(pd.read_parquet(cache / "calendar.parquet"))
    certified_open = set(calendar.loc[calendar["is_open"].eq(True), "trade_date"].astype(str))
    independently_observed_open = set()
    for cached_calendar in (data_dir / "facts/reference").glob("as_of_date=*/trading_calendar.parquet"):
        prior = pd.read_parquet(cached_calendar)
        if "source_provider" in prior:
            prior = prior.loc[prior["source_provider"].eq("ifind_http")]
        independently_observed_open.update(prior.loc[prior["is_open"].eq(True), "trade_date"].astype(str))
    independently_observed_open = {date for date in independently_observed_open if meta["sessions"][0] <= date <= meta["sessions"][-1]}
    if independently_observed_open - certified_open:
        raise ArtifactContractError("AKShare calendar conflicts with verified iFinD open-session observations")
    frames, checkpoint_hashes, coverage = [], {}, []
    raw_reused_count = 0
    for row in original_ref.to_dict("records"):
        code = row["thscode"]
        path = cache / "securities" / f"{code}.parquet"
        receipt = load_json_object(path.with_suffix(".json"), missing_ok=False)
        if receipt["bootstrap_sha256"] != json_sha256(meta) or sha256_file(path) != receipt["sha256"]:
            raise ArtifactContractError(f"normalized checkpoint hash mismatch: {code}")
        frame = pd.read_parquet(path)
        traded = frame["volume"].gt(0) & frame["amount"].gt(0)
        vwap = frame["amount"] / frame["volume"].where(traded)
        if (traded & (vwap.lt(frame["low"] / 3) | vwap.gt(frame["high"] * 3))).any():
            raise ArtifactContractError(f"checkpoint amount/volume unit mismatch: {code}")
        raw_reused_count += int(receipt.get("raw_history_reused", False))
        if frame["trade_date"].duplicated().any() or set(frame["trade_date"]) - set(meta["sessions"]):
            raise ArtifactContractError(f"checkpoint calendar mismatch: {code}")
        frames.append(frame)
        checkpoint_hashes[code] = receipt["sha256"]
        listing = row.get("listing_date")
        expected = [day for day in meta["sessions"] if pd.isna(listing) or day >= str(listing)]
        observed = set(frame["trade_date"])
        missing = sorted(set(expected) - observed)
        coverage.append({"thscode": code, "security_name": row["security_name"],
            "requested_sessions": len(meta["sessions"]), "observed_sessions": len(observed),
            "listing_date": None if pd.isna(listing) else str(listing), "expected_since_listing": len(expected),
            "pre_listing_sessions": len(meta["sessions"]) - len(expected),
            "missing_expected_sessions": missing,
            "missing_reason": "no_vendor_bar; suspension_or_source_gap_not_independently_confirmed" if missing else None})
    panel = pd.concat(frames, ignore_index=True)
    del frames
    if panel.duplicated(["thscode", "trade_date"]).any():
        raise ArtifactContractError("duplicate all-market AKShare observations")
    panel["calculated_vwap_from_rounded_inputs"] = panel["avg_price"]
    usable_average = panel["avg_price"].ge(panel["low"] - 1e-6) & panel["avg_price"].le(panel["high"] + 1e-6)
    panel["avg_price_basis"] = usable_average.map(lambda value: "calculated_from_rounded_amount_and_volume" if value else "unavailable_rounded_ratio_outside_price_range")
    panel["avg_price"] = panel["avg_price"].where(usable_average)
    raw = normalize_quotes(panel, universe)
    raw = raw.merge(panel[["thscode", "trade_date", "pre_close_basis", "calculated_vwap_from_rounded_inputs", "avg_price_basis"]], on=["thscode", "trade_date"], validate="one_to_one")
    factor_columns = ["thscode", "trade_date", "adj_factor", "adj_multiplier", "adj_offset", "known_at", "base_date",
        "collection_completed_at", "adjustment_method", "source_provider", "source_endpoint",
        "provider_adj_open", "provider_adj_high", "provider_adj_low", "provider_adj_close"]
    factors = panel[factor_columns].copy()
    completed = str(panel["known_at"].max())
    day = meta["sessions"][-1]
    # A durable vintage is keyed by the identity and every verified checkpoint.
    vintage = json_sha256({"adapter": ADAPTER_VERSION, "bootstrap": json_sha256(meta), "checkpoints": checkpoint_hashes})
    history_dir = data_dir / "facts/akshare/market_history" / f"vintage={vintage}"
    partitions = []
    for date, frame in raw.groupby("trade_date", sort=True):
        path = history_dir / f"trade_date={date}" / "daily_quotes.parquet"
        atomic_write_parquet(path, frame)
        receipt = {"schema_version": 3, "trade_date": date, "verified": True, "data_fresh": True,
            "provider": "akshare_tencent", "collection_completed_at": completed,
            "collection_started_at": meta["collection_completed_at"],
            "history_basis": meta["universe_basis"],
            "artifacts": {path.name: {"sha256": sha256_file(path), "rows": len(frame)}}}
        atomic_write_json(path.parent / "market_receipt.json", receipt)
        partitions.append({"trade_date": date, "path": path.relative_to(data_dir).as_posix(), "rows": len(frame),
                           "sha256": receipt["artifacts"][path.name]["sha256"]})
    if len(partitions) != len(meta["sessions"]):
        raise ArtifactContractError("missing full-market date partition")
    observed_date = pd.Timestamp(completed).tz_convert("Asia/Shanghai").date().isoformat()
    factor_parts = []
    for prefix, group in factors.groupby(factors["thscode"].str[:2], sort=True):
        factor_path = data_dir / "facts/adjustment" / f"as_of_date={observed_date}" / f"vintage={vintage}-{prefix}" / "adjustment_factors.parquet"
        atomic_write_parquet(factor_path, group)
        factor_parts.append({"path": factor_path.relative_to(data_dir).as_posix(), "sha256": sha256_file(factor_path), "rows": len(group)})
    coverage_frame = pd.DataFrame(coverage)
    coverage_path = history_dir / "history_coverage.parquet"
    atomic_write_parquet(coverage_path, coverage_frame)
    report = {"schema_version": 1, "provider": "akshare_tencent", "akshare_version": meta["akshare_version"],
        "adapter_version": ADAPTER_VERSION, "vintage": vintage, "trade_date": day,
        "collection_completed_at": completed, "window_start": meta["sessions"][0], "window_end": day,
        "certified_sessions": len(meta["sessions"]), "universe_count": len(universe), "quote_rows": len(raw), "raw_history_reused_securities": raw_reused_count,
        "unavailable_derived_average_price_rows": int((~usable_average).sum()),
        "full_300_session_securities": int(coverage_frame["observed_sessions"].eq(len(meta["sessions"])).sum()),
        "new_listing_short_history_count": int(coverage_frame["pre_listing_sessions"].gt(0).sum()),
        "securities_with_unresolved_bar_gaps": int(coverage_frame["missing_expected_sessions"].map(len).gt(0).sum()),
        "unresolved_bar_gap_count": int(coverage_frame["missing_expected_sessions"].map(len).sum()),
        "unexpected_request_failures": result["failures"], "universe_basis": meta["universe_basis"],
        "adjustment_basis": "native_Tencent_hfq_OHLC; one_normalization_vintage; validated_affine_inverse; actual_known_at",
        "pre_close_basis": "native_Tencent_previous_close_for_latest_date; previous_observed_raw_close_for_history",
        "share_counts_basis": "prior_verified_iFinD_reference; per_security_reference_date_preserved",
        "calendar": {"provider": "akshare_sina", "explicit_open_and_closed_dates": True, "independent_iFinD_open_sessions_checked": len(independently_observed_open), "conflicts": 0},
        "coverage_path": coverage_path.relative_to(data_dir).as_posix(),
        "coverage_sha256": sha256_file(coverage_path),
        "adjustment_parts": factor_parts,
        "partitions": partitions}
    atomic_write_json(history_dir / "history_manifest.json", report)
    # Current names identify ST and delisting period; a positive-volume trade
    # establishes not suspended for this completed daily bar. Trading limits
    # and lot sizes remain unknown until separately verified.
    current = raw.loc[raw["trade_date"].eq(day)].copy()
    by_code = current.set_index("thscode")
    statuses = []
    for record in universe.to_dict("records"):
        code, name = record["thscode"], str(record["security_name"])
        quote = by_code.loc[code] if code in by_code.index else pd.Series(dtype=object)
        traded = not quote.empty and float(quote["volume"]) > 0 and float(quote["amount"]) > 0
        statuses.append({"as_of_date": day, "thscode": code, "is_st": name.upper().startswith(("ST", "*ST", "S*ST")),
            "is_delisting": "退" in name, "is_suspended": False if traded else None,
            "daily_price_limit_pct": None, "lot_size": None, "known_at": completed, "collection_completed_at": completed,
            "source_provider": "akshare_current_names_and_observed_trade", "status_basis": "name_classification_and_positive_completed_daily_trade; execution_rules_unknown"})
    tradability_path = data_dir / "facts/tradability" / f"as_of_date={day}" / f"vintage={vintage}" / "provider_tradability.parquet"
    atomic_write_parquet(tradability_path, pd.DataFrame(statuses))
    daily_status = build_daily_security_status(universe, current, day)
    quality = validate_data(universe, reference, current, calendar, daily_status, day)
    if not quality.ok:
        atomic_write_json(data_dir / "akshare_backfill_status.json", {"ok": False, "history": report, "quality": quality.as_dict()})
        raise ArtifactContractError("AKShare current snapshot failed normal quality gate")
    summary = build_market_summary(current, day, daily_status)
    configured = data_cutoff_time_for_date(day)
    effective = min(pd.Timestamp(configured), pd.Timestamp(completed)).isoformat()
    source_relative = Path("facts/akshare/snapshots") / f"vintage={vintage}" / f"trade_date={day}"
    manifest = {"source_snapshot_path": source_relative.as_posix(), "schema_version": 3, "trade_date": day, "verified": True, "data_fresh": True,
        "provider": "akshare_tencent", "collected_at_utc": completed,
        "collection_started_at": meta["collection_completed_at"], "collection_completed_at": completed,
        "configured_decision_cutoff": configured, "effective_pit_cutoff": effective,
        "quality": quality.as_dict(), "quality_thresholds": {"min_universe_size": 5000, "min_quote_coverage": .98,
            "min_reference_coverage": .98, "min_extended_field_coverage": .95},
        "provenance": {key: value for name, frame, keys in (
            ("universe", universe, ["thscode"]), ("security_reference", reference, ["thscode"]),
            ("quotes", current, ["trade_date", "thscode"]), ("trading_calendar", calendar, ["calendar", "trade_date"]),
            ("daily_status", daily_status, ["trade_date", "thscode"]))
            for key, value in ((name + "_frame_sha256", frame_hash(frame, keys)),
                               (name + "_schema_signature", schema_signature(frame)))},
        "akshare_history": {"path": (history_dir / "history_manifest.json").relative_to(data_dir).as_posix(),
            "sha256": sha256_file(history_dir / "history_manifest.json"), "current_universe_count": len(universe)},
        "raw_payload_persisted": False}
    # Keep the previous latest until derived outputs have also passed their
    # contract. Historical source snapshots remain separately versioned.
    import shutil
    import tempfile
    source_store = data_dir / source_relative
    # Preserve the original iFinD snapshot and canonical facts for this date.
    # Generate AKShare's market snapshot in isolation and retain its own source
    # directory, selected by an explicit manifest reference.
    with tempfile.TemporaryDirectory(prefix=".akshare-source-", dir=cache) as seed_dir:
        seed = Path(seed_dir)
        persisted_manifest = promote_snapshot(seed, day, universe, reference, current, calendar, daily_status,
                                              summary, manifest, promote_latest=False)
        seed_snapshot = seed / "snapshots" / day
        for name, filename, keys in (
            ("universe", "universe.parquet", ["thscode"]),
            ("security_reference", "security_reference.parquet", ["thscode"]),
            ("quotes", "daily_quotes.parquet", ["trade_date", "thscode"]),
            ("trading_calendar", "trading_calendar.parquet", ["calendar", "trade_date"]),
            ("daily_status", "daily_security_status.parquet", ["trade_date", "thscode"])):
            stored = pd.read_parquet(seed_snapshot / filename)
            persisted_manifest["provenance"][name + "_schema_signature"] = schema_signature(stored)
            persisted_manifest["provenance"][name + "_frame_sha256"] = frame_hash(stored, keys)
        atomic_write_json(seed_snapshot / "manifest.json", persisted_manifest)
        shutil.copytree(seed_snapshot, source_store, dirs_exist_ok=True)
    atomic_write_parquet(data_dir / "facts/akshare/reference" / f"as_of_date={day}" /
                         f"vintage={vintage}" / "trading_calendar.parquet", calendar)
    # Release the assembly panels before the feature builder loads its own
    # 300-session panel and normalization vintages.
    del panel, raw, factors, coverage_frame
    # Build the current research view in isolation. This allows a late-observed
    # backfill to produce signals without backdating knowledge or publishing a
    # half-built latest if a derived contract fails.
    import shutil
    import tempfile
    from .storage import promote_verified_snapshot_to_latest, _persist_technical_artifacts
    research_cutoff = now()
    with tempfile.TemporaryDirectory(prefix=".akshare-publish-", dir=data_dir) as temporary:
        staging = Path(temporary)
        shutil.copytree(source_store, staging / "snapshots" / day)
        (staging / "facts").symlink_to((data_dir / "facts").resolve(), target_is_directory=True)
        previous_signals = data_dir / "features/technical_signals"
        if previous_signals.exists():
            shutil.copytree(previous_signals, staging / "features/technical_signals")
        promote_verified_snapshot_to_latest(staging, day)
        derived = build_data_reference_outputs(staging, day, research_asof=research_cutoff)
        ok, validation = validate_latest(data_dir=staging)
        if not ok:
            raise ArtifactContractError("isolated AKShare derived publication failed validation: " + json.dumps(validation.get("errors", []), ensure_ascii=False))
        final_manifest = load_manifest(staging / "latest/manifest.json", missing_ok=False)
        _persist_technical_artifacts(data_dir, day, {
            "technical_scan.parquet": pd.read_parquet(staging / "latest/technical_scan.parquet"),
            "technical_signals_latest.json": load_json_object(staging / "latest/technical_signals_latest.json", missing_ok=False),
        }, final_manifest, current_research=True)
        promote_verified_snapshot_to_latest(data_dir, day, source_dir=staging / "latest")
    ok, validation = validate_latest(data_dir=data_dir)
    final = {"ok": ok, "phase": "published", "trade_date": day, "history": report,
             "technical_signals": derived["technical_signals"], "validation": validation,
             "research_asof": research_cutoff, "completed_at": now()}
    atomic_write_json(data_dir / "akshare_backfill_status.json", final)
    from .storage import write_run_status
    write_run_status(data_dir, {"schema_version": 2, "state": "success_akshare_backfill" if ok else "failed_akshare_validation",
        "data_fresh": ok, "provider": "akshare_tencent", "last_valid_trade_date": day,
        "completed_at_utc": now(), "certified_sessions": len(meta["sessions"]),
        "raw_payload_persisted": False})
    return final


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".private-stock-data/akshare-backfill"))
    parser.add_argument("--end", required=True)
    parser.add_argument("--sessions", type=int, default=300)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, help="read-only normalized canary count; never publish a partial universe")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--publish-only", action="store_true", help="verify and publish completed normalized checkpoints without API calls")
    parser.add_argument("--probe-code", action="append", help="small paired OHLC canary; no batch or latest changes")
    args = parser.parse_args(argv)
    if not 61 <= args.sessions <= 300 or not 1 <= args.workers <= 4:
        parser.error("sessions must be 61..300; workers must be 1..4")
    if args.publish and args.limit:
        parser.error("partial canary cannot publish")
    if args.probe_code:
        meta = bootstrap(args.data_dir, args.cache_dir, args.end, args.sessions)
        transport = TencentTransport()
        start = (pd.Timestamp(meta["sessions"][0]) - pd.Timedelta(days=14)).date().isoformat()
        for code in args.probe_code:
            raw, native = transport.fetch(code, start, meta["sessions"][-1], "")
            hfq, _ = transport.fetch(code, start, meta["sessions"][-1], "hfq")
            frame = normalize_pair(raw, hfq, code, meta["sessions"], now(), native.get("quote"))
            print(json.dumps({"probe": code, "rows": len(frame), "latest_date": str(frame["trade_date"].max()),
                "latest": {key: float(frame.iloc[-1][key]) for key in ("close", "volume", "amount", "pre_close")}}, ensure_ascii=False), flush=True)
        return 0
    result = load_json_object(args.cache_dir / "collection_result.json", missing_ok=False) if args.publish_only else collect(args.data_dir, args.cache_dir, args.end, args.sessions, args.workers, args.limit)
    if args.publish_only and (result["bootstrap"].get("requested_end") != args.end or result["bootstrap"].get("session_count") != args.sessions):
        raise ArtifactContractError("completed batch does not match requested publication window")
    if args.publish or args.publish_only:
        result = publish(args.data_dir, args.cache_dir, result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
