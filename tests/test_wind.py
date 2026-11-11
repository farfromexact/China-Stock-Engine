from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

import pandas as pd

from china_stock_engine.wind_provider import WindProvider, WindProfile, WindError, columns, adjustment_pair
from china_stock_engine.wind_session import WindSession, WindBusyError, collector_lock
from china_stock_engine.wind_collect import collect_candidate, doctor


CODES = ["600000.SH", "000001.SZ", "920001.BJ"]
DAYS = pd.bdate_range("2026-01-05", periods=5).strftime("%Y-%m-%d").tolist()


class FakeWind:
    def __init__(self, days=None):
        self.calls = []
        self.days = days or DAYS

    def __call__(self, op, *args):
        self.calls.append((op, args))
        if op == "wset":
            return dict(fields=["wind_code", "sec_name"], codes=[], times=[], data=[CODES, ["A", "B", "C"]])
        if op == "tdays":
            return dict(fields=["date"], codes=[], times=[], data=[self.days])
        if op == "wss":
            codes = args[0].split(",")
            return dict(fields=args[1].split(","), codes=codes, times=[],
                        data=[["2000-01-01"] * len(codes), [1e8] * len(codes), [8e7] * len(codes)])
        if op == "wsd":
            code, fields, start, end, options = args
            dates = [d for d in self.days if start <= d <= end]
            values = {"open": 10., "high": 11., "low": 9., "close": 10.5,
                      "pre_close": 10.5 / 1.01, "vwap": 10.2, "volume": 1000.,
                      "amt": 10200., "turn": 1.5, "pct_chg": 1.}
            factor = 2 if "PriceAdj=B" in options else 1
            return dict(fields=fields.split(","), codes=[code], times=dates,
                        data=[[values[f] * factor] * len(dates) for f in fields.split(",")])
        raise AssertionError(op)


def provider(fake=None):
    return WindProvider(fake or FakeWind(), WindProfile(min_interval=0))


class MatrixFakeWind(FakeWind):
    def __call__(self, op, *args):
        if op != "wsd" or "," not in args[0]:
            return super().__call__(op, *args)
        codes = args[0].split(",")
        columns = [super(MatrixFakeWind, self).__call__(op, code, *args[1:]) for code in codes]
        return dict(fields=columns[0]["fields"], codes=codes, times=columns[0]["times"],
                    data=[item["data"][0] for item in columns])


