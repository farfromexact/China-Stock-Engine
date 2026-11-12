from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from china_stock_engine.storage import atomic_write_json, json_sha256, read_parquet_file
from china_stock_engine.wind_collect import collect_candidate
from china_stock_engine.wind_incremental import (AUDIT_BUCKETS, IncrementalWind,
    audit_bucket, collect_incremental, load_seed, missing_runs)
from china_stock_engine.wind_provider import WindProfile, WindProvider, WindError, adjustment_pair
from china_stock_engine.wind_publish import publish, inventory
from china_stock_engine.pipeline import validate_latest
from test_wind import MatrixFakeWind, CODES


DAYS = pd.bdate_range("2025-08-01", periods=301).strftime("%Y-%m-%d").tolist()
PROFILE = WindProfile(min_interval=0, units_verified=True)


class MutableWind(MatrixFakeWind):
    def __init__(self):
        super().__init__(DAYS)
        self.requests, self.changes, self.listings, self.factors = [], {}, {}, {}

    def __call__(self, op, *args):
        self.requests.append((op, args))
        result = super().__call__(op, *args)
        if op == "tdays":
            result["data"][0] = [day for day in self.days if args[0] <= day <= args[1]]
        if op == "wss":
            result["data"][0] = [self.listings.get(c, "2000-01-01") for c in result["codes"]]
        if op == "wsd":
            adjusted = "PriceAdj=B" in args[-1]
            # Incremental queries use exactly one field and one/many code matrices.
            self.assert_one_field(result)
            field = result["fields"][0]
            for index, code in enumerate(result["codes"]):
                for time_index, day in enumerate(result["times"]):
                    value = result["data"][index][time_index]
                    if adjusted and value is not None:
                        value = value / 2
                    value = self.changes.get((code, day, field), value)
                    if day < self.listings.get(code, "2000-01-01"):
                        value = None
                    if adjusted and value is not None:
                        value *= self.factors.get(code, 2)
                    result["data"][index][time_index] = value
            if len(result["times"]) == 1 and len(result["codes"]) > 1:
                result["data"] = [[column[0] for column in result["data"]]]
        return result

    @staticmethod
    def assert_one_field(result):
        if len(result["fields"]) != 1:
            raise AssertionError("fixture expects the production many-code/one-field protocol")


def make_seed():
    native = MutableWind()
    provider = WindProvider(native, PROFILE)
    raw = provider.history_many(CODES, DAYS[0], DAYS[-2])
    adj = provider.history_many(CODES, DAYS[0], DAYS[-2], adjusted=True)
    factors = {c: adjustment_pair(raw[c], adj[c]) for c in CODES}
    empty_bucket = next(i for i in range(AUDIT_BUCKETS) if i not in {audit_bucket(c) for c in CODES})
    return {"vintage": "b" * 24, "raw": raw, "factors": factors,
            "receipt": {"incremental": {"next_audit_bucket": empty_bucket}}}


