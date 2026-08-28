from __future__ import annotations

import json
import unittest

import pandas as pd

from china_stock_engine.opportunity_inputs import (
    MAX_OPPORTUNITY_INPUTS_BYTES,
    OPPORTUNITY_INPUTS_SCHEMA_VERSION,
    build_opportunity_inputs,
)


class OpportunityInputsTests(unittest.TestCase):
    def test_compact_contract_contains_facts_and_bounded_deterministic_screens(
        self,
    ) -> None:
        count = 40
        codes = [f"{600000 + index:06d}.SH" for index in range(count)]
        state = pd.DataFrame(
            {
                "thscode": codes,
                "security_name": [f"Security {index}" for index in range(count)],
                "exchange": ["SSE"] * count,
                "board": ["SSE_MAIN"] * count,
                "raw_close": [10.0 + index for index in range(count)],
                "amount_change_pct": [float(index - 10) for index in range(count)],
                "turnover_change_pct": [float(index - 20) for index in range(count)],
                "close_location": [index / (count - 1) for index in range(count)],
                "raw_return_1d_pct": [float(index - 20) / 10 for index in range(count)],
                "raw_return_3d_pct": [float(index - 20) / 5 for index in range(count)],
                "raw_return_5d_pct": [float(index - 20) / 4 for index in range(count)],
                "raw_return_20d_pct": [float(index - 20) / 2 for index in range(count)],
                "return_1d_pct": [pd.NA] * count,
                "return_3d_pct": [pd.NA] * count,
                "return_5d_pct": [pd.NA] * count,
                "return_20d_pct": [pd.NA] * count,
                "float_market_cap": [4_000_000_000.0 + index * 1_000_000_000 for index in range(count)],
                "total_market_cap": [5_000_000_000.0 + index * 10_000_000_000 for index in range(count)],
                "tradability_state": ["unknown"] * count,
                "limit_up": pd.Series([pd.NA] * count, dtype="boolean"),
                "limit_down": pd.Series([pd.NA] * count, dtype="boolean"),
                "one_word_limit": pd.Series([pd.NA] * count, dtype="boolean"),
                "effective_pit_cutoff": ["2026-08-20T10:30:00+00:00"] * count,
            }
        )
        quotes = pd.DataFrame(
            {
                "thscode": codes,
                "change_ratio": [float(index - 20) / 10 for index in range(count)],
                "amount": [1_000_000.0 * (index + 1) for index in range(count)],
                "volume": [100_000.0 * (index + 1) for index in range(count)],
                "turnover_ratio": [float(index + 1) / 10 for index in range(count)],
            }
        )
        readiness = {
            "history": {
                "state": "ready",
                "sessions": 20,
                "target_sessions": 20,
                "horizons": {
                    f"{periods}D": {"state": "ready", "coverage": 1.0}
                    for periods in (1, 3, 5, 20)
                },
            },
            "stock_state": {"state": "ready", "rows": count},
        }
        timing = {
            "collection_started_at": "2026-08-20T10:00:00+00:00",
            "collection_completed_at": "2026-08-20T10:30:00+00:00",
            "configured_decision_cutoff": "2026-08-20T20:15:00+08:00",
            "effective_pit_cutoff": "2026-08-20T10:30:00+00:00",
        }
        payload = build_opportunity_inputs(
            {
                "trade_date": "2026-08-20",
                "quality": {"metrics": {"drift": {"state": "checked"}}},
            },
            {
                "advancers": 19,
                "decliners": 20,
                "unchanged": 1,
                "moves_ge_9_5pct": 0,
                "moves_le_minus_9_5pct": 0,
                "equal_weight_change_pct": 0.0,
                "median_change_pct": 0.0,
                "total_amount": float(quotes["amount"].sum()),
                "quoted_securities": count,
                "observation_state_counts": {"traded": count},
            },
            quotes,
            state,
            readiness,
            "source-hash",
            timing,
        )

        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )
        self.assertLess(len(encoded), MAX_OPPORTUNITY_INPUTS_BYTES)
        self.assertEqual(payload["schema_version"], OPPORTUNITY_INPUTS_SCHEMA_VERSION)
        self.assertEqual(payload["trade_date"], "2026-08-20")
        self.assertEqual(payload["market"]["changes"]["20D"]["state"], "ready")
        self.assertFalse(payload["data_mode"]["subjective_view_produced"])
        self.assertFalse(payload["data_mode"]["composite_score_produced"])
        for screen in payload["deterministic_screens"].values():
            self.assertLessEqual(len(screen["rows"]), 25)
            for row in screen["rows"]:
                self.assertIn("trigger", row)
        unknown = payload["deterministic_screens"]["highest_amount"]["rows"][0]
        self.assertIsNone(unknown["limit_up"])
        self.assertLessEqual(len(payload["candidate_union"]), 150)
        self.assertEqual(
            [row["rank"] for row in payload["candidate_union"]],
            list(range(1, len(payload["candidate_union"]) + 1)),
        )
        for row in payload["candidate_union"]:
            self.assertEqual(row["screen_count"], len(row["triggered_screens"]))
            self.assertEqual(set(row["triggered_screens"]), set(row["screen_ranks"]))
            self.assertEqual(
                set(row["triggered_screens"]), set(row["screen_percentiles"])
            )
        lowered = encoded.decode("utf-8").lower()
        for forbidden in ("recommendation", "total_score", '"buy"', '"sell"'):
            self.assertNotIn(forbidden, lowered)

    def test_candidate_union_keeps_orthogonal_evidence_and_flags(self) -> None:
        codes = ["688001.SH", "300001.SZ", "600001.SH", "000001.SZ", "920001.BJ"]
        state = pd.DataFrame(
            {
                "thscode": codes,
                "security_name": ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"],
                "exchange": ["SSE", "SZSE", "SSE", "SZSE", "BSE"],
                "board": ["STAR", "CHINEXT", "SSE_MAIN", "SZSE_MAIN", "BSE"],
                "raw_close": [20.0, 12.0, 8.0, 15.0, 6.0],
                "amount_change_pct": [250.0, 180.0, 120.0, -30.0, -10.0],
                "turnover_change_pct": [180.0, 160.0, 100.0, -20.0, -5.0],
                "close_location": [0.95, 0.20, 0.10, 0.85, 0.50],
                "gap_pct": [2.0, 4.0, -1.0, -3.0, 0.0],
                "intraday_range_pct": [8.0, 12.0, 10.0, 9.0, 2.0],
                "close_vs_avg_pct": [2.0, -1.0, -3.0, 1.0, 0.0],
                "raw_return_1d_pct": [8.0, 6.0, -7.0, -5.0, 0.5],
                "raw_return_3d_pct": [12.0, -3.0, -8.0, -2.0, 1.0],
                "raw_return_5d_pct": [20.0, -12.0, -4.0, 3.0, 2.0],
                "raw_return_20d_pct": [30.0, -20.0, -10.0, 5.0, 3.0],
                "return_1d_pct": [pd.NA] * 5,
                "return_3d_pct": [pd.NA] * 5,
                "return_5d_pct": [pd.NA] * 5,
                "return_20d_pct": [pd.NA] * 5,
                "float_market_cap": [8e9, 2e9, 30e9, 100e9, 3e9],
                "total_market_cap": [10e9, 4e9, 50e9, 150e9, 4.5e9],
                "tradability_state": ["unknown"] * 5,
                "limit_up": pd.Series([pd.NA] * 5, dtype="boolean"),
                "limit_down": pd.Series([pd.NA] * 5, dtype="boolean"),
                "one_word_limit": pd.Series([pd.NA] * 5, dtype="boolean"),
                "effective_pit_cutoff": ["2026-08-20T10:30:00+00:00"] * 5,
            }
        )
        quotes = pd.DataFrame(
            {
                "thscode": codes,
                "change_ratio": [8.0, 6.0, -7.0, -5.0, 0.5],
                "amount": [2e9, 10e6, 1e9, 800e6, 5e6],
                "volume": [20e6, 1e6, 15e6, 10e6, 500e3],
                "turnover_ratio": [8.0, 30.0, 15.0, 5.0, 2.0],
            }
        )
        readiness = {
            "history": {
                "state": "ready",
                "sessions": 20,
                "target_sessions": 20,
                "horizons": {
                    f"{periods}D": {"state": "ready", "coverage": 1.0}
                    for periods in (1, 3, 5, 20)
                },
            },
            "stock_state": {"state": "ready", "rows": 5},
        }
        timing = {
            "collection_started_at": "2026-08-20T10:00:00+00:00",
            "collection_completed_at": "2026-08-20T10:30:00+00:00",
            "configured_decision_cutoff": "2026-08-20T20:15:00+08:00",
            "effective_pit_cutoff": "2026-08-20T10:30:00+00:00",
        }
        payload = build_opportunity_inputs(
            {"trade_date": "2026-08-20", "quality": {"metrics": {}}},
            {
                "advancers": 3,
                "decliners": 2,
                "unchanged": 0,
                "total_amount": float(quotes["amount"].sum()),
                "quoted_securities": 5,
            },
            quotes,
            state,
            readiness,
            "source-hash",
            timing,
            screen_limit=5,
            candidate_union_limit=5,
        )

        self.assertEqual(len(payload["candidate_union"]), 5)
        beta = next(
            row for row in payload["candidate_union"] if row["thscode"] == "300001.SZ"
        )
        self.assertIn("price_up_but_weak_close", beta["contradiction_flags"])
        self.assertIn("tiny_absolute_amount", beta["contradiction_flags"])
        self.assertIn("micro_cap", beta["contradiction_flags"])
        self.assertIn("high_turnover", beta["contradiction_flags"])
        self.assertIn("gap_up_failed", beta["contradiction_flags"])
        self.assertIn("price_up_activity_expansion", beta["triggered_screens"])
        self.assertIn("large_positive_weak_close", beta["triggered_screens"])
        self.assertEqual(beta["facts"]["amount_rank"], 4)
        self.assertAlmostEqual(beta["facts"]["amount_to_float_market_cap"], 0.005)
        for percentile in beta["percentiles"].values():
            if percentile is not None:
                self.assertGreater(percentile, 0)
                self.assertLessEqual(percentile, 1)
        screens = payload["deterministic_screens"]
        self.assertIn("positive_momentum_1d_3d_5d", screens)
        self.assertIn("gap_down_strong_close", screens)
        self.assertIn("board_neutral_absolute_move__bse", screens)
        self.assertIn(
            "market_cap_neutral_absolute_move__micro_lt_5bn_cny", screens
        )
        self.assertIn("not a score", payload["candidate_union_metadata"]["screen_count_semantics"])


if __name__ == "__main__":
    unittest.main()
