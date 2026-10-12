import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO

import pandas as pd

from china_stock_engine.company_exposures import load_exposures
from china_stock_engine.data_reference import load_market_history
from china_stock_engine.research_context import availability, build_capabilities, timestamp
from china_stock_engine.research_inputs import build_research_artifacts, import_research_batch
from china_stock_engine.storage import (
    ArtifactContractError, atomic_write_json, publish_data_reference_artifacts,
    serialize_json, sha256_file, verify_latest_artifacts,
)
from test_research_inputs import batch, record, source_and_frames


def securities(outputs):
    return {row["thscode"]: row for name, page in outputs.items()
            if name != "research_inputs_latest.json" for row in page["securities"]}


class ResearchContextTests(unittest.TestCase):
    def test_late_observation_is_usable_now_but_never_backdated(self):
        result = availability(published_at="2026-10-09T10:00:00Z",
            first_observed_at="2026-10-10T01:00:00Z", known_at="2026-10-10T01:00:00Z",
            research_asof="2026-10-12T09:00:00+08:00", historical_cutoff="2026-10-09T20:15:00+08:00")
        self.assertTrue(result["usable_for_current_research"])
        self.assertFalse(result["usable_for_historical_replay"])
        with self.assertRaises(ArtifactContractError):
            timestamp("2026-10-12")

    def test_later_cutoff_includes_new_fact_without_rewriting_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incoming = root / "input.json"
            late = "2026-08-29T10:00:00+00:00"
            atomic_write_json(incoming, batch([record(known=late)], completed=late))
            import_research_batch(root, incoming)
            original = {str(path): path.read_bytes() for path in (root / "facts").rglob("*.json")}
            args = source_and_frames()
            old = build_research_artifacts(root, *args)
            self.assertIsNone(securities(old)["600001.SH"]["financials"])
            new = build_research_artifacts(root, *args, research_asof="2026-08-30T10:00:00Z")
            item = securities(new)["600001.SH"]
            self.assertEqual(item["financials"]["latest_statement"]["report_period"], "2026-06-30")
            self.assertFalse(item["financials"]["latest_statement"]["research_availability"]["usable_for_historical_replay"])
            self.assertIn("capex", item["minimum_financial_evidence"]["missing_fields"])
            self.assertEqual(new["research_inputs_latest.json"]["capability_counts"]["earnings_cashflow"], 1)
            self.assertEqual(original, {str(path): path.read_bytes() for path in (root / "facts").rglob("*.json")})
            for parts in new["research_inputs_latest.json"]["shards"].values():
                for part in parts:
                    encoded = serialize_json(new[part["path"]], compact=True).encode("utf-8")
                    self.assertEqual(part["sha256"], hashlib.sha256(encoded).hexdigest())
                    self.assertLessEqual(len(encoded), 300 * 1024)

    def test_capabilities_do_not_promote_partial_profit_or_missing_adjustments(self):
        state = pd.DataFrame({"raw_return_1d_pct": [1.0, 2.0], "tradability_state": ["clear", "unknown"]})
        caps = build_capabilities(state, {"stock_state": {"state": "ready"}}, {
            "universe_count": 2, "field_coverage": {"financial_non_null_counts": {"net_profit_parent": 2}}})
        self.assertEqual(caps["daily_price_volume"]["state"], "ready")
        self.assertEqual(caps["multi_period_price"]["windows"]["1D"]["adjusted"]["state"], "missing")
        self.assertEqual(caps["earnings_cashflow"]["state"], "missing")
        self.assertEqual(caps["index_breadth"]["state"], "missing")
        self.assertEqual(caps["execution_checks"]["state"], "not_ready")

    def test_exposures_require_provenance_and_respect_cutoff(self):
        row = {"exposure_id": "synthetic-1", "thscode": "600001.SH", "business_segment": "synthetic",
               "commodity": "synthetic", "channel": "input_cost", "direction": "negative",
               "constraints": ["hedging unknown"], "transmission_horizon": "unknown",
               "source_url": "https://example.org/synthetic-filing", "source_published_at": "2026-08-28T00:00:00Z",
               "first_observed_at": "2026-08-29T00:00:00Z", "known_at": "2026-08-29T00:00:00Z",
               "confidence": "research_only", "evidence_summary": "synthetic fixture only"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "exposures.json"
            atomic_write_json(path, {"schema_version": 1, "exposures": [row]})
            self.assertEqual(load_exposures(path, "2026-08-28T23:00:00Z")[0], [])
            self.assertEqual(len(load_exposures(path, "2026-08-30T00:00:00Z")[0]), 1)
            invalid = copy.deepcopy(row)
            invalid["first_observed_at"] = "2026-08-27T00:00:00Z"
            atomic_write_json(path, {"schema_version": 1, "exposures": [invalid]})
            with self.assertRaises(ArtifactContractError):
                load_exposures(path, "2026-08-30T00:00:00Z")

    def test_current_publication_preserves_snapshot_and_failure_preserves_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            day = "2026-08-28"
            snapshot = root / "snapshots" / day
            atomic_write_json(snapshot / "sentinel.json", {"original": True})
            atomic_write_json(snapshot / "manifest.json", {"schema_version": 3, "trade_date": day,
                "artifacts": {"sentinel.json": {"sha256": sha256_file(snapshot / "sentinel.json")}}})
            before = (snapshot / "manifest.json").read_bytes()
            arguments = (root, day, pd.DataFrame({"thscode": ["600001.SH"]}),
                         {"data_catalog": [{"name": "stock_state.parquet", "path": "historical"}]},
                         {}, {"candidate_union": []}, {})
            publish_data_reference_artifacts(*arguments, current_research=True)
            self.assertEqual(before, (snapshot / "manifest.json").read_bytes())
            self.assertFalse((snapshot / "stock_state.parquet").exists())
            self.assertFalse((root / "features").exists())
            self.assertEqual(verify_latest_artifacts(root)[1], [])
            self.assertEqual(arguments[3]["data_catalog"][0]["path"], "latest/stock_state.parquet")
            latest_before = {p.name: p.read_bytes() for p in (root / "latest").iterdir()}
            bad = list(arguments)
            bad[5] = {"oversize": "x" * 310000}
            with self.assertRaises(ArtifactContractError):
                publish_data_reference_artifacts(*bad, current_research=True)
            self.assertEqual(latest_before, {p.name: p.read_bytes() for p in (root / "latest").iterdir()})

    def test_history_uses_verified_observation_time_not_trade_date(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            day = "2026-08-28"
            path = root / "facts" / "market" / f"trade_date={day}" / "daily_quotes.parquet"
            path.parent.mkdir(parents=True)
            pd.DataFrame({"trade_date": [day], "thscode": ["600001.SH"], "close": [10.0]}).to_parquet(path)
            atomic_write_json(root / "snapshots" / day / "manifest.json", {
                "schema_version": 3, "verified": True, "collection_completed_at": "2026-08-29T00:00:00Z",
                "artifacts": {"daily_quotes.parquet": {"sha256": sha256_file(path)}}})
            self.assertTrue(load_market_history(root, day, research_asof="2026-08-28T23:00:00Z").empty)
            self.assertEqual(len(load_market_history(root, day, research_asof="2026-08-30T00:00:00Z")), 1)
            pd.DataFrame({"trade_date": [day], "thscode": ["600001.SH"], "close": [20.0]}).to_parquet(path)
            with self.assertRaisesRegex(ArtifactContractError, "hash mismatch"):
                load_market_history(root, day, research_asof="2026-08-30T00:00:00Z")

    def test_deep_research_scope_is_explicit_verified_and_bounded_before_client(self):
        from china_stock_engine.ifind_enrichment import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.mkdir()
            universe = latest / "universe.parquet"
            pd.DataFrame({"thscode": ["600001.SH"]}).to_parquet(universe)
            atomic_write_json(latest / "manifest.json", {"schema_version": 3, "trade_date": "2026-08-28",
                "verified": True, "data_fresh": True,
                "artifacts": {"universe.parquet": {"sha256": sha256_file(universe)}}})
            with patch("china_stock_engine.ifind_enrichment.load_config", return_value={}), \
                 patch("china_stock_engine.ifind_enrichment.BoundedClient") as client, \
                 patch("china_stock_engine.ifind_enrichment.run_enrichment", return_value={"ok": True, "modules": {}}) as run, \
                 redirect_stdout(StringIO()):
                self.assertEqual(main(["--data-dir", directory, "--codes", "600001.SH", "--modules", "financial_history"]), 0)
                self.assertEqual(run.call_args.args[3], ["600001.SH"])
                client.reset_mock()
                with self.assertRaises(ArtifactContractError):
                    main(["--data-dir", directory, "--codes", "000001.SZ"])
                client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
