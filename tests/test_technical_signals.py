from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import pandas as pd

from china_stock_engine.data_reference import apply_adjustments, build_stock_state, load_market_history
from china_stock_engine.technical_signals import (
    TechnicalConfig, add_technical_metrics, scan_technical_signals, technical_candidates,
    attach_signal_history,
)
from china_stock_engine.storage import atomic_write_json, atomic_write_parquet, sha256_file
from china_stock_engine.opportunity_radar import build_opportunity_radar_inputs


def fixture(n=130, code="600000.SH"):
    dates = pd.bdate_range("2025-01-02", periods=n).strftime("%Y-%m-%d").tolist()
    rows = [{"thscode": code, "trade_date": date, "open": 9.9, "high": 10., "low": 9.6, "close": 9.9,
             "pre_close": 9.9, "avg_price": 9.9, "volume": 1_000_000, "amount": 150_000_000.,
             "turnover_ratio": 1., "change_ratio": 0., "history_verified": True,
             "security_name": "Example", "exchange": "SSE", "board": "MAIN"} for date in dates]
    rows[-1].update(open=10., high=10.35, low=9.95, close=10.3, amount=300_000_000.)
    history = pd.DataFrame(rows)
    factors = pd.DataFrame([{"thscode": code, "trade_date": date, "adj_factor": 1.,
                             "known_at": dates[-1] + "T18:00:00+08:00"} for date in dates])
    reference = pd.DataFrame([{"thscode": code, "as_of_date": dates[-1], "listing_date": dates[0],
                               "security_name": "Example", "is_delisting": False,
                               "total_shares": 1e9, "float_a_shares": 8e8}])
    return history, factors, reference, dates


def scan_fixture(history, factors, reference, dates):
    panel = add_technical_metrics(apply_adjustments(history, factors, dates[-1] + "T20:00:00+08:00"))
    state = panel.loc[panel["trade_date"].eq(dates[-1])].copy()
    for key in ("is_st", "is_suspended", "limit_up", "one_word_limit"):
        state[key] = False
    state["daily_price_limit_pct"], state["lot_size"] = 10., 100
    scan, summary = scan_technical_signals(state, reference, dates, dates[-1], TechnicalConfig())
    return panel, scan, summary