class WindAdapterTests(unittest.TestCase):
    def test_many_code_one_field_matrices_match_single_code_history(self):
        client = provider(MatrixFakeWind())
        for adjusted in [False, True]:
            many = client.history_many(CODES, DAYS[0], DAYS[-1], adjusted=adjusted)
            for code in CODES:
                pd.testing.assert_frame_equal(many[code], client.history(code, DAYS[0], DAYS[-1], adjusted=adjusted))

    def test_matrix_rejects_reordered_time_axis_and_unexpected_codes(self):
        fake = MatrixFakeWind()
        for error in ["times", "codes"]:
            def request(op, *args):
                result = fake(op, *args)
                if op == "wsd" and args[1] == "high":
                    if error == "times": result["times"] = list(reversed(result["times"]))
                    else: result["codes"] = ["999999.SH"] + result["codes"][1:]
                return result
            with self.subTest(error=error), self.assertRaises(WindError):
                provider(request).history_many(CODES, DAYS[0], DAYS[-1])
    def test_stock_calendar_units_and_reference_axes(self):
        client = provider()
        self.assertEqual(client.fetch_universe(DAYS[-1]).thscode.tolist(), CODES)
        calendar = client.fetch_trade_calendar(DAYS[-1], offset=-5)
        self.assertEqual(calendar.trade_date.tolist(), DAYS)
        self.assertTrue(calendar.calendar_window_complete.all())
        reference = client.fetch_security_reference(CODES, DAYS[-1])
        self.assertEqual(reference.total_shares.tolist(), [1e8] * 3)
        history = client.history(CODES[0], DAYS[0], DAYS[-1])
        self.assertEqual(history.volume.tolist(), [1000.] * 5)
        self.assertEqual(history.source_provider.unique().tolist(), ["windpy"])

    def test_adjusted_ohlc_pair_and_observation_cannot_be_backdated(self):
        client = provider()
        raw = client.history(CODES[0], DAYS[0], DAYS[-1])
        adjusted = client.history(CODES[0], DAYS[0], DAYS[-1], adjusted=True)
        factors = adjustment_pair(raw, adjusted, known_at="2000-01-01T00:00:00Z")
        self.assertEqual(factors.adj_factor.tolist(), [2.] * 5)
        self.assertTrue(factors.known_at.gt("2026-01-01").all())
        adjusted.loc[0, "high"] += 5
        with self.assertRaises(WindError):
            adjustment_pair(raw, adjusted)

    def test_adjustments_reject_duplicate_and_missing_dates(self):
        client = provider()
        raw = client.history(CODES[0], DAYS[0], DAYS[-1])
        adjusted = client.history(CODES[0], DAYS[0], DAYS[-1], adjusted=True)
        for bad in [adjusted.iloc[1:], pd.concat([adjusted, adjusted.iloc[:1]])]:
            with self.assertRaises(WindError):
                adjustment_pair(raw, bad)

    def test_shapes_and_bad_profiles_fail_closed(self):
        for result in [dict(fields=["close"], data=[[1], [2]]),
                       dict(fields=["close", "open"], data=[[1], [2, 3]])]:
            with self.assertRaises(WindError):
                columns(result)
        for kwargs in [{"volume_multiplier": 0}, {"amount_multiplier": float("nan")},
                       {"raw_options": "Fill=Previous"}]:
            with self.assertRaises(ValueError):
                WindProfile(**kwargs)

    def test_wrong_units_and_ohlc_are_rejected(self):
        for field, value in [("amt", 10200_000.), ("high", 2.)]:
            fake = FakeWind()
            def request(op, *args):
                r = fake(op, *args)
                if op == "wsd":
                    r["data"][r["fields"].index(field)] = [value] * len(r["times"])
                return r
            with self.subTest(field=field), self.assertRaises(WindError):
                provider(request).history(CODES[0], DAYS[0], DAYS[-1])

    def test_blank_prices_are_not_forward_filled(self):
        fake = FakeWind()
        def request(op, *args):
            r = fake(op, *args)
            if op == "wsd":
                r["data"][r["fields"].index("close")][0] = None
            return r
        history = provider(request).history(CODES[0], DAYS[0], DAYS[-1])
        self.assertEqual(history.trade_date.tolist(), DAYS[1:])


class WindIsolationTests(unittest.TestCase):
    def test_login_error_preserves_error_code_without_sdk_account_data(self):
        from china_stock_engine.wind_session import _worker
        connection = MagicMock()
        response = SimpleNamespace(ErrorCode=-40520007, Data=["private-account-detail"])
        native = SimpleNamespace(w=SimpleNamespace(start=MagicMock(return_value=response)))
        with patch("china_stock_engine.wind_session.importlib.import_module", return_value=native):
            _worker(connection)
        error = connection.send.call_args.args[0]["error"]
        self.assertIn("-40520007", error)
        self.assertNotIn("private-account", error)
        connection.close.assert_called_once()
    def test_running_dashboard_blocks_before_process_or_sdk_import(self):
        with patch("china_stock_engine.wind_session.dashboard_running", return_value=True), \
             patch("china_stock_engine.wind_session.mp.get_context") as spawn:
            with self.assertRaises(WindBusyError):
                with WindSession(exclusive_window=True):
                    self.fail("must not start")
            spawn.assert_not_called()

    def test_implicit_session_is_always_rejected(self):
        with patch("china_stock_engine.wind_session.mp.get_context") as spawn:
            with self.assertRaises(WindBusyError):
                with WindSession():
                    self.fail("must not start")
            spawn.assert_not_called()

    def test_collector_lock_rejects_second_collector(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "wind.lock"
            with collector_lock(path):
                with self.assertRaises(WindBusyError):
                    with collector_lock(path):
                        self.fail("second owner")

    def test_windows_lock_branch_locks_and_releases_one_byte(self):
        fake = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=MagicMock())
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "wind.lock"
            with patch.dict("sys.modules", {"msvcrt": fake}), \
                 patch("china_stock_engine.wind_session.os.name", "nt"):
                with collector_lock(path):
                    self.assertEqual(fake.locking.call_args.args[1:], (1, 1))
            self.assertEqual(fake.locking.call_args.args[1:], (2, 1))
            self.assertEqual(path.read_bytes(), b"0")

    def test_timed_out_request_closes_only_owned_child_and_cannot_reuse_late_reply(self):
        session = WindSession(exclusive_window=True)
        connection = MagicMock()
        connection.poll.return_value = False
        process = MagicMock()
        process.is_alive.return_value = False
        session.connection, session.process = connection, process
        with patch("china_stock_engine.wind_session.dashboard_running", return_value=False):
            with self.assertRaises(WindError):
                session.request("wsd", CODES[0], "close", DAYS[0], DAYS[-1], "Fill=Blank")
        connection.close.assert_called_once()
        self.assertIsNone(session.connection)
        self.assertIsNone(session.process)

    def test_doctor_does_not_import_sdk(self):
        with patch("china_stock_engine.wind_collect.dashboard_running", return_value=True), \
             patch("china_stock_engine.wind_session.importlib.import_module") as loader:
            result = doctor()
            self.assertFalse(result["live_session_started"])
            self.assertEqual(result["state"], "blocked_dashboard_running")
            loader.assert_not_called()


