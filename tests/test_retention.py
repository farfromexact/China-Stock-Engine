from pathlib import Path
import tempfile
import unittest

from china_stock_engine.retention import maintain
from china_stock_engine.storage import (
    ArtifactContractError, _prune_snapshots, atomic_write_json, sha256_file,
)


class SnapshotRetentionTests(unittest.TestCase):
    def snapshot(self, root, day):
        directory = root / "snapshots" / day
        directory.mkdir(parents=True)
        artifacts = {}
        for name, category in (("daily_quotes.parquet", "market"),
                               ("daily_security_status.parquet", "market"),
                               ("security_reference.parquet", "reference"),
                               ("universe.parquet", "reference"),
                               ("trading_calendar.parquet", "reference")):
            source = directory / name
            source.write_bytes((day + name).encode())
            fact = root / "facts" / category / (("trade_date=" if category == "market" else "as_of_date=") + day) / name
            fact.parent.mkdir(parents=True, exist_ok=True)
            fact.write_bytes(source.read_bytes())
            artifacts[name] = {"sha256": sha256_file(source)}
        atomic_write_json(directory / "manifest.json", {
            "schema_version": 3, "trade_date": day, "verified": True, "artifacts": artifacts})
        (directory / "research_a_001.json").write_text("derived report" * 100)
        return directory

    def test_prunes_duplicates_preserves_all_facts_factors_and_latest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            oldest = self.snapshot(root, "2026-10-28")
            self.snapshot(root, "2026-10-29")
            self.snapshot(root, "2026-10-30")
            factor = root / "facts" / "adjustment" / "factors.parquet"
            factor.parent.mkdir(parents=True); factor.write_bytes(b"300 sessions of factors")
            atomic_write_json(root / "latest" / "manifest.json", {
                "schema_version": 3, "trade_date": "2026-10-28"})
            before = {str(p): sha256_file(p) for p in (root / "facts").rglob("*.parquet")}
            status = maintain(root, snapshot_limit=1)
            self.assertTrue(oldest.exists())
            self.assertFalse((root / "snapshots" / "2026-10-29").exists())
            self.assertTrue((root / "snapshots" / "2026-10-30").exists())
            self.assertTrue((root / "facts/market/trade_date=2026-10-29/market_receipt.json").exists())
            self.assertTrue((root / "facts/collection_receipts/snapshot_retention/trade_date=2026-10-29/manifest.json").exists())
            self.assertEqual(before, {str(p): sha256_file(p) for p in (root / "facts").rglob("*.parquet")})
            self.assertLess(status["snapshot_bytes_after"], status["snapshot_bytes_before"])

    def test_missing_or_corrupt_fact_blocks_pruning(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                oldest = self.snapshot(root, "2026-10-29")
                self.snapshot(root, "2026-10-30")
                fact = root / "facts/reference/as_of_date=2026-10-29/universe.parquet"
                if corrupt:
                    fact.write_bytes(b"corrupted")
                else:
                    fact.unlink()
                with self.assertRaises(ArtifactContractError):
                    _prune_snapshots(root, 1)
                self.assertTrue(oldest.exists())

    def test_capacity_gate_excludes_git_and_records_block(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp); root = repo / "data"; root.mkdir()
            (repo / ".git").mkdir(); (repo / ".git/pack").write_bytes(b"x" * 1000)
            (root / "fact").write_bytes(b"x" * 20)
            status = maintain(root, max_data_bytes=100)
            self.assertEqual(status["state"], "capacity_ok")
            self.assertEqual(status["git_bytes"], 1000)
            status = maintain(root, max_data_bytes=10)
            self.assertEqual(status["state"], "capacity_blocked")
            self.assertTrue((root / "storage_status.json").exists())

    def test_invalid_limit_never_deletes(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                _prune_snapshots(Path(temp), 0)
