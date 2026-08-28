from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import pandas as pd

from china_stock_engine.cli import main


TEST_ROOT = Path(__file__).resolve().parents[1] / ".test-tmp" / "cli-backfill"


class RangeFakeClient:
    refresh_token = "fake-secret"
    access_token = "fake-access"

    def __init__(self) -> None:
        self.dates = ["2026-08-18", "2026-08-19", "2026-08-20"]
        self.codes = ["600000.SH", "688001.SH", "000001.SZ", "920001.BJ"]
        self.history_calls = 0

    def fetch_trade_calendar(self, as_of_date: str, *, offset: int) -> pd.DataFrame:
        del offset
        return pd.DataFrame(
            {
                "as_of_date": [as_of_date] * len(self.dates),
                "trade_date": self.dates,
                "calendar": ["SSE"] * len(self.dates),
                "market_code": ["212001"] * len(self.dates),
                "is_open": [True] * len(self.dates),
                "source_provider": ["ifind_http"] * len(self.dates),
                "source_endpoint": ["get_trade_dates"] * len(self.dates),
            }
        )

    def fetch_universe(self, trade_date: str) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "as_of_date": [trade_date] * len(self.codes),
                "thscode": self.codes,
                "security_name": ["A", "B", "C", "D"],
                "security_name_in_time": ["A", "B", "C", "D"],
            }
        )

    def fetch_security_reference(
        self,
        codes: list[str],
        trade_date: str,
        *,
        batch_size: int,
        request_interval_seconds: float,
        progress=None,
    ) -> pd.DataFrame:
        del batch_size, request_interval_seconds, progress
        return pd.DataFrame(
            {
                "as_of_date": [trade_date] * len(codes),
                "thscode": codes,
                "listing_date": ["2000-01-01"] * len(codes),
                "total_shares": [1_000_000.0] * len(codes),
                "float_a_shares": [800_000.0] * len(codes),
                "source_provider": ["ifind_http"] * len(codes),
                "source_endpoint": ["basic_data_service"] * len(codes),
            }
        )

    def fetch_daily_history(
        self,
        codes: list[str],
        start_date: str,
        end_date: str,
        *,
        batch_size: int,
        request_interval_seconds: float,
        progress=None,
    ) -> pd.DataFrame:
        del batch_size, request_interval_seconds
        self.history_calls += 1
        selected_dates = [
            value for value in self.dates if start_date <= value <= end_date
        ]
        rows = []
        for date_index, trade_date in enumerate(selected_dates):
            for code in codes:
                close = 10.0 + date_index * 0.1
                rows.append(
                    {
                        "trade_date": trade_date,
                        "thscode": code,
                        "open": close - 0.05,
                        "high": close + 0.1,
                        "low": close - 0.1,
                        "close": close,
                        "pre_close": close / 1.01,
                        "avg_price": close - 0.02,
                        "volume": 1_000.0,
                        "amount": 10_000.0,
                        "turnover_ratio": 1.5,
                        "change_ratio": 1.0,
                        "source_provider": "ifind_http",
                        "source_endpoint": "cmd_history_quotation",
                    }
                )
        if progress:
            progress(1, 1)
        return pd.DataFrame(rows)


class CLIBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        if TEST_ROOT.exists():
            shutil.rmtree(TEST_ROOT)
        TEST_ROOT.mkdir(parents=True)

    def test_backfill_prefetches_range_and_promotes_only_tail(self) -> None:
        client = RangeFakeClient()
        data_dir = TEST_ROOT / "data"
        argv = [
            "backfill",
            "--start",
            "2026-08-18",
            "--end",
            "2026-08-20",
            "--data-dir",
            str(data_dir),
            "--min-universe-size",
            "4",
            "--min-quote-coverage",
            "1",
            "--min-reference-coverage",
            "1",
            "--min-extended-field-coverage",
            "1",
        ]
        with patch("china_stock_engine.cli._client", return_value=client):
            with redirect_stdout(io.StringIO()):
                result = main(argv)

        self.assertEqual(result, 0)
        self.assertEqual(client.history_calls, 1)
        manifest = json.loads(
            (data_dir / "latest" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["trade_date"], "2026-08-20")
        self.assertTrue((data_dir / "latest/data_reference_latest.json").exists())
        self.assertEqual(
            len(list((data_dir / "facts" / "market").glob("trade_date=*"))),
            3,
        )
        status = json.loads(
            (data_dir / "last_run_status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(status["state"], "success_backfill")

    def test_backfill_reuses_normalized_reference_and_quote_partitions(self) -> None:
        data_dir = TEST_ROOT / "data"
        argv = [
            "backfill",
            "--start",
            "2026-08-18",
            "--end",
            "2026-08-20",
            "--data-dir",
            str(data_dir),
            "--min-universe-size",
            "4",
            "--min-quote-coverage",
            "1",
            "--min-reference-coverage",
            "1",
            "--min-extended-field-coverage",
            "1",
        ]
        first = RangeFakeClient()
        with patch("china_stock_engine.cli._client", return_value=first):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(argv), 0)
        self.assertEqual(first.history_calls, 1)

        second = RangeFakeClient()
        with patch("china_stock_engine.cli._client", return_value=second):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(argv), 0)

        self.assertEqual(second.history_calls, 0)


if __name__ == "__main__":
    unittest.main()
