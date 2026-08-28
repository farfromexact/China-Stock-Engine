from __future__ import annotations

import json
import unittest

import pandas as pd

from china_stock_engine.opportunity_inputs import (
    MAX_OPPORTUNITY_INPUTS_BYTES,
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
        lowered = encoded.decode("utf-8").lower()
        for forbidden in ("recommendation", "total_score", '"buy"', '"sell"'):
            self.assertNotIn(forbidden, lowered)


if __name__ == "__main__":
    unittest.main()