class WindCandidateTests(unittest.TestCase):
    def test_batched_checkpoint_collection_has_same_validated_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            status = collect_candidate(provider(MatrixFakeWind()), Path(temp), end=DAYS[-1],
                                       sessions=5, codes=CODES, batch_size=3, build_report=False)
            self.assertEqual(status["rows"], 15)
            self.assertFalse(status["production_changed"])
    def test_300_session_candidate_builds_screen_and_validates_without_native_sdk(self):
        from china_stock_engine.data_reference import load_market_history
        days = pd.bdate_range("2025-01-02", periods=300).strftime("%Y-%m-%d").tolist()
        with tempfile.TemporaryDirectory() as temp:
            status = collect_candidate(WindProvider(FakeWind(days), WindProfile(min_interval=0, units_verified=True)), Path(temp), end=days[-1],
                                       sessions=300, min_universe=3)
            root = Path(status["candidate"])
            self.assertTrue(status["report_built"])
            self.assertEqual(status["sessions"], 300)
            self.assertEqual(status["rows"], 900)
            history = load_market_history(root, days[-1], research_asof=status["completed_at"])
            self.assertEqual(history.trade_date.nunique(), 300)
            self.assertTrue(history.history_verified.all())
            self.assertTrue((root / "latest/technical_scan.parquet").exists())

    def test_candidate_uses_wind_provenance_preserves_missing_days_and_reuses_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "wind"
            fake = FakeWind()
            client = provider(fake)
            first = collect_candidate(client, workspace, end=DAYS[-1], sessions=5,
                                      codes=CODES, build_report=False)
            self.assertEqual(first["rows"], 15)
            self.assertTrue(first["canary_only"])
            self.assertFalse(first["production_changed"])
            self.assertFalse(first["units_verified"])
            import json
            manifest = json.loads((Path(first["candidate"]) / "latest/manifest.json").read_text())
            self.assertEqual(manifest["provider"], "windpy")
            self.assertIn("wsd", manifest["source_endpoints"])
            self.assertNotIn("universe_report", manifest["provenance"])
            before = sum(op == "wsd" for op, _ in fake.calls)
            second = collect_candidate(client, workspace, end=DAYS[-1], sessions=5,
                                       codes=CODES, build_report=False)
            self.assertEqual(before, sum(op == "wsd" for op, _ in fake.calls))
            self.assertNotEqual(first["candidate"], second["candidate"])

    def test_partial_universe_cannot_be_collected_as_full_market(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeWind()
            with self.assertRaises(WindError):
                collect_candidate(WindProvider(fake, WindProfile(min_interval=0, units_verified=True)), Path(temp), end=DAYS[-1], sessions=5)
            self.assertFalse(any(op == "wsd" for op, _ in fake.calls))

    def test_unreviewed_units_block_full_collection_before_any_request(self):
        fake = FakeWind()
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(WindError):
            collect_candidate(provider(fake), Path(temp), end=DAYS[-1], sessions=5)
        self.assertFalse(fake.calls)

    def test_candidate_cannot_target_production_directory(self):
        from china_stock_engine.wind_collect import ROOT
        with self.assertRaises(ValueError):
            collect_candidate(provider(), ROOT / "data", end=DAYS[-1])
