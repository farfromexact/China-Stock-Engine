from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from china_stock_engine.ifind_supplemental import (
    BoundedClient, FINANCIAL_INDICATOR, PUBLICATION_INDICATOR,
    collect_financials, collect_events, run_collection, safe_failure,
    _document_link,
)
from china_stock_engine.ifind_http import IFindHTTPError
from china_stock_engine.storage import ArtifactContractError, atomic_write_parquet
from china_stock_engine.data_reference import feature_pit_timing, apply_adjustments, load_adjustment_snapshot


class SupplementalTests(unittest.TestCase):
    def transport(self, url, headers, payload, timeout):
        endpoint = url.rsplit("/", 1)[-1]
        codes = payload["codes"].split(",")
        if endpoint == "basic_data_service":
            return {"errorcode": 0, "dataVol": 2 * len(codes), "tables": [
                {"thscode": code, "table": {FINANCIAL_INDICATOR: [123.0], PUBLICATION_INDICATOR: ["20260820"]}}
                for code in codes]}
        if endpoint == "report_query":
            return {"errorcode": 0, "tables": [{"table": {
                "thscode": codes, "ctime": ["2026-09-01 18:00:00"] * len(codes),
                "reportDate": ["2026-09-02"] * len(codes), "seq": list(range(len(codes))),
                "reportTitle": ["Synthetic announcement"] * len(codes),
                "pdfURL": ["https://example.org/filing.pdf"] * len(codes),
            }}]}
        if endpoint == "cmd_history_quotation":
            self.assertEqual(payload["functionpara"]["CPS"], "2")
            return {"errorcode": 0, "tables": [{"thscode": code, "time": ["2026-09-04"],
                     "table": {"close": [10.]}} for code in codes]}
        raise AssertionError(endpoint)

    def client(self, **kwargs):
        return BoundedClient(access_token="synthetic-private", transport=self.transport, **kwargs)

    def test_dated_financial_facts_and_unknown_fields(self):
        result = collect_financials(self.client(), ["600000.SH"], "2026-06-30", "2026-09-06")
        row = result["records"][0]
        self.assertEqual(row["values"]["net_profit_parent"], 123.)
        self.assertIsNone(row["values"]["revenue"])
        self.assertEqual(row["published_at"], "2026-08-20T15:59:59+00:00")
        self.assertEqual(row["known_at"], result["collection_completed_at"])
        self.assertFalse(result["coverage"]["complete"])

    def test_announcement_status_is_unknown_not_completed(self):
        result = collect_events(self.client(), ["600000.SH"], "2026-09-01", "2026-09-04")
        row = result["records"][0]
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["event_type"], "filing")
        self.assertIsNone(row["document_sha256"])
        self.assertFalse(result["coverage"]["complete"])

    def test_rerun_reuses_checkpoints_and_adjustments_do_not_refetch_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            atomic_write_parquet(data_dir / "facts/market/trade_date=2026-09-04/daily_quotes.parquet",
                                 pd.DataFrame({"thscode": ["600000.SH"], "trade_date": ["2026-09-04"], "close": [10.]}))
            kwargs = dict(modules=["financials", "events", "adjustment"], codes=["600000.SH"],
                          trade_date="2026-09-04", as_of_date="2026-09-06")
            first_client = self.client()
            first = run_collection(first_client, data_dir, **kwargs)
            self.assertTrue(first["ok"], first)
            self.assertEqual(len(first_client.audit), 3)
            pointer = (data_dir / "latest/supplemental_manifest.json").read_bytes()
            other_client = self.client()
            second = run_collection(other_client, data_dir, **kwargs)
            self.assertTrue(second["ok"])
            self.assertEqual(other_client.audit, [])
            self.assertEqual(pointer, (data_dir / "latest/supplemental_manifest.json").read_bytes())
            artifact = first["modules"]["adjustment"]
            frame = pd.read_parquet(data_dir / artifact["path"])
            self.assertTrue((pd.to_datetime(frame["known_at"], utc=True) > pd.Timestamp("2026-09-04T23:59:59Z")).all())

    def test_budget_and_failure_do_not_replace_last_valid_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "latest").mkdir()
            target = data_dir / "latest/supplemental_manifest.json"
            target.write_text("last valid sentinel", encoding="utf-8")
            result = run_collection(self.client(max_requests=1), data_dir, modules=["financials", "events"],
                                    codes=["600000.SH"], trade_date="2026-09-04", as_of_date="2026-09-06")
            self.assertFalse(result["ok"])
            self.assertEqual(target.read_text(), "last valid sentinel")
            self.assertEqual(len(list((data_dir / "facts/research/financials").glob("*.json"))), 1)
        self.assertNotIn("private-secret", str(safe_failure(IFindHTTPError("private-secret code -4318"))))

    def test_feature_completion_includes_same_day_late_factor_not_future_day(self):
        timing = {"collection_started_at": "2026-09-04T10:00:00Z",
                  "collection_completed_at": "2026-09-04T10:30:00Z",
                  "effective_pit_cutoff": "2026-09-04T10:30:00Z",
                  "configured_decision_cutoff": "2026-09-04T12:15:00Z"}
        factors = pd.DataFrame({"trade_date": ["2026-09-04"] * 2,
                                "thscode": ["600000.SH"] * 2, "adj_factor": [1., 2.],
                                "effective_at": ["2026-09-04"] * 2, "published_at": [None] * 2,
                                "known_at": ["2026-09-04T11:00:00Z", "2026-09-06T11:00:00Z"],
                                "collection_completed_at": ["2026-09-04T11:00:00Z", "2026-09-06T11:00:00Z"]})
        adjusted_timing = feature_pit_timing(timing, factors)
        self.assertEqual(timing["collection_completed_at"], "2026-09-04T10:30:00Z")
        self.assertEqual(adjusted_timing["effective_pit_cutoff"], "2026-09-04T11:00:00+00:00")
        raw = pd.DataFrame({"trade_date": ["2026-09-04"], "thscode": ["600000.SH"], "close": [10.]})
        result = apply_adjustments(raw, factors, adjusted_timing["effective_pit_cutoff"])
        self.assertEqual(result.iloc[0]["adjusted_close"], 10.)
        self.assertEqual(feature_pit_timing(timing, factors.iloc[1:]), timing)

    def test_adjustment_vintage_date_not_backdated_to_price_date(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            facts = pd.DataFrame({"trade_date": ["2026-09-04"], "thscode": ["600000.SH"], "adj_factor": [1.]})
            atomic_write_parquet(data_dir / "facts/adjustment/as_of_date=2026-09-06/vintage=synthetic/adjustment_factors.parquet", facts)
            self.assertTrue(load_adjustment_snapshot(data_dir, "2026-09-04").empty)
            self.assertEqual(len(load_adjustment_snapshot(data_dir, "2026-09-07")), 1)

    def test_authorized_access_renewal_is_once_and_refresh_never_changes(self):
        calls = []
        def transport(url, headers, payload, timeout):
            calls.append(url)
            self.assertTrue(url.endswith("/update_access_token"))
            self.assertEqual(headers, {"refresh_token": "synthetic-refresh"})
            return {"errorcode": 0, "data": {"access_token": "synthetic-new-access"}}
        client = BoundedClient(refresh_token="synthetic-refresh", transport=transport)
        client.renew_access_token_once()
        self.assertEqual(client.refresh_token, "synthetic-refresh")
        self.assertEqual(client.get_access_token(), "synthetic-new-access")
        with self.assertRaises(IFindHTTPError):
            client.renew_access_token_once()
        self.assertEqual(len(calls), 1)

    def test_authenticated_announcement_link_never_persists_secret(self):
        value = _document_link("https://example.org/filing?seq=42&access_token=synthetic-secret")
        self.assertNotIn("synthetic-secret", str(value))
        self.assertEqual(value["document_access"], "authenticated_link_omitted")
        self.assertEqual(value["source_url_kind"], "provider_query_documentation")


if __name__ == "__main__":
    unittest.main()
