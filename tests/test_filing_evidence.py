import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from china_stock_engine.research_context import minimum_financial_evidence
from china_stock_engine.research_inputs import (
    normalize_research_batch, financial_features, build_research_artifacts, import_research_batch,
)
from china_stock_engine.storage import ArtifactContractError, atomic_write_json, json_sha256, serialize_json
from test_research_inputs import batch, record, source_and_frames


def filing():
    row = record()
    row.update(source_url_kind="public_document", document_access="public_document_verified",
               document_sha256="a" * 64, values={"capex": 123000000.0})
    row["field_evidence"] = {"capex": {"pdf_page": 12, "label": "Synthetic cash outflow",
        "column": "2026 H1", "source_value": -1.23, "source_unit": "CNY_1e8",
        "precision_cny": 1000000, "transformation": "cash_outflow_absolute"}}
    payload = batch([row])
    payload["schema_version"] = 2
    return payload


class FilingEvidenceTests(unittest.TestCase):
    def test_conversion_receipt_is_preserved_and_normalization_is_idempotent(self):
        normalized = normalize_research_batch(filing())
        self.assertEqual(normalized["records"][0]["values"]["capex"], 123000000)
        self.assertEqual(normalize_research_batch(normalized), normalized)
        self.assertEqual(normalized["records"][0]["field_evidence"]["capex"]["source_value"], -1.23)

    def test_missing_receipt_mismatched_unit_precision_or_page_fails_closed(self):
        changes = [("source_unit", "CNY"), ("precision_cny", 100000000),
                   ("pdf_page", 0), ("pdf_page", True), ("source_value", float("nan"))]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                payload = filing()
                payload["records"][0]["field_evidence"]["capex"][field] = value
                with self.assertRaises(ArtifactContractError):
                    normalize_research_batch(payload)
        for mutate in (lambda row: row.pop("field_evidence"),
                       lambda row: row.update(document_sha256=None),
                       lambda row: row["values"].update(revenue=1)):
            payload = filing()
            mutate(payload["records"][0])
            with self.assertRaises(ArtifactContractError):
                normalize_research_batch(payload)

    def test_legacy_batches_keep_existing_fields_and_hashes(self):
        for version in (1, 2):
            payload = batch([record()])
            payload["schema_version"] = version
            normalized = normalize_research_batch(payload)
            self.assertNotIn("field_evidence", normalized["records"][0])
            self.assertEqual(json_sha256(normalized), json_sha256(normalize_research_batch(normalized)))
        payload = filing()
        payload["schema_version"] = 1
        with self.assertRaises(ArtifactContractError):
            normalize_research_batch(payload)

    def test_net_debt_honors_the_coarser_source_precision(self):
        statement = {"values": {"cash": 23257789653.91, "interest_bearing_debt": 40353000000},
                     "field_evidence": {"cash": {"precision_cny": 0.01},
                         "interest_bearing_debt": {"precision_cny": 1000000}}}
        result = minimum_financial_evidence({"latest_statement": statement})
        self.assertEqual(result["net_debt_cny"], 17095000000)
        self.assertEqual(result["net_debt_precision_cny"], 1000000)

    def test_directory_finds_fundamentals_without_tape_candidate_membership(self):
        payload = batch([record("2025-06-30", profit=40), record(profit=30)])
        payload["schema_version"] = 2
        for row in payload["records"]:
            row["values"].update(net_profit_ex_nonrecurring=25, capex=1, cash=8,
                interest_bearing_debt=10, accounts_receivable=2, inventory=3)
            row["values"]["revenue"] = 100 if row["report_period"].startswith("2025") else 120
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_write_json(root / "incoming.json", payload)
            import_research_batch(root, root / "incoming.json")
            artifacts = build_research_artifacts(root, *source_and_frames())
            directory = artifacts["research_inputs_latest.json"]["company_evidence_directory"]
            self.assertEqual(directory["eligible_count"], 1)
            company = directory["companies"][0]
            self.assertIn("revenue_growth_profit_decline", company["routes"])
            encoded = serialize_json(artifacts[company["part"]], compact=True).encode("utf-8")
            self.assertEqual(company["sha256"], hashlib.sha256(encoded).hexdigest())

    def test_reviewed_live_batch_has_two_complete_bundles_and_no_invented_ttm(self):
        root = Path(__file__).resolve().parents[1]
        path = root / "data/facts/research/financials/0bfa8e0e759a61c7eac5783f1ba57a2551bbb990a0c704cfc5bec42bd029e29e.json"
        normalized = normalize_research_batch(json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(json_sha256(normalized), path.stem)
        for code in ("600019.SH", "601899.SH"):
            rows = [row for row in normalized["records"] if row["thscode"] == code]
            facts = financial_features(rows, None)
            self.assertEqual(minimum_financial_evidence(facts)["state"], "observed")
            self.assertIsNotNone(facts["yoy_pct"]["operating_cash_flow"])
            self.assertIsNone(facts["ttm_flows"]["revenue"])
            self.assertIsNone(facts["quarter_flows"]["revenue"])
            self.assertIsNone(facts["quality_facts"]["inventory_yoy_pct"])


if __name__ == "__main__":
    unittest.main()
