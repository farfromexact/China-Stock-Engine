import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from china_stock_engine.ifind_enrichment import (
    load_config, run_enrichment, report_periods, _decode, _templates,
)
from china_stock_engine.ifind_supplemental import BoundedClient
from china_stock_engine.storage import ArtifactContractError, atomic_write_json, atomic_write_parquet, json_sha256
from china_stock_engine.research_inputs import normalize_research_batch, financial_features
from china_stock_engine.research_metrics import forecast_features, peer_valuation_percentiles
from test_research_inputs import batch, record


def profile(module):
    fields = {"thscode": {"source": "thscode", "type": "text"}}
    if module == "financial_history":
        fields.update(published_at={"source": "publication", "type": "text"},
                      revenue={"source": "revenue", "type": "number", "scale": 10000},
                      net_profit_parent={"source": "profit", "type": "number"})
    elif module == "tradability":
        fields.update(is_st={"source": "st", "type": "boolean", "enum": {"Y": True, "N": False}},
                      is_suspended={"source": "suspend", "type": "boolean", "enum": {"Y": True, "N": False}})
    elif module == "index_prices":
        fields.update(trade_date={"source": "time", "type": "text"}, close={"source": "close", "type": "number"})
    else:
        fields.update(sw1_code={"source": "sw1", "type": "text"}, sw1_name={"source": "sw1name", "type": "text"})
    return {"endpoint": "cmd_history_quotation" if module == "index_prices" else "basic_data_service",
            "request": {"codes": "{codes}", "period": "{report_period}", "start": "{start}", "end": "{end}"},
            "fields": fields, "source_documentation": "https://example.org/synthetic-catalog",
            "scope_codes": ["000300.SH"] if module == "index_prices" else None,
            "ttl_days": 7 if module == "financial_history" else 0}


def config(module="financial_history", periods=2):
    return {"schema_version": 1, "publication_permitted": True, "financial_periods": periods,
            "profiles": {module: profile(module)}}


