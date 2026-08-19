from __future__ import annotations

import unittest

import pandas as pd

from china_stock_engine.quality import (
    board_from_code,
    build_market_summary,
    normalize_quotes,
    normalize_universe,
    validate_data,
)


def universe_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "as_of_date": ["2026-08-18"] * 4,
            "thscode": ["600000.SH", "688001.SH", "000001.SZ", "920001.BJ"],
            "security_name": ["A", "B", "C", "D"],
            "security_name_in_time": ["A", "B", "C", "D"],
        }
    )


def quote_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": ["2026-08-18"] * 4,
            "thscode": ["600000.SH", "688001.SH", "000001.SZ", "920001.BJ"],
            "open": [10, 20, 30, 40],
            "high": [11, 21, 31, 41],
            "low": [9, 19, 29, 39],
            "close": [10.5, 20.5, 30.5, 40.5],
            "volume": [100, 200, 300, 400],
            "amount": [1000, 2000, 3000, 4000],
            "change_ratio": [1.0, -2.0, 0.0, 10.0],
            "source_provider": ["ifind_http"] * 4,
            "source_endpoint": ["cmd_history_quotation"] * 4,
        }
    )


class QualityTests(unittest.TestCase):
    def test_board_classification(self) -> None:
        self.assertEqual(board_from_code("688001.SH"), "STAR")
        self.assertEqual(board_from_code("300001.SZ"), "CHINEXT")
        self.assertEqual(board_from_code("920001.BJ"), "BSE")

    def test_valid_snapshot_passes(self) -> None:
        universe = normalize_universe(universe_frame())
        quotes = normalize_quotes(quote_frame(), universe)
        report = validate_data(
            universe,
            quotes,
            "2026-08-18",
            min_universe_size=4,
            min_quote_coverage=1.0,
        )
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.metrics["quote_count"], 4)

    def test_date_and_ohlc_failures_are_explicit(self) -> None:
        universe = normalize_universe(universe_frame())
        raw_quotes = quote_frame()
        raw_quotes.loc[0, "trade_date"] = "2026-08-17"
        raw_quotes.loc[1, "high"] = 18
        quotes = normalize_quotes(raw_quotes, universe)
        report = validate_data(
            universe,
            quotes,
            "2026-08-18",
            min_universe_size=4,
            min_quote_coverage=1.0,
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("source dates" in error for error in report.errors))
        self.assertTrue(any("invalid OHLC" in error for error in report.errors))

    def test_market_summary_uses_non_null_changes(self) -> None:
        universe = normalize_universe(universe_frame())
        quotes = normalize_quotes(quote_frame(), universe)
        summary = build_market_summary(quotes, "2026-08-18")
        self.assertEqual(summary["advancers"], 2)
        self.assertEqual(summary["decliners"], 1)
        self.assertEqual(summary["unchanged"], 1)
        self.assertEqual(summary["moves_ge_9_5pct"], 1)


if __name__ == "__main__":
    unittest.main()
