from __future__ import annotations

import unittest

import pandas as pd

from china_stock_engine.data_reference import (
    apply_adjustments,
    build_stock_state,
    build_tradability_state,
    data_cutoff_time_for_date,
)


def data_state_fixture() -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    str,
]:
    dates = pd.bdate_range("2026-07-24", periods=20).strftime("%Y-%m-%d").tolist()
    trade_date = dates[-1]
    codes = ["600001.SH", "000001.SZ", "688001.SH"]
    names = ["Alpha", "Beta", "Gamma"]
    rows: list[dict] = []
    adjustment_rows: list[dict] = []
    split_index = 15
    for code_index, (code, name) in enumerate(zip(codes, names, strict=True)):
        for index, date in enumerate(dates):
            if code_index == 0:
                economic_close = 50 + index * 0.08
                raw_close = economic_close * (2 if index < split_index else 1)
                factor = 0.5 if index < split_index else 1.0
            elif code_index == 1:
                economic_close = 30 + index * 0.005
                raw_close = economic_close
                factor = 1.0
            else:
                economic_close = 80 - index * 0.06
                raw_close = economic_close
                factor = 1.0
            rows.append(
                {
                    "trade_date": date,
                    "thscode": code,
                    "security_name": name,
                    "exchange": "SSE" if code.endswith(".SH") else "SZSE",
                    "board": "STAR" if code.startswith("688") else "MAIN",
                    "open": raw_close * 0.995,
                    "high": raw_close * 1.005,
                    "low": raw_close * 0.99,
                    "close": raw_close,
                    "pre_close": raw_close / 1.001,
                    "avg_price": raw_close * 0.998,
                    "volume": 10_000_000 + index * 10_000,
                    "amount": 150_000_000 + index * 100_000,
                    "turnover_ratio": 2 + index * 0.001,
                    "change_ratio": 0.1,
                    "source_provider": "synthetic",
                    "source_endpoint": "fixture",
                }
            )
            adjustment_rows.append(
                {
                    "trade_date": date,
                    "thscode": code,
                    "adj_factor": factor,
                    "known_at": f"{trade_date}T18:30:00+08:00",
                    "effective_at": date,
                    "published_at": None,
                }
            )
    history = pd.DataFrame(rows)
    reference = pd.DataFrame(
        {
            "as_of_date": [trade_date] * 3,
            "thscode": codes,
            "security_name": names,
            "exchange": ["SSE", "SZSE", "SSE"],
            "board": ["SSE_MAIN", "SZSE_MAIN", "STAR"],
            "listing_date": ["2000-01-01"] * 3,
            "total_shares": [1_000_000_000.0] * 3,
            "float_a_shares": [800_000_000.0] * 3,
            "source_provider": ["synthetic"] * 3,
            "source_endpoint": ["fixture"] * 3,
        }
    )
    status = pd.DataFrame(
        {
            "trade_date": [trade_date] * 3,
            "thscode": codes,
            "observation_state": ["traded"] * 3,
        }
    )
    industry = pd.DataFrame(
        [
            {
                "thscode": code,
                "classification_system": "SW",
                "level": level,
                "industry_code": f"{level}-{index}",
                "industry_name": f"Industry {index}",
                "effective_from": dates[0],
                "effective_to": None,
                "known_at": f"{dates[0]}T00:00:00+08:00",
            }
            for index, code in enumerate(codes)
            for level in ("SW1", "SW2")
        ]
    )
    industry = pd.concat(
        [
            industry,
            pd.DataFrame(
                [
                    {
                        "thscode": codes[0],
                        "classification_system": "SW",
                        "level": "SW1",
                        "industry_code": "FUTURE",
                        "industry_name": "Future Industry",
                        "effective_from": dates[0],
                        "effective_to": None,
                        "known_at": f"{trade_date}T23:00:00+08:00",
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    indexes = pd.DataFrame(
        [
            {
                "index_code": "000300.SH",
                "index_name": "CSI 300",
                "thscode": code,
                "weight": 1.0,
                "effective_from": dates[0],
                "effective_to": None,
                "known_at": f"{dates[0]}T00:00:00+08:00",
            }
            for code in codes
        ]
    )
    indexes = pd.concat(
        [
            indexes,
            pd.DataFrame(
                [
                    {
                        "index_code": "FUTURE.SH",
                        "index_name": "Future Index",
                        "thscode": codes[0],
                        "weight": 1.0,
                        "effective_from": dates[0],
                        "effective_to": None,
                        "known_at": f"{trade_date}T23:00:00+08:00",
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    provider = pd.DataFrame(
        {
            "as_of_date": [trade_date] * 3,
            "thscode": codes,
            "is_st": [False] * 3,
            "is_suspended": [False] * 3,
            "daily_price_limit_pct": [10.0, 10.0, 20.0],
            "lot_size": [100] * 3,
            "stock_connect_eligible": [True, True, False],
            "margin_eligible": [True, True, False],
            "short_sell_eligible": [False] * 3,
            "known_at": [f"{trade_date}T18:30:00+08:00"] * 3,
        }
    )
    return (
        history,
        reference,
        status,
        pd.DataFrame(adjustment_rows),
        industry,
        indexes,
        provider,
        trade_date,
    )


class DataStateTests(unittest.TestCase):
    def test_adjustments_neutralize_cash_split_and_rights_discontinuities(self) -> None:
        decision_time = "2026-08-20T20:15:00+08:00"
        history = pd.DataFrame(
            {
                "trade_date": ["2026-08-19", "2026-08-20"] * 3,
                "thscode": ["CASH.SH"] * 2 + ["SPLIT.SH"] * 2 + ["RIGHTS.SH"] * 2,
                "close": [10.0, 9.8, 100.0, 50.0, 20.0, 16.0],
            }
        )
        adjustments = pd.DataFrame(
            {
                "trade_date": ["2026-08-19", "2026-08-20"] * 3,
                "thscode": ["CASH.SH"] * 2 + ["SPLIT.SH"] * 2 + ["RIGHTS.SH"] * 2,
                "adj_factor": [0.98, 1.0, 0.5, 1.0, 0.8, 1.0],
                "known_at": ["2026-08-20T18:30:00+08:00"] * 6,
            }
        )
        adjusted = apply_adjustments(history, adjustments, decision_time)
        for _, group in adjusted.groupby("thscode"):
            self.assertAlmostEqual(
                float(group.iloc[0]["adjusted_close"]),
                float(group.iloc[1]["adjusted_close"]),
            )

    def test_stock_state_is_pit_adjusted_and_contains_no_selection_output(self) -> None:
        history, reference, status, adjustments, industry, indexes, provider, date = (
            data_state_fixture()
        )
        state, readiness = build_stock_state(
            history,
            reference,
            status,
            adjustments,
            pd.DataFrame(),
            industry,
            indexes,
            pd.DataFrame(),
            provider,
            date,
            "snapshot-hash",
        )
        alpha = state.loc[state["thscode"].eq("600001.SH")].iloc[0]
        self.assertEqual(int(alpha["history_sessions"]), 20)
        self.assertGreater(float(alpha["raw_return_20d_pct"]), 0)
        self.assertGreater(float(alpha["return_5d_pct"]), 0)
        self.assertEqual(alpha["sw1_name"], "Industry 0")
        self.assertNotEqual(alpha["sw1_name"], "Future Industry")
        self.assertEqual(alpha["index_memberships"], ["000300.SH"])
        self.assertEqual(alpha["tradability_state"], "clear")
        self.assertEqual(alpha["data_cutoff_time"], data_cutoff_time_for_date(date))
        self.assertEqual(readiness["history"]["state"], "ready")
        self.assertEqual(readiness["history"]["horizons"]["1D"]["state"], "ready")
        self.assertEqual(readiness["history"]["horizons"]["3D"]["state"], "ready")
        self.assertEqual(readiness["history"]["horizons"]["5D"]["state"], "ready")
        self.assertEqual(readiness["history"]["horizons"]["20D"]["state"], "ready")
        self.assertTrue(pd.isna(alpha["return_60d_pct"]))
        self.assertTrue(pd.isna(alpha["distance_from_high_60_pct"]))
        self.assertTrue(pd.isna(alpha["drawdown_from_high_252_pct"]))
        self.assertEqual(readiness["adjustment"]["state"], "ready")
        self.assertEqual(readiness["tradability"]["state"], "ready")

        self.assertEqual(alpha["source_snapshot_sha256"], "snapshot-hash")
        self.assertNotIn("score", " ".join(state.columns).lower())

    def test_partial_history_keeps_legal_horizons_and_stock_state(self) -> None:
        history, reference, status, adjustments, industry, indexes, provider, date = (
            data_state_fixture()
        )
        recent_dates = sorted(history["trade_date"].unique())[-5:]
        history = history.loc[history["trade_date"].isin(recent_dates)].copy()
        adjustments = adjustments.loc[
            adjustments["trade_date"].isin(recent_dates)
        ].copy()

        state, readiness = build_stock_state(
            history,
            reference,
            status,
            adjustments,
            pd.DataFrame(),
            industry,
            indexes,
            pd.DataFrame(),
            provider,
            date,
            "snapshot-hash",
        )

        alpha = state.loc[state["thscode"].eq("600001.SH")].iloc[0]
        self.assertEqual(int(alpha["history_sessions"]), 5)
        self.assertEqual(readiness["history"]["state"], "partial")
        self.assertEqual(readiness["history"]["horizons"]["1D"]["state"], "ready")
        self.assertEqual(readiness["history"]["horizons"]["3D"]["state"], "ready")
        self.assertEqual(readiness["history"]["horizons"]["5D"]["state"], "ready")
        self.assertEqual(readiness["history"]["horizons"]["20D"]["state"], "missing")
        self.assertGreater(float(alpha["raw_return_5d_pct"]), 0)
        self.assertTrue(pd.isna(alpha["raw_return_20d_pct"]))
        self.assertEqual(readiness["stock_state"]["state"], "ready")

    def test_one_word_limit_and_unknown_provider_fields_are_classified(self) -> None:
        history, reference, status, _, _, _, provider, date = data_state_fixture()
        current_mask = history["trade_date"].eq(date) & history["thscode"].eq("600001.SH")
        history.loc[current_mask, ["open", "high", "low", "close"]] = 100.0
        history.loc[current_mask, "change_ratio"] = 10.0
        provider = provider.loc[provider["thscode"].ne("000001.SZ")].copy()
        tradability, _ = build_tradability_state(
            history,
            reference,
            status,
            provider,
            date,
            data_cutoff_time_for_date(date),
        )
        one_word = tradability.loc[tradability["thscode"].eq("600001.SH")].iloc[0]
        unknown = tradability.loc[tradability["thscode"].eq("000001.SZ")].iloc[0]
        self.assertTrue(bool(one_word["one_word_limit"]))
        self.assertEqual(one_word["tradability_state"], "restricted")
        self.assertIn("one_word_limit", one_word["tradability_reason_codes"])
        self.assertTrue(pd.isna(unknown["is_suspended"]))
        self.assertTrue(pd.isna(unknown["limit_up"]))
        self.assertTrue(pd.isna(unknown["limit_down"]))
        self.assertTrue(pd.isna(unknown["one_word_limit"]))
        self.assertEqual(unknown["tradability_state"], "unknown")
        self.assertIn(
            "provider_tradability_missing", unknown["tradability_reason_codes"]
        )


if __name__ == "__main__":
    unittest.main()