class EnrichmentTests(unittest.TestCase):
    def client(self, max_requests=24):
        def transport(url, headers, payload, timeout):
            codes = payload["codes"].split(",")
            if url.endswith("cmd_history_quotation"):
                dates = pd.bdate_range(payload["start"], payload["end"]).strftime("%Y-%m-%d").tolist()
                return {"errorcode": 0, "tables": [{"thscode": code, "time": dates, "table": {"close": [10.] * len(dates)}} for code in codes]}
            return {"errorcode": 0, "dataVol": 3 * len(codes), "tables": [{"thscode": code, "table": {
                "publication": ["20260820"], "revenue": [100.], "profit": [10.],
                "st": ["N"], "suspend": ["N"], "sw1": ["test-industry"], "sw1name": ["Synthetic industry"]}}
                for code in codes]}
        return BoundedClient(access_token="synthetic-only", transport=transport, max_requests=max_requests)

    def test_configuration_requires_permission_and_rejects_guessed_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            expected = config()
            atomic_write_json(path, expected)
            self.assertEqual(load_config(path), expected)
            expected["publication_permitted"] = False
            atomic_write_json(path, expected)
            with self.assertRaises(ArtifactContractError):
                load_config(path)
        with self.assertRaises(ArtifactContractError):
            _templates({"codes": "{codes.__class__}"})

    def test_nullable_booleans_and_unit_scaling(self):
        fields = profile("tradability")["fields"]
        rows = _decode(pd.DataFrame({"thscode": ["600000.SH"], "st": [None], "suspend": ["N"]}), fields)
        self.assertIsNone(rows[0]["is_st"])
        self.assertIs(rows[0]["is_suspended"], False)
        with self.assertRaises(ArtifactContractError):
            _decode(pd.DataFrame({"thscode": ["600000.SH"], "st": ["???"], "suspend": ["N"]}), fields)

    def test_daily_requires_matching_canary_without_api(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.client()
            result = run_enrichment(client, Path(directory), config(), ["600000.SH"], "2026-09-04", "2026-09-07")
            self.assertFalse(result["ok"])
            self.assertEqual(result["modules"]["financial_history"]["reason"], "matching_live_canary_required")
            self.assertEqual(client.audit, [])

    def test_canary_history_scope_growth_and_zero_api_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config()
            first = self.client()
            result = run_enrichment(first, root, cfg, ["600000.SH"], "2026-09-04", "2026-09-07", canary=True)
            self.assertTrue(result["ok"], result)
            self.assertEqual(len(first.audit), 2)
            client = self.client()
            second = run_enrichment(client, root, cfg, ["600000.SH", "300033.SZ"], "2026-09-04", "2026-09-07")
            self.assertEqual(sum(call["data_volume"] for call in client.audit), 6)
            self.assertTrue(second["ok"], second)
            before = (root / "latest/enrichment_manifest.json").read_bytes()
            replay = self.client(max_requests=0)
            result = run_enrichment(replay, root, cfg, ["600000.SH", "300033.SZ"], "2026-09-04", "2026-09-07")
            self.assertTrue(result["ok"], result)
            self.assertEqual(before, (root / "latest/enrichment_manifest.json").read_bytes())
            self.assertEqual(replay.audit, [])
            for path in (root / "facts/research/financials").glob("*.json"):
                import json
                item = normalize_research_batch(json.loads(path.read_text()))
                self.assertEqual(item["records"][0]["values"]["revenue"], 1000000.)

    def test_failure_preserves_last_valid_and_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_enrichment(self.client(), root, config(), ["600000.SH"], "2026-09-04", "2026-09-07", canary=True)
            target = root / "latest/enrichment_manifest.json"
            before = target.read_bytes()
            result = run_enrichment(self.client(max_requests=0), root, config(), ["300033.SZ"], "2026-09-04", "2026-09-07")
            self.assertFalse(result["ok"])
            self.assertEqual(target.read_bytes(), before)
            atomic_write_json(target, {"schema_version": 999})
            client = self.client()
            with self.assertRaises(ArtifactContractError):
                run_enrichment(client, root, config(), ["600000.SH"], "2026-09-04", "2026-09-07")
            self.assertEqual(client.audit, [])

    def test_index_only_fetches_missing_cached_market_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for day in ["2026-09-03", "2026-09-04"]:
                atomic_write_parquet(root / f"facts/market/trade_date={day}/daily_quotes.parquet", pd.DataFrame({"close": [10]}))
            cfg = config("index_prices")
            result = run_enrichment(self.client(), root, cfg, ["600000.SH"], "2026-09-04", "2026-09-07", canary=True)
            self.assertTrue(result["ok"], result)
            client = self.client(max_requests=0)
            self.assertTrue(run_enrichment(client, root, cfg, ["300033.SZ"], "2026-09-04", "2026-09-07")["ok"])
            self.assertEqual(client.audit, [])
            atomic_write_parquet(root / "facts/market/trade_date=2026-09-07/daily_quotes.parquet", pd.DataFrame({"close": [10]}))
            client = self.client()
            result = run_enrichment(client, root, cfg, ["300033.SZ"], "2026-09-07", "2026-09-07")
            self.assertEqual(len(client.audit), 1)
            self.assertTrue(result["ok"], result)

    def test_report_period_target_is_independent_of_market_sessions(self):
        self.assertEqual(report_periods("2026-09-07", 8), ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31", "2024-12-31", "2024-09-30"])

    def test_legacy_batches_keep_original_identity(self):
        legacy = normalize_research_batch(batch([record()]))
        self.assertNotIn("inventory", legacy["records"][0]["values"])
        self.assertEqual(json_sha256(legacy), json_sha256(normalize_research_batch(legacy)))

    def test_v2_flow_and_stock_fields_do_not_mix(self):
        rows = [record("2025-06-30"), record("2025-12-31"), record("2026-03-31"), record("2026-06-30")]
        for row in rows:
            row["values"].update(inventory=200., operating_costs=60., total_assets=1000., total_liabilities=200.)
        incoming = batch(rows)
        incoming["schema_version"] = 2
        result = financial_features(normalize_research_batch(incoming)["records"], 500.)
        self.assertNotIn("inventory", result["quarter_flows"])
        self.assertEqual(result["quality_facts"]["gross_margin_ytd_pct"], 40.)
        self.assertEqual(result["valuation"]["ps_ttm"], 5.)

    def test_event_planned_amount_does_not_become_executed(self):
        row = record(module="events")
        row.update(details={"announced_amount_cny": 100., "executed_amount_cny": None},
                   details_source="provider_structured_fields", report_period=None)
        incoming = batch([row], module="events")
        incoming["schema_version"] = 2
        result = normalize_research_batch(incoming)["records"][0]
        self.assertIsNone(result["details"]["executed_amount_cny"])
        self.assertEqual(result["status"], "announced")

    def test_forecast_revisions_match_year_and_institution_and_exclude_future(self):
        def estimate(year, institution, known, value):
            return {"forecast_year": year, "institution_id": institution, "estimate_basis": "individual_institution",
                    "known_at": known, "revision": known, "values": {"net_profit_parent": value}}
        rows = [estimate(2026, "A", "2026-08-01T10:00:00Z", 100.),
                estimate(2026, "A", "2026-09-04T10:00:00Z", 120.),
                estimate(2027, "A", "2026-09-04T10:00:00Z", 300.),
                estimate(2026, "B", "2026-09-04T10:00:00Z", 200.),
                estimate(2026, "A", "2026-09-08T10:00:00Z", 999.)]
        sessions = pd.bdate_range("2026-08-07", "2026-09-04").strftime("%Y-%m-%d").tolist()
        result = forecast_features(rows, "2026-09-04T12:15:00Z", sessions)
        self.assertEqual(result["revisions"]["5D"]["matched_individual_count"], 1)
        self.assertEqual(result["revisions"]["5D"]["individual_up_count"], 1)
        self.assertNotIn("999", str(result))

    def test_partial_peer_set_does_not_claim_industry_percentile(self):
        rows = [{"thscode": str(i), "financials": {"valuation": {"pe_ttm": float(i + 1)}}} for i in range(5)]
        rows[4]["financials"]["valuation"]["pe_ttm"] = None
        peer_valuation_percentiles(rows, {str(i): "industry" for i in range(5)})
        self.assertIsNone(rows[0]["financials"]["valuation"]["peer_comparison"]["pe_ttm"]["percentile"])


if __name__ == "__main__":
    unittest.main()
