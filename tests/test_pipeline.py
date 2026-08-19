from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest

import pandas as pd

from china_stock_engine.pipeline import (
    CollectionConfig,
    collect_and_publish,
    validate_latest,
)


TEST_TEMP_ROOT = Path(__file__).resolve().parents[1] / ".test-tmp"
TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)


def make_test_workspace(name: str) -> Path:
    path = TEST_TEMP_ROOT / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


class FakeClient:
    refresh_token = "sensitive-refresh-token"
    access_token = "sensitive-access-token"

    def __init__(
        self, *, partial: bool = False, fail: bool = False, no_trade: bool = False
    ) -> None:
        self.partial = partial
        self.fail = fail
        self.no_trade = no_trade

    def fetch_trade_calendar(self, trade_date: str, *, offset: int) -> pd.DataFrame:
        calendar_date = "2026-08-17" if self.no_trade else trade_date
        return pd.DataFrame(
            {
                "as_of_date": [trade_date],
                "trade_date": [calendar_date],
                "calendar": ["SSE"],
                "market_code": ["212001"],
                "is_open": [True],
                "source_provider": ["ifind_http"],
                "source_endpoint": ["get_trade_dates"],
            }
        )

    def fetch_universe(self, trade_date: str) -> pd.DataFrame:
        if self.fail:
            raise RuntimeError("upstream unavailable")
        return pd.DataFrame(
            {
                "as_of_date": [trade_date] * 4,
                "thscode": ["600000.SH", "688001.SH", "000001.SZ", "920001.BJ"],
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
        if progress:
            progress(1, 1)
        return pd.DataFrame(
            {
                "as_of_date": [trade_date] * len(codes),
                "thscode": codes,
                "listing_date": ["2000-01-01"] * len(codes),
                "total_shares": [1000000.0] * len(codes),
                "float_a_shares": [800000.0] * len(codes),
                "source_provider": ["ifind_http"] * len(codes),
                "source_endpoint": ["basic_data_service"] * len(codes),
            }
        )

    def fetch_daily_quotes(
        self,
        codes: list[str],
        trade_date: str,
        *,
        batch_size: int,
        request_interval_seconds: float,
        progress=None,
    ) -> pd.DataFrame:
        selected = codes[:1] if self.partial else codes
        if progress:
            progress(1, 1)
        return pd.DataFrame(
            {
                "trade_date": [trade_date] * len(selected),
                "thscode": selected,
                "open": [10.0] * len(selected),
                "high": [11.0] * len(selected),
                "low": [9.0] * len(selected),
                "close": [10.5] * len(selected),
                "pre_close": [10.5 / 1.01] * len(selected),
                "avg_price": [10.2] * len(selected),
                "volume": [1000.0] * len(selected),
                "amount": [10000.0] * len(selected),
                "turnover_ratio": [1.5] * len(selected),
                "change_ratio": [1.0] * len(selected),
                "source_provider": ["ifind_http"] * len(selected),
                "source_endpoint": ["cmd_history_quotation"] * len(selected),
            }
        )


class PipelineTests(unittest.TestCase):
    def config(self, root: Path) -> CollectionConfig:
        return CollectionConfig(
            data_dir=root / "data",
            min_universe_size=4,
            min_quote_coverage=0.75,
            min_reference_coverage=1.0,
            min_extended_field_coverage=1.0,
            reference_batch_size=2,
            quote_batch_size=2,
            request_interval_seconds=0,
            history_limit=3,
            snapshot_limit=2,
        )

    def test_success_promotes_and_validates_snapshot(self) -> None:
        root = make_test_workspace("success")
        result = collect_and_publish(
            FakeClient(), "2026-08-18", config=self.config(root)
        )
        self.assertTrue(result.ok, result.status)
        self.assertTrue((root / "data/latest/manifest.json").exists())
        self.assertTrue((root / "data/snapshots/2026-08-18/daily_quotes.parquet").exists())
        self.assertTrue(
            (root / "data/latest/security_reference.parquet").exists()
        )
        self.assertTrue(
            (root / "data/latest/daily_security_status.parquet").exists()
        )
        self.assertNotIn(b"\r\n", (root / "data/latest/manifest.json").read_bytes())
        self.assertNotIn(
            b"\r\n", (root / "data/latest/market_summary.json").read_bytes()
        )
        ok, payload = validate_latest(
            data_dir=root / "data",
            policy_min_universe_size=4,
            policy_min_quote_coverage=0.75,
        )
        self.assertTrue(ok, payload)

        all_text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in (root / "data").rglob("*")
            if path.is_file() and path.suffix in {".json", ".txt"}
        )
        self.assertNotIn("sensitive-refresh-token", all_text)
        self.assertNotIn("sensitive-access-token", all_text)

    def test_quality_failure_preserves_absence_of_latest(self) -> None:
        root = make_test_workspace("quality-failure")
        result = collect_and_publish(
            FakeClient(partial=True), "2026-08-18", config=self.config(root)
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status["state"], "failed_quality")
        self.assertFalse((root / "data/latest").exists())
        status = json.loads(
            (root / "data/last_run_status.json").read_text(encoding="utf-8")
        )
        self.assertFalse(status["data_fresh"])

    def test_identical_rerun_does_not_rewrite_formal_artifacts(self) -> None:
        root = make_test_workspace("idempotent")
        first = collect_and_publish(
            FakeClient(), "2026-08-18", config=self.config(root)
        )
        self.assertTrue(first.ok)
        manifest_path = root / "data/latest/manifest.json"
        status_path = root / "data/last_run_status.json"
        manifest_before = manifest_path.read_bytes()
        status_before = status_path.read_bytes()

        second = collect_and_publish(
            FakeClient(), "2026-08-18", config=self.config(root)
        )
        self.assertTrue(second.ok)
        self.assertEqual(second.status["state"], "success_unchanged")
        self.assertEqual(manifest_before, manifest_path.read_bytes())
        self.assertEqual(status_before, status_path.read_bytes())

    def test_identical_success_clears_previous_failure_status(self) -> None:
        root = make_test_workspace("failure-recovery")
        first = collect_and_publish(
            FakeClient(), "2026-08-18", config=self.config(root)
        )
        self.assertTrue(first.ok)
        status_path = root / "data/last_run_status.json"
        status_path.write_text(
            json.dumps(
                {
                    "state": "failed_collection",
                    "requested_trade_date": "2026-08-18",
                    "data_fresh": False,
                }
            ),
            encoding="utf-8",
        )

        recovered = collect_and_publish(
            FakeClient(), "2026-08-18", config=self.config(root)
        )
        self.assertTrue(recovered.ok)
        self.assertEqual(recovered.status["state"], "success_unchanged")
        stored = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["state"], "success_unchanged")
        self.assertTrue(stored["data_fresh"])

    def test_collection_failure_records_sanitized_status(self) -> None:
        root = make_test_workspace("collection-failure")
        result = collect_and_publish(
            FakeClient(fail=True), "2026-08-18", config=self.config(root)
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status["state"], "failed_collection")
        text = (root / "data/last_run_status.json").read_text(encoding="utf-8")
        self.assertNotIn("sensitive-refresh-token", text)
        self.assertNotIn("sensitive-access-token", text)

    def test_no_trade_date_is_explicit_and_does_not_promote(self) -> None:
        root = make_test_workspace("no-trade-date")
        result = collect_and_publish(
            FakeClient(no_trade=True), "2026-08-18", config=self.config(root)
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status["state"], "no_trade_date")
        self.assertFalse(result.status["data_fresh"])
        self.assertFalse((root / "data/latest").exists())


if __name__ == "__main__":
    unittest.main()
