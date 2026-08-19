from __future__ import annotations

import unittest

import pandas as pd

from china_stock_engine.quality import (
    board_from_code,
    build_daily_security_status,
    build_market_summary,
    normalize_quotes,
    normalize_security_reference,
    normalize_trade_calendar,
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
    closes = [10.5, 20.5, 30.5, 40.5]
    changes = [1.0, -2.0, 0.0, 10.0]
    return pd.DataFrame(
        {
            "trade_date": ["2026-08-18"] * 4,
            "thscode": ["600000.SH", "688001.SH", "000001.SZ", "920001.BJ"],
            "open": [10, 20, 30, 40],
            "high": [11, 21, 31, 41],
            "low": [9, 19, 29, 39],
            "close": closes,
            "pre_close": [
                close / (1 + change / 100)
                for close, change in zip(closes, changes, strict=True)
            ],
            "avg_price": [10.2, 20.2, 30.2, 40.2],
            "volume": [100, 200, 300, 400],
            "amount": [1000, 2000, 3000, 4000],
            "turnover_ratio": [1.0, 2.0, 3.0, 4.0],
            "change_ratio": changes,
            "source_provider": ["ifind_http"] * 4,
            "source_endpoint": ["cmd_history_quotation"] * 4,
        }
    )


def reference_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "as_of_date": ["2026-08-18"] * 4,
            "thscode": ["600000.SH", "688001.SH", "000001.SZ", "920001.BJ"],
            "listing_date": ["1999-11-10", "2019-07-22", "1991-04-03", "2024-01-01"],
            "total_shares": [1000, 2000, 3000, 4000],
            "float_a_shares": [800, 1500, 2500, 3000],
            "source_provider": ["ifind_http"] * 4,
            "source_endpoint": ["basic_data_service"] * 4,
        }
    )


def calendar_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "as_of_date": ["2026-08-18"],
            "trade_date": ["2026-08-18"],
            "calendar": ["SSE"],
            "market_code": ["212001"],
            "is_open": [True],
            "source_provider": ["ifind_http"],
            "source_endpoint": ["get_trade_dates"],
        }
    )


class QualityTests(unittest.TestCase):
    def test_board_classification(self) -> None:
        self.assertEqual(board_from_code("688001.SH"), "STAR")
        self.assertEqual(board_from_code("300001.SZ"), "CHINEXT")
        self.assertEqual(board_from_code("920001.BJ"), "BSE")

    def test_valid_snapshot_passes(self) -> None:
        universe = normalize_universe(universe_frame())
        reference = normalize_security_reference(reference_frame(), universe)
        quotes = normalize_quotes(quote_frame(), universe)
        calendar = normalize_trade_calendar(calendar_frame())
        status = build_daily_security_status(universe, quotes, "2026-08-18")
        report = validate_data(
            universe,
            reference,
            quotes,
            calendar,
            status,
            "2026-08-18",
            min_universe_size=4,
            min_quote_coverage=1.0,
            min_reference_coverage=1.0,
            min_extended_field_coverage=1.0,
        )
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.metrics["quote_count"], 4)

    def test_date_and_ohlc_failures_are_explicit(self) -> None:
        universe = normalize_universe(universe_frame())
        reference = normalize_security_reference(reference_frame(), universe)
        raw_quotes = quote_frame()
        raw_quotes.loc[0, "trade_date"] = "2026-08-17"
        raw_quotes.loc[1, "high"] = 18
        quotes = normalize_quotes(raw_quotes, universe)
        calendar = normalize_trade_calendar(calendar_frame())
        status = build_daily_security_status(universe, quotes, "2026-08-18")
        report = validate_data(
            universe,
            reference,
            quotes,
            calendar,
            status,
            "2026-08-18",
            min_universe_size=4,
            min_quote_coverage=1.0,
            min_reference_coverage=1.0,
            min_extended_field_coverage=1.0,
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("source dates" in error for error in report.errors))
        self.assertTrue(any("invalid OHLC" in error for error in report.errors))

    def test_market_summary_uses_non_null_changes(self) -> None:
        universe = normalize_universe(universe_frame())
        quotes = normalize_quotes(quote_frame(), universe)
        status = build_daily_security_status(universe, quotes, "2026-08-18")
        summary = build_market_summary(quotes, "2026-08-18", status)
        self.assertEqual(summary["advancers"], 2)
        self.assertEqual(summary["decliners"], 1)
        self.assertEqual(summary["unchanged"], 1)
        self.assertEqual(summary["moves_ge_9_5pct"], 1)
        self.assertEqual(summary["observation_state_counts"]["traded"], 4)

    def test_missing_quote_is_observation_not_suspension_claim(self) -> None:
        universe = normalize_universe(universe_frame())
        quotes = normalize_quotes(quote_frame().iloc[:3], universe)
        status = build_daily_security_status(universe, quotes, "2026-08-18")
        missing = status.loc[status["thscode"] == "920001.BJ"].iloc[0]
        self.assertFalse(missing["quote_row_present"])
        self.assertEqual(missing["observation_state"], "no_quote_observed")

    def test_sub_micro_price_rounding_does_not_fail_range_gate(self) -> None:
        universe = normalize_universe(universe_frame())
        reference = normalize_security_reference(reference_frame(), universe)
        raw_quotes = quote_frame()
        raw_quotes[["open", "high", "low", "close"]] = raw_quotes[
            ["open", "high", "low", "close"]
        ].astype(float)
        raw_quotes.loc[0, ["open", "high", "low", "close"]] = 7.82
        raw_quotes.loc[0, "avg_price"] = 7.8199999870177
        raw_quotes.loc[0, "pre_close"] = 7.82 / 1.01
        quotes = normalize_quotes(raw_quotes, universe)
        calendar = normalize_trade_calendar(calendar_frame())
        status = build_daily_security_status(universe, quotes, "2026-08-18")
        report = validate_data(
            universe,
            reference,
            quotes,
            calendar,
            status,
            "2026-08-18",
            min_universe_size=4,
            min_quote_coverage=1.0,
            min_reference_coverage=1.0,
            min_extended_field_coverage=1.0,
        )
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.metrics["average_price_outside_range_rows"], 0)


if __name__ == "__main__":
    unittest.main()
