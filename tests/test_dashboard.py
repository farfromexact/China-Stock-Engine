from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest

import pandas as pd

from china_stock_engine.dashboard import build_dashboard


TEST_ROOT = Path(__file__).resolve().parents[1] / ".test-tmp" / "dashboard"


def write_fixture(*, verified: bool = True) -> Path:
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    latest = TEST_ROOT / "data" / "latest"
    latest.mkdir(parents=True)
    universe = pd.DataFrame(
        {
            "as_of_date": ["2026-08-19", "2026-08-19"],
            "thscode": ["600000.SH", "000001.SZ"],
            "security_name": ["浦发银行", "平安银行"],
            "security_name_in_time": ["浦发银行", "平安银行"],
            "exchange": ["SSE", "SZSE"],
            "board": ["SSE_MAIN", "SZSE_MAIN"],
        }
    )
    reference = pd.DataFrame(
        {
            "as_of_date": ["2026-08-19", "2026-08-19"],
            "thscode": ["600000.SH", "000001.SZ"],
            "security_name": ["浦发银行", "平安银行"],
            "exchange": ["SSE", "SZSE"],
            "board": ["SSE_MAIN", "SZSE_MAIN"],
            "listing_date": ["1999-11-10", "1991-04-03"],
            "total_shares": [1000000.0, 2000000.0],
            "float_a_shares": [800000.0, 1800000.0],
            "source_provider": ["ifind_http", "ifind_http"],
            "source_endpoint": ["basic_data_service", "basic_data_service"],
        }
    )
    quotes = pd.DataFrame(
        {
            "trade_date": ["2026-08-19", "2026-08-19"],
            "thscode": ["600000.SH", "000001.SZ"],
            "security_name": ["浦发银行", "平安银行"],
            "exchange": ["SSE", "SZSE"],
            "board": ["SSE_MAIN", "SZSE_MAIN"],
            "open": [10.0, 20.0],
            "high": [11.0, 21.0],
            "low": [9.0, 19.0],
            "close": [10.5, 19.5],
            "pre_close": [10.0, 20.0],
            "avg_price": [10.2, 19.8],
            "volume": [1000.0, 2000.0],
            "amount": [10000.0, 40000.0],
            "turnover_ratio": [1.0, 2.0],
            "change_ratio": [5.0, -2.5],
            "source_provider": ["ifind_http", "ifind_http"],
            "source_endpoint": ["cmd_history_quotation"] * 2,
        }
    )
    calendar = pd.DataFrame(
        {
            "as_of_date": ["2026-08-19"],
            "trade_date": ["2026-08-19"],
            "calendar": ["SSE"],
            "market_code": ["212001"],
            "is_open": [True],
            "source_provider": ["ifind_http"],
            "source_endpoint": ["get_trade_dates"],
        }
    )
    status = pd.DataFrame(
        {
            "trade_date": ["2026-08-19", "2026-08-19"],
            "thscode": ["600000.SH", "000001.SZ"],
            "security_name": ["浦发银行", "平安银行"],
            "exchange": ["SSE", "SZSE"],
            "board": ["SSE_MAIN", "SZSE_MAIN"],
            "quote_row_present": [True, True],
            "has_price_observation": [True, True],
            "has_turnover_observation": [True, True],
            "observation_state": ["traded", "traded"],
            "source_provider": ["derived", "derived"],
            "source_endpoint": ["universe+cmd_history_quotation"] * 2,
        }
    )
    for name, frame in {
        "universe.parquet": universe,
        "security_reference.parquet": reference,
        "daily_quotes.parquet": quotes,
        "trading_calendar.parquet": calendar,
        "daily_security_status.parquet": status,
    }.items():
        frame.to_parquet(latest / name, index=False)
    manifest = {
        "schema_version": 2,
        "trade_date": "2026-08-19",
        "verified": verified,
        "data_fresh": True,
        "quality": {
            "ok": True,
            "errors": [],
            "metrics": {
                "universe_count": 2,
                "quote_count": 2,
                "quote_coverage": 1.0,
                "reference_coverage": 1.0,
            },
        },
    }
    summary = {
        "trade_date": "2026-08-19",
        "quoted_securities": 2,
        "advancers": 1,
        "decliners": 1,
        "unchanged": 0,
        "equal_weight_change_pct": 1.25,
        "median_change_pct": 1.25,
        "total_amount": 50000.0,
    }
    (latest / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (latest / "market_summary.json").write_text(
        json.dumps(summary), encoding="utf-8"
    )
    return TEST_ROOT / "data"


class DashboardTests(unittest.TestCase):
    def test_builds_self_contained_dashboard(self) -> None:
        data_dir = write_fixture()
        output = TEST_ROOT / "dashboard.html"
        built = build_dashboard(data_dir, output)
        text = built.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("<!doctype html>"))
        self.assertIn("China Stock Engine", text)
        self.assertIn("600000.SH", text)
        self.assertIn("浦发银行", text)
        self.assertIn("逐证券数据浏览", text)
        self.assertNotIn("<script src=", text)
        self.assertNotIn("refresh_token", text)

    def test_rejects_unverified_snapshot(self) -> None:
        data_dir = write_fixture(verified=False)
        with self.assertRaises(ValueError):
            build_dashboard(data_dir, TEST_ROOT / "dashboard.html")


if __name__ == "__main__":
    unittest.main()
