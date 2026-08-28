from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest

import pandas as pd

from china_stock_engine.report_dashboard import build_data_reference_dashboard
from china_stock_engine.data_reference import STOCK_STATE_COLUMNS, build_data_reference


TEST_ROOT = Path(__file__).resolve().parents[1] / ".test-tmp" / "data-reference"


class DataReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        if TEST_ROOT.exists():
            shutil.rmtree(TEST_ROOT)
        (TEST_ROOT / "data" / "latest").mkdir(parents=True)

    def test_contract_and_html_are_factual_and_share_source_hash(self) -> None:
        state = pd.DataFrame(
            [
                {
                    **{field: None for field in STOCK_STATE_COLUMNS},
                    "schema_version": 3,
                    "trade_date": "2026-08-20",
                    "data_cutoff_time": "2026-08-20T20:15:00+08:00",
                    "thscode": "600000.SH",
                    "security_name": "浦发银行",
                    "sw1_code": "801780",
                    "sw1_name": "银行",
                    "return_20d_pct": 2.5,
                    "adt20": 1_000_000_000.0,
                    "tradability_state": "clear",
                    "tradability_reason_codes": [],
                    "source_snapshot_sha256": "source-hash",
                }
            ]
        )
        manifest = {
            "schema_version": 2,
            "trade_date": "2026-08-20",
            "collected_at_utc": "2026-08-20T10:30:00+00:00",
            "provider": "ifind_http",
            "verified": True,
            "data_fresh": True,
            "quality": {"ok": True, "metrics": {"quote_coverage": 1.0}},
            "quality_thresholds": {"min_quote_coverage": 0.98},
            "artifacts": {
                "daily_quotes.parquet": {
                    "rows": 1,
                    "sha256": "a" * 64,
                },
                "market_summary.json": {"sha256": "b" * 64},
            },
        }
        readiness = {
            "history": {"state": "ready", "sessions": 20},
            "stock_state": {"state": "ready", "rows": 1},
        }
        reference = build_data_reference(
            manifest,
            {
                "advancers": 1,
                "decliners": 0,
                "unchanged": 0,
                "equal_weight_change_pct": 1.0,
                "median_change_pct": 1.0,
                "total_amount": 1_000_000_000.0,
                "quoted_securities": 1,
                "observation_state_counts": {"traded": 1},
            },
            state,
            readiness,
            "source-hash",
        )
        serialized = json.dumps(reference, ensure_ascii=False).lower()
        for forbidden in (
            "candidate",
            "no_trade",
            "recommendation",
            "regime",
            "total_score",
            "outcomes",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(reference["document_type"], "a_share_data_reference")
        self.assertEqual(reference["coverage"]["stock_state_rows"], 1)
        self.assertEqual(
            reference["as_of"]["configured_decision_cutoff"],
            "2026-08-20T20:15:00+08:00",
        )
        self.assertEqual(
            reference["as_of"]["effective_pit_cutoff"],
            "2026-08-20T10:30:00+00:00",
        )
        self.assertLessEqual(
            pd.Timestamp(reference["as_of"]["effective_pit_cutoff"]),
            pd.Timestamp(reference["as_of"]["collection_completed_at"]),
        )

        manifest["data_reference"] = {
            "source_snapshot_sha256": "source-hash"
        }
        latest = TEST_ROOT / "data" / "latest"
        (latest / "data_reference_latest.json").write_text(
            json.dumps(reference, ensure_ascii=False), encoding="utf-8"
        )
        (latest / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        output = build_data_reference_dashboard(
            TEST_ROOT / "data", latest / "data_reference.html"
        )
        page = output.read_text(encoding="utf-8")
        self.assertIn("中国股票数据参考", page)
        self.assertIn("逐股字段覆盖率", page)
        self.assertIn("source-hash", page)
        self.assertNotIn("投资建议", page)


if __name__ == "__main__":
    unittest.main()