class IncrementalPlanningTests(unittest.TestCase):
    def setUp(self):
        self.native = MutableWind()
        self.provider = WindProvider(self.native, PROFILE)
        self.seed = make_seed()
        self.adapter = IncrementalWind(self.provider, self.seed)
        self.adapter.fetch_trade_calendar(DAYS[-1], offset=-330)
        self.adapter.fetch_security_reference(CODES, DAYS[-1])

    def collect(self):
        raw = self.adapter.history_many(CODES, DAYS[1], DAYS[-1])
        adj = self.adapter.history_many(CODES, DAYS[1], DAYS[-1], adjusted=True)
        for code in CODES:
            adjustment_pair(raw[code], adj[code])
        return raw, adj

    def test_normal_day_downloads_only_one_new_complete_bar_per_stock(self):
        raw, _ = self.collect()
        self.assertTrue(all(len(frame) == 300 for frame in raw.values()))
        status = self.adapter.incremental_status
        self.assertEqual(status["raw_rows_downloaded_this_attempt"], 3)
        self.assertEqual(status["reused_rows"], 3 * 299)
        self.assertFalse(status["repairs"])
        full_non_close = [args for op, args in self.native.requests
                          if op == "wsd" and args[1] != "close" and args[2] < DAYS[-1]]
        self.assertEqual(full_non_close, [])
        self.assertEqual(set(raw[CODES[0]].trade_date), set(DAYS[1:]))

    def test_disjoint_holes_fetch_only_missing_certified_ranges(self):
        missing = {DAYS[50], DAYS[51], DAYS[170]}
        for name in ("raw", "factors"):
            frame = self.seed[name][CODES[0]]
            self.seed[name][CODES[0]] = frame.loc[~frame.trade_date.isin(missing)].copy()
        self.collect()
        requests = [args for op, args in self.native.requests if op == "wsd" and args[1] == "open"
                    and "PriceAdj=U" in args[-1]]
        self.assertEqual({(a[2], a[3]) for a in requests},
                         {(DAYS[50], DAYS[51]), (DAYS[170], DAYS[170]), (DAYS[-1], DAYS[-1])})
        self.assertEqual(self.adapter.incremental_status["gap_rows_downloaded"], 6)

    def test_dividend_or_rebasing_refreshes_only_affected_stock_native_ohlc(self):
        self.native.factors[CODES[0]] = 3
        raw, adjusted = self.collect()
        factors = adjustment_pair(raw[CODES[0]], adjusted[CODES[0]])
        self.assertTrue(factors.adj_factor.eq(3).all())
        status = self.adapter.incremental_status
        self.assertEqual(set(status["repairs"]), {CODES[0]})
        self.assertIn("adjustment_basis_revised", status["repairs"][CODES[0]])
        self.assertTrue(factors.known_at.gt("2026-01-01").all())

    def test_old_close_revision_is_detected_outside_audit_bucket(self):
        self.native.changes[(CODES[1], DAYS[25], "close")] = 10.7
        raw, _ = self.collect()
        self.assertEqual(raw[CODES[1]].set_index("trade_date").loc[DAYS[25], "close"], 10.7)
        self.assertEqual(set(self.adapter.incremental_status["repairs"]), {CODES[1]})

    def test_non_close_revision_is_detected_by_rotating_full_field_audit(self):
        self.adapter.cursor = audit_bucket(CODES[0])
        self.native.changes[(CODES[0], DAYS[15], "volume")] = 2000
        self.native.changes[(CODES[0], DAYS[15], "amt")] = 20400
        raw, _ = self.collect()
        self.assertEqual(raw[CODES[0]].set_index("trade_date").loc[DAYS[15], "volume"], 2000)
        self.assertIn("historical_full_fields_revised", self.adapter.incremental_status["repairs"][CODES[0]])

    def test_non_audited_non_close_revision_has_explicit_bounded_detection(self):
        self.native.changes[(CODES[0], DAYS[15], "volume")] = 2000
        raw, _ = self.collect()
        self.assertEqual(raw[CODES[0]].set_index("trade_date").loc[DAYS[15], "volume"], 1000)
        self.assertFalse(self.adapter.incremental_status["repairs"])
        self.assertIn(audit_bucket(CODES[0]), {(self.adapter.cursor + i) % AUDIT_BUCKETS for i in range(AUDIT_BUCKETS)})

    def test_new_ipo_fetch_starts_at_listing_not_300_days_ago(self):
        for name in ("raw", "factors"):
            self.seed[name].pop(CODES[2])
        self.native.listings[CODES[2]] = DAYS[-3]
        self.adapter.fetch_security_reference(CODES, DAYS[-1])
        raw, _ = self.collect()
        self.assertEqual(raw[CODES[2]].trade_date.tolist(), DAYS[-3:])
        requests = [a for op, a in self.native.requests if op == "wsd" and CODES[2] in a[0].split(",")
                    and a[1] != "close"]
        self.assertTrue(all(a[2] >= DAYS[-3] for a in requests))

    def test_vendor_blank_is_not_forward_filled_or_declared_suspended(self):
        self.native.changes[(CODES[0], DAYS[-1], "close")] = None
        raw, _ = self.collect()
        self.assertNotIn(DAYS[-1], set(raw[CODES[0]].trade_date))

    def test_removed_historical_observation_is_not_kept_as_a_fake_bar(self):
        self.native.changes[(CODES[0], DAYS[30], "close")] = None
        raw, _ = self.collect()
        self.assertNotIn(DAYS[30], set(raw[CODES[0]].trade_date))
        self.assertIn("historical_observation_removed", self.adapter.incremental_status["repairs"][CODES[0]])

    def test_invalid_factor_basis_forces_native_repair(self):
        self.seed["factors"][CODES[0]].loc[50, "adj_factor"] = float("nan")
        self.collect()
        self.assertIn("missing_or_invalid_adjustment_basis", self.adapter.incremental_status["repairs"][CODES[0]])

    def test_mismatched_adjusted_ohlc_fails_closed(self):
        self.native.factors[CODES[0]] = 3
        original = self.provider.history_many
        def bad(codes, start, end, *, adjusted=False):
            result = original(codes, start, end, adjusted=adjusted)
            if adjusted and start == DAYS[1] and CODES[0] in result:
                result[CODES[0]].loc[0, "high"] += 10
            return result
        with patch.object(self.provider, "history_many", side_effect=bad):
            with self.assertRaises(WindError):
                self.collect()

    def test_audit_checks_native_adjusted_high_even_when_close_ratio_is_unchanged(self):
        self.adapter.cursor = audit_bucket(CODES[0])
        original = self.provider.history_many
        def incompatible(codes, start, end, *, adjusted=False):
            result = original(codes, start, end, adjusted=adjusted)
            if adjusted and start == DAYS[1] and CODES[0] in result:
                result[CODES[0]].loc[0, "high"] += 10
            return result
        with patch.object(self.provider, "history_many", side_effect=incompatible):
            with self.assertRaises(WindError):
                self.collect()

    def test_concurrent_vendor_revision_between_probe_and_fetch_aborts(self):
        original = self.provider.history_many
        def changed(codes, start, end, *, adjusted=False):
            result = original(codes, start, end, adjusted=adjusted)
            if not adjusted and CODES[0] in result:
                result[CODES[0]].loc[:, "close"] = 10.8
            return result
        with patch.object(self.provider, "history_many", side_effect=changed):
            with self.assertRaises(WindError):
                self.collect()

    def test_range_planner_does_not_merge_across_cached_trading_sessions(self):
        self.assertEqual(missing_runs(DAYS, {DAYS[3], DAYS[4], DAYS[7]}),
                         [[DAYS[3], DAYS[4]], [DAYS[7], DAYS[7]]])


class IncrementalIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        baseline = collect_candidate(WindProvider(MutableWind(), PROFILE), cls.root / "baseline",
            end=DAYS[-2], sessions=300, min_universe=3, batch_size=3)
        cls.candidate = Path(baseline["candidate"])
        cls.baseline_status = baseline

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def initial_production(self, root):
        data = root / "data"
        def small(*, data_dir):
            return validate_latest(data_dir=data_dir, policy_min_universe_size=3)
        with patch("china_stock_engine.wind_publish.verify_candidate", return_value=self.baseline_status), \
             patch("china_stock_engine.wind_publish.validate_latest", side_effect=small):
            publish(self.candidate, data)
        return data

    def test_real_candidate_reuses_baseline_resumes_checkpoints_and_keeps_300_days(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = self.initial_production(root)
            before = inventory(data)
            seed = load_seed(data, json_sha256(asdict(PROFILE)))
            self.assertEqual(len(seed["raw"]), 3)
            native = MutableWind()
            workspace = root / "work"
            first = collect_incremental(WindProvider(native, PROFILE), workspace, data_dir=data,
                end=DAYS[-1], batch_size=3, min_universe=3)
            native.requests.clear()
            second = collect_incremental(WindProvider(native, PROFILE), workspace, data_dir=data,
                end=DAYS[-1], batch_size=3, min_universe=3)
            self.assertFalse(any(op == "wsd" for op, _ in native.requests))
            self.assertEqual(second["incremental"]["checkpoint_reused_codes"], 3)
            self.assertEqual(first["rows"], 900)
            self.assertEqual(first["start"], DAYS[1])
            self.assertEqual(inventory(data), before)
            candidate = Path(first["candidate"])
            partitions = sorted((candidate / "facts/market").glob("trade_date=*"))
            self.assertEqual(len(partitions), 300)
            self.assertEqual(partitions[0].name, "trade_date=" + DAYS[1])
            self.assertTrue((candidate / "latest/technical_scan.parquet").exists())
            def small(*, data_dir):
                return validate_latest(data_dir=data_dir, policy_min_universe_size=3)
            with patch("china_stock_engine.wind_publish.verify_candidate", return_value=first), \
                 patch("china_stock_engine.wind_publish.validate_latest", side_effect=small):
                receipt = publish(candidate, data, rolling=True)
            self.assertEqual(receipt["incremental"]["source_vintage"], seed["vintage"])
            published = inventory(data)
            with patch("china_stock_engine.wind_publish.verify_candidate", return_value=second):
                with self.assertRaises(ValueError):
                    publish(Path(second["candidate"]), data, rolling=True)
            self.assertEqual(inventory(data), published)

    def test_corrupt_partition_becomes_a_gap_and_profile_change_cannot_mix_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            data = self.initial_production(Path(temp))
            manifest = json.loads((data / "latest/manifest.json").read_text())
            market = data / "facts/wind" / ("vintage=" + manifest["wind_vintage"]) / "market"
            (market / ("trade_date=" + DAYS[50]) / "daily_quotes.parquet").write_bytes(b"corrupt")
            seed = load_seed(data, json_sha256(asdict(PROFILE)))
            self.assertNotIn(DAYS[50], set(seed["raw"][CODES[0]].trade_date))
            adapter = IncrementalWind(WindProvider(MutableWind(), PROFILE), seed)
            adapter.cursor = next(i for i in range(AUDIT_BUCKETS) if i not in {audit_bucket(c) for c in CODES})
            adapter.fetch_trade_calendar(DAYS[-1], offset=-330)
            adapter.fetch_security_reference(CODES, DAYS[-1])
            adapter.history_many(CODES, DAYS[1], DAYS[-1])
            self.assertFalse(adapter.incremental_status["repairs"])
            self.assertEqual(adapter.incremental_status["gap_rows_downloaded"], 6)
            with self.assertRaises(WindError):
                load_seed(data, "different-profile")