class TechnicalSignalsTests(unittest.TestCase):
    def test_exact_breakout_example_and_today_excluded(self):
        h, f, ref, dates = fixture()
        panel, scan, summary = scan_fixture(h, f, ref, dates)
        row = scan.iloc[0]
        self.assertEqual(row["signal"], "BREAKOUT")
        self.assertEqual(row["eligibility"], "eligible")
        self.assertEqual(row["entry_state"], "review_next_session")
        self.assertAlmostEqual(row["prior_high60"], 10.)
        self.assertAlmostEqual(row["prior_adt20"], 150_000_000.)
        self.assertAlmostEqual(row["amount_ratio20"], 2.)
        self.assertAlmostEqual(row["technical_close_location"], .875)
        self.assertAlmostEqual(row["breakout_distance_atr"], .75)
        self.assertEqual(summary["comparison_counts"], {"A0": 1, "A1": 1, "A2": 1})

    def test_raw_split_does_not_create_false_breakout(self):
        h, f, ref, dates = fixture()
        # Post-split quote jumps in raw units, but the adjusted series is unchanged.
        h.loc[h.index[-1], ["open", "high", "low", "close"]] = [19.8, 20., 19.2, 19.8]
        f.loc[f.index[-1], "adj_factor"] = .5
        panel, scan, _ = scan_fixture(h, f, ref, dates)
        self.assertEqual(scan.iloc[0]["signal"], "NONE")
        self.assertAlmostEqual(panel.iloc[-1]["adjusted_high"], 10.)

    def test_missing_factor_gap_or_zero_trade_breaks_window(self):
        for case in ("factor", "gap", "zero", "unverified"):
            with self.subTest(case=case):
                h, f, ref, dates = fixture()
                if case == "factor":
                    f = f.drop(f.index[-15])
                elif case == "gap":
                    h.loc[h.index[-15], ["open", "high", "low", "close"]] = float("nan")
                elif case == "zero":
                    h.loc[h.index[-15], "volume"] = 0
                else:
                    h.loc[h.index[-15], "history_verified"] = False
                _, scan, _ = scan_fixture(h, f, ref, dates)
                self.assertEqual(scan.iloc[0]["signal"], "NONE")
                self.assertIn("history_or_adjustment_gap_61_sessions", scan.iloc[0]["exclusion_reasons"])

    def test_thresholds_and_flat_candle(self):
        for value, expected in ((225_000_000., "BREAKOUT"), (224_999_999., "NONE")):
            h, f, ref, dates = fixture()
            h.loc[h.index[-1], "amount"] = value
            self.assertEqual(scan_fixture(h, f, ref, dates)[1].iloc[0]["signal"], expected)
        h.loc[h.index[-1], "amount"] = 300_000_000.
        h.loc[h.index[-1], ["open", "high", "low", "close"]] = 10.3
        row = scan_fixture(h, f, ref, dates)[1].iloc[0]
        self.assertEqual(row["signal"], "NONE")
        self.assertTrue(row["a1"])
        self.assertFalse(row["a2"])

    def test_watch_and_shrink_tag_not_hard_gate(self):
        h, f, ref, dates = fixture()
        for index in h.index:
            c = 8. + index * .015
            h.loc[index, ["open", "high", "low", "close"]] = [c, c + .2, c - .2, c]
        for index in h.index[-5:]:
            c = 9.90 + (index - h.index[-5]) * .01
            h.loc[index, ["open", "high", "low", "close"]] = [c, c + .01, c - .01, c]
        _, scan, _ = scan_fixture(h, f, ref, dates)
        self.assertEqual(scan.iloc[0]["signal"], "WATCH")
        self.assertFalse(scan.iloc[0]["shrink_amount_tag"])

    def test_full_reference_includes_missing_quotes_and_pending_status(self):
        h, f, ref, dates = fixture()
        panel, _, _ = scan_fixture(h, f, ref, dates)
        state = panel.loc[panel["trade_date"].eq(dates[-1])]
        ref = pd.concat([ref, ref.assign(thscode="000001.SZ")])
        scan, summary = scan_technical_signals(state, ref, dates, dates[-1], TechnicalConfig())
        self.assertEqual(summary["universe_count"], 2)
        row = scan.loc[scan["thscode"].eq("600000.SH")].iloc[0]
        self.assertEqual(row["signal"], "BREAKOUT")
        self.assertEqual(row["entry_state"], "pending_verification")
        self.assertIn("is_st_unknown", row["pending_reasons"])
        self.assertEqual(summary["exclusion_reason_counts"]["current_quote_missing"], 1)

    def test_extended_breakout_kept_but_waits(self):
        h, f, ref, dates = fixture()
        h.loc[h.index[-1], ["open", "high", "low", "close"]] = [10., 11., 9.9, 10.9]
        _, scan, _ = scan_fixture(h, f, ref, dates)
        self.assertEqual(scan.iloc[0]["signal"], "BREAKOUT")
        self.assertEqual(scan.iloc[0]["entry_state"], "wait")

    def test_120_day_state_not_truncated_and_year_fields_optional(self):
        h, f, ref, dates = fixture(300)
        status = pd.DataFrame([{"thscode": "600000.SH", "observation_state": "traded"}])
        state, readiness = build_stock_state(h, ref, status, f, pd.DataFrame(), pd.DataFrame(),
            pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), dates[-1], "a" * 64,
            decision_time=dates[-1] + "T20:00:00+08:00", trading_sessions=dates)
        row = state.iloc[0]
        self.assertEqual(row["history_sessions"], 300)
        self.assertTrue(pd.notna(row["return_60d_pct"]))
        self.assertTrue(pd.notna(row["drawdown_from_high_252_pct"]))
        self.assertEqual(readiness["history"]["state"], "ready")
        state, _ = build_stock_state(h, ref, status, f, pd.DataFrame(), pd.DataFrame(),
            pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), dates[-1], "a" * 64,
            decision_time=dates[-1] + "T20:00:00+08:00", trading_sessions=dates, history_sessions=120)
        self.assertEqual(state.iloc[0]["history_sessions"], 120)
        self.assertTrue(pd.isna(state.iloc[0]["drawdown_from_high_252_pct"]))
        self.assertTrue(state.iloc[0]["technical_window_ready"])

    def test_technical_radar_ignores_old_top100(self):
        h, f, ref, dates = fixture()
        _, scan, summary = scan_fixture(h, f, ref, dates)
        matches = technical_candidates(scan)
        source = {"generated_at": "2026-01-01T10:00:00+00:00", "source_snapshot_sha256": "a" * 64,
                  "pit_timing": {"collection_completed_at": "2026-01-01T10:00:00+00:00"},
                  "candidate_union": [], "technical_signals": {"summary": summary, "candidates": matches,
                  "ordering_policy": "explicit", "series_command": "inspect-series", "research_policy": "optional"}}
        radar = build_opportunity_radar_inputs(source)
        self.assertEqual(radar["candidate_union"][0]["thscode"], "600000.SH")
        self.assertEqual(radar["candidate_union_metadata"]["screening_scope"], "all_PIT_reference_securities")

    def test_price_equality_is_not_breakout_and_young_listing_is_excluded(self):
        h, f, ref, dates = fixture()
        h.loc[h.index[-1], "close"] = 10.
        self.assertEqual(scan_fixture(h, f, ref, dates)[1].iloc[0]["signal"], "NONE")
        h, f, ref, dates = fixture(119)
        row = scan_fixture(h, f, ref, dates)[1].iloc[0]
        self.assertIn("listing_age_below_minimum", row["exclusion_reasons"])
        self.assertEqual(row["signal"], "NONE")

    def test_research_exclusion_is_logged_without_deleting_signal(self):
        from contextlib import redirect_stdout
        import io
        import json
        from china_stock_engine.cli import main
        h, f, ref, dates = fixture()
        _, scan, _ = scan_fixture(h, f, ref, dates)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "features/technical_signals" / f"trade_date={dates[-1]}" / "technical_scan.parquet"
            atomic_write_parquet(path, scan)
            before = path.read_bytes()
            atomic_write_json(path.parent / "signal_receipt.json", {"trade_date": dates[-1],
                "effective_pit_cutoff": dates[-1] + "T20:00:00+08:00", "feature_input_sha256": "a" * 64,
                "artifacts": {path.name: {"sha256": sha256_file(path)}}})
            with redirect_stdout(io.StringIO()) as output:
                result = main(["record-technical-review", "--code", "600000.SH", "--date", dates[-1],
                    "--decision", "exclude", "--reason", "Synthetic verified risk", "--data-dir", str(root)])
            self.assertEqual(result, 0)
            record = json.loads(Path(json.loads(output.getvalue())["path"]).read_text())
            self.assertEqual(record["decision"], "exclude")
            self.assertEqual(record["signal"], "BREAKOUT")
            self.assertEqual(record["signal_scan_sha256"], sha256_file(path))
            self.assertEqual(before, path.read_bytes())

    def test_full_matching_set_is_saved_before_radar_limit(self):
        h, f, ref, dates = fixture()
        _, scan, summary = scan_fixture(h, f, ref, dates)
        scans = pd.concat([scan.assign(thscode=f"{i:06d}.SH") for i in range(150)], ignore_index=True)
        matches = technical_candidates(scans)
        source = {"generated_at": "2026-01-01T10:00:00+00:00", "source_snapshot_sha256": "a" * 64,
                  "pit_timing": {"collection_completed_at": "2026-01-01T10:00:00+00:00"},
                  "candidate_union": [], "technical_signals": {"summary": summary, "candidates": matches,
                  "ordering_policy": "explicit", "series_command": "inspect-series", "research_policy": "optional"}}
        radar = build_opportunity_radar_inputs(source)
        self.assertEqual(len(matches), 150)
        self.assertEqual(len(radar["candidate_union"]), 100)
        self.assertEqual(radar["candidate_union_metadata"]["source_candidate_count"], 150)
        self.assertTrue(radar["candidate_union_metadata"]["truncation_applied"])

    def test_late_collected_prices_cannot_trigger_historical_signal(self):
        h, f, ref, dates = fixture()
        h["first_observed_at"] = (pd.Timestamp(dates[-1]) + pd.Timedelta(days=2)).isoformat() + "+08:00"
        adjusted = apply_adjustments(h, f, dates[-1] + "T20:00:00+08:00")
        metrics = add_technical_metrics(adjusted)
        self.assertFalse(metrics.iloc[-1]["technical_window_ready"])
        self.assertFalse(metrics.iloc[-1]["current_bar_valid"])

    def test_explicit_adjustment_range_is_not_truncated_to_21(self):
        from china_stock_engine.ifind_supplemental import _cached_closes
        h, _, _, dates = fixture(80)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for row in h.to_dict("records"):
                path = root / "facts/market" / f"trade_date={row['trade_date']}" / "daily_quotes.parquet"
                atomic_write_parquet(path, pd.DataFrame([row]))
            closes = _cached_closes(root, dates[-1], ["600000.SH"], start_date=dates[0])
            self.assertEqual(len(closes), 80)
            self.assertEqual(len(_cached_closes(root, dates[-1], ["600000.SH"])), 80)

    def test_certified_calendar_excludes_holidays_without_hiding_quote_gap(self):
        h, _, _, dates = fixture(3)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for row in h.to_dict("records"):
                path = root / "facts/market" / f"trade_date={row['trade_date']}" / "daily_quotes.parquet"
                atomic_write_parquet(path, pd.DataFrame([row]))
            # Middle weekday is a confirmed exchange holiday for this fixture.
            opens = [dates[0], dates[-1]]
            calendar = pd.DataFrame({"trade_date": opens, "is_open": True,
                "calendar_window_start": dates[0], "calendar_window_end": dates[-1],
                "calendar_window_complete": True})
            path = root / "facts/reference" / f"as_of_date={dates[-1]}" / "trading_calendar.parquet"
            atomic_write_parquet(path, calendar)
            # No quote exists on the certified holiday.
            (root / "facts/market" / f"trade_date={dates[1]}" / "daily_quotes.parquet").unlink()
            loaded = load_market_history(root, dates[-1])
            self.assertEqual(loaded.attrs["session_dates"], opens)
            self.assertNotIn(dates[1], loaded.attrs["unconfirmed_calendar_dates"])

    def test_legacy_snapshot_receipt_is_saved_before_pruning(self):
        from china_stock_engine.storage import _prune_snapshots
        h, _, _, dates = fixture(2)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for day in dates:
                artifacts = {}
                for name in ("daily_quotes.parquet", "daily_security_status.parquet"):
                    frame = h.loc[h["trade_date"].eq(day)]
                    fact = root / "facts/market" / f"trade_date={day}" / name
                    snapshot = root / "snapshots" / day / name
                    atomic_write_parquet(fact, frame)
                    atomic_write_parquet(snapshot, frame)
                    artifacts[name] = {"sha256": sha256_file(snapshot)}
                atomic_write_json(root / "snapshots" / day / "manifest.json", {
                    "schema_version": 3, "trade_date": day, "verified": True,
                    "collection_completed_at": day + "T18:00:00+08:00", "artifacts": artifacts})
            _prune_snapshots(root, 1)
            self.assertFalse((root / "snapshots" / dates[0]).exists())
            loaded = load_market_history(root, dates[-1], research_asof=dates[-1] + "T20:00:00+08:00")
            self.assertEqual(len(loaded), 2)

    def test_anchor_fixed_on_continuation_and_failure(self):
        h, f, ref, dates = fixture()
        panel, scan, _ = scan_fixture(h, f, ref, dates)
        cutoff = dates[-1] + "T20:00:00+08:00"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = attach_signal_history(root, scan, panel, dates[-1], cutoff, TechnicalConfig())
            self.assertEqual(first.iloc[0]["lifecycle"], "first_breakout")
            path = root / "features/technical_signals" / f"trade_date={dates[-1]}" / "technical_scan.parquet"
            atomic_write_parquet(path, first)
            atomic_write_json(path.parent / "signal_receipt.json", {"effective_pit_cutoff": cutoff,
                "artifacts": {path.name: {"sha256": sha256_file(path)}}})
            day = (pd.Timestamp(dates[-1]) + pd.offsets.BDay()).date().isoformat()
            scan["trade_date"] = day
            scan["prior_high60"] = 10.35  # Rolling high must not replace original 10.00 anchor.
            continuing = attach_signal_history(root, scan, panel, day, day + "T20:00:00+08:00", TechnicalConfig())
            self.assertEqual(continuing.iloc[0]["lifecycle"], "continuation")
            self.assertEqual(continuing.iloc[0]["anchor_level_adjusted"], 10.)
            scan["adjusted_close"] = 9.9
            failed = attach_signal_history(root, scan, panel, day, day + "T20:00:00+08:00", TechnicalConfig())
            self.assertEqual(failed.iloc[0]["lifecycle"], "failed")
            # A later current-research view may own the original signal when
            # historical EOD facts were collected after the EOD cutoff.
            research_path = path.parent / "research_view=aaaaaaaaaaaaaaaa" / path.name
            atomic_write_parquet(research_path, first)
            atomic_write_json(research_path.parent / "signal_receipt.json", {
                "trade_date": dates[-1], "effective_pit_cutoff": dates[-1] + "T21:00:00+08:00",
                "artifacts": {path.name: {"sha256": sha256_file(research_path)}}})
            scan["adjusted_close"] = 10.3
            carried = attach_signal_history(root, scan, panel, day, day + "T20:00:00+08:00", TechnicalConfig())
            self.assertEqual(carried.iloc[0]["lifecycle"], "continuation")
            self.assertEqual(carried.iloc[0]["anchor_level_adjusted"], 10.)

    def test_durable_receipt_survives_pruning_and_checks_hash(self):
        h, _, _, dates = fixture(1)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "facts/market" / f"trade_date={dates[-1]}" / "daily_quotes.parquet"
            atomic_write_parquet(path, h)
            atomic_write_json(path.parent / "market_receipt.json", {"schema_version": 3, "verified": True,
                "collection_completed_at": dates[-1] + "T18:00:00+08:00",
                "artifacts": {path.name: {"sha256": sha256_file(path)}}})
            loaded = load_market_history(root, dates[-1], research_asof=dates[-1] + "T20:00:00+08:00")
            self.assertEqual(len(loaded), 1)
            self.assertTrue(loaded.iloc[0]["history_verified"])
            atomic_write_parquet(path, h.assign(close=11.))
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                load_market_history(root, dates[-1])


if __name__ == "__main__":
    unittest.main()
