import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from china_stock_engine.ifind_supplemental import (
    BoundedClient, FINANCIAL_INDICATOR, PUBLICATION_INDICATOR,
    collect_financials, collect_events, run_collection, safe_failure,
    _document_link, main,
    collect_financials_incremental, _save_batch,
)
from china_stock_engine.ifind_http import IFindHTTPError
from china_stock_engine.storage import read_parquet_file, ArtifactContractError, atomic_write_parquet, atomic_write_json, sha256_file
from china_stock_engine.data_reference import feature_pit_timing, apply_adjustments, load_adjustment_snapshot


class SupplementalTests(unittest.TestCase):
    def test_verified_empty_radar_records_skip_without_provider_or_pointer_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            radar = root / "latest/opportunity_radar_latest.json"
            pointer = root / "latest/supplemental_manifest.json"
            manifest = root / "latest/manifest.json"
            source = "a" * 64
            atomic_write_json(pointer, {"previous": True})
            atomic_write_json(radar, {"trade_date": "2026-11-05", "candidate_union": [],
                                      "source_snapshot_sha256": source})
            meta = {"schema_version": 3, "verified": True, "data_fresh": True,
                    "trade_date": "2026-11-05", "data_reference": {"source_snapshot_sha256": source},
                    "artifacts": {radar.name: {"sha256": sha256_file(radar)}}}
            atomic_write_json(manifest, meta)
            argv = ["--scope", "radar", "--data-dir", str(root)]
            with patch("china_stock_engine.ifind_supplemental.BoundedClient") as client:
                self.assertEqual(main(argv), 0)
                status = json.loads((root / "supplemental_last_attempt.json").read_bytes())
                self.assertEqual(status["state"], "skipped_empty_radar_scope")
                self.assertEqual(status["requests"], [])
                self.assertEqual(json.loads(pointer.read_bytes()), {"previous": True})
                for field, value in (("trade_date", "2026-11-04"), ("data_fresh", False),
                                     ("artifacts", {radar.name: {"sha256": "0" * 64}})):
                    atomic_write_json(manifest, {**meta, field: value})
                    with self.assertRaises(ArtifactContractError):
                        main(argv)
                client.assert_not_called()

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
            frame = read_parquet_file(data_dir / artifact["path"])
            self.assertTrue((pd.to_datetime(frame["known_at"], utc=True) > pd.Timestamp("2026-09-04T23:59:59Z")).all())

    def test_budget_and_failure_do_not_replace_last_valid_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            run_collection(self.client(), data_dir, modules=["financials"], codes=["600000.SH"],
                           trade_date="2026-09-04", as_of_date="2026-09-06")
            target = data_dir / "latest/supplemental_manifest.json"
            previous = target.read_bytes()
            result = run_collection(self.client(max_requests=0), data_dir, modules=["financials", "events"],
                                    codes=["600000.SH"], trade_date="2026-09-04", as_of_date="2026-09-06")
            self.assertFalse(result["ok"])
            self.assertEqual(target.read_bytes(), previous)
            self.assertEqual(len(list((data_dir / "facts/research/financials").glob("*.json"))), 1)
        self.assertNotIn("private-secret", str(safe_failure(IFindHTTPError("private-secret code -4318"))))

    def test_corrupt_manifest_fails_before_any_api_request(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "latest").mkdir()
            (data_dir / "latest/supplemental_manifest.json").write_text('{"schema_version":999}')
            client = self.client()
            with self.assertRaises(ArtifactContractError):
                run_collection(client, data_dir, modules=["financials"], codes=["600000.SH"],
                               trade_date="2026-09-04", as_of_date="2026-09-06")
            self.assertEqual(client.audit, [])

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

    def test_mixed_iso_precision_selects_latest_vintage_only_after_completion(self):
        old = "2026-11-06T21:41:10+08:00"
        new = "2026-11-06T13:41:10.446509+00:00"
        next_instant = "2026-11-06T13:41:10.446510Z"
        factors = pd.DataFrame({
            "thscode": ["600000.SH"] * 4, "trade_date": ["2026-11-05"] * 4,
            "base_date": ["2026-11-04", "2026-11-05", "2026-11-05", "2026-11-05"],
            "adj_factor": [1., 1.1, 1.2, 1.3], "known_at": [old, new, new, "invalid"],
            "collection_completed_at": [old, new, next_instant, "invalid"],
        })
        raw = pd.DataFrame({"trade_date": ["2026-11-04", "2026-11-05"], "thscode": ["600000.SH"] * 2,
                            "close": [10., 10.], "history_verified": [True, True],
                            "first_observed_at": [old, new]})
        for ordered in (factors, factors.iloc[::-1]):
            timing = feature_pit_timing({"collection_completed_at": old,
                                        "configured_decision_cutoff": new}, ordered)
            self.assertEqual(pd.Timestamp(timing["effective_pit_cutoff"]), pd.Timestamp(new))
            early = apply_adjustments(raw, ordered, old)
            self.assertEqual(early.iloc[-1]["adjusted_close"], 10.)
            self.assertFalse(early.iloc[-1]["history_verified"])
            current = apply_adjustments(raw, ordered, new)
            self.assertEqual(current.iloc[-1]["adjusted_close"], 11.)
            self.assertTrue(current["history_verified"].all())

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

    def test_financial_scope_growth_queries_only_new_issuer(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            first = collect_financials(self.client(), ["600000.SH"], "2026-06-30", "2026-09-06")
            _save_batch(data_dir, first)
            client = self.client()
            result = collect_financials_incremental(client, data_dir, ["600000.SH", "300033.SZ"], "2026-06-30", "2026-09-06")
            self.assertEqual(client.audit[0]["data_volume"], 2)
            old = next(row for row in result["records"] if row["thscode"] == "600000.SH")
            self.assertEqual(old, first["records"][0])
            _save_batch(data_dir, result)
            cached_client = self.client()
            again = collect_financials_incremental(cached_client, data_dir, ["600000.SH", "300033.SZ"], "2026-06-30", "2026-09-06")
            self.assertEqual(cached_client.audit, [])
            self.assertEqual(result["records"], again["records"])


if __name__ == "__main__":
    unittest.main()
