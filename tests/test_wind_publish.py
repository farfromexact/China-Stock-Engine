from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from china_stock_engine.wind_collect import collect_candidate
from china_stock_engine.wind_provider import WindProvider, WindProfile
from china_stock_engine.wind_publish import publish, verify_candidate, inventory
from china_stock_engine.pipeline import validate_latest
from china_stock_engine.storage import ArtifactContractError, verify_latest_artifacts
from china_stock_engine.wind_git import DataPublisher, git_binary
from test_wind import FakeWind


class WindPublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.workspace = Path(cls.temporary.name)
        days = pd.bdate_range("2025-01-02", periods=300).strftime("%Y-%m-%d").tolist()
        cls.status = collect_candidate(
            WindProvider(FakeWind(days), WindProfile(min_interval=0, units_verified=True)),
            cls.workspace, end=days[-1], sessions=300, min_universe=3)
        cls.candidate = Path(cls.status["candidate"])

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_small_sample_is_never_publishable(self):
        with self.assertRaises(ArtifactContractError):
            verify_candidate(self.candidate)

    def test_real_staging_preserves_history_and_changes_only_verified_latest(self):
        with tempfile.TemporaryDirectory() as temp:
            production = Path(temp) / "data"
            for name in ["facts/prior-provider/keep.bin", "snapshots/2025-01-01/keep.bin", "features/prior/keep.bin"]:
                p = production / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b"original")
            protected = inventory(production)
            # The 5000-security gate is tested separately. Use a small real
            # 300-session data bundle to exercise the complete filesystem/report
            # transaction without allocating millions of fixture rows.
            def validate_small(*, data_dir):
                return validate_latest(data_dir=data_dir, policy_min_universe_size=3)
            with patch("china_stock_engine.wind_publish.verify_candidate", return_value=self.status), \
                 patch("china_stock_engine.wind_publish.validate_latest", side_effect=validate_small):
                first = publish(self.candidate, production)
                second = publish(self.candidate, production)
            self.assertEqual(first, second)
            self.assertEqual(first["state"], "published")
            self.assertEqual(first["protected_historical_files"], len(protected))
            for name, digest in protected.items():
                from china_stock_engine.storage import sha256_file
                self.assertEqual(sha256_file(production / name), digest)
            manifest, errors = verify_latest_artifacts(production)
            self.assertFalse(errors)
            self.assertEqual(manifest["provider"], "windpy")
            self.assertTrue(manifest["source_snapshot_path"].startswith("facts/wind/vintage="))
            from china_stock_engine.pipeline import _previous_quality_context
            next_day = (pd.Timestamp(first["trade_date"]) + pd.Timedelta(days=1)).date().isoformat()
            previous = _previous_quality_context(production, next_day)
            self.assertIsNotNone(previous)
            self.assertEqual(set(previous["quotes"].source_provider), {"windpy"})

    def test_report_failure_cannot_change_production(self):
        with tempfile.TemporaryDirectory() as temp:
            production = Path(temp) / "data"; production.mkdir()
            (production / "keep.bin").write_bytes(b"original")
            before = inventory(production)
            with patch("china_stock_engine.wind_publish.verify_candidate", return_value=self.status), \
                 patch("china_stock_engine.wind_publish.build_data_reference_outputs", side_effect=ArtifactContractError("bad report")):
                with self.assertRaises(ArtifactContractError):
                    publish(self.candidate, production)
            self.assertEqual(inventory(production), before)

    def test_actual_publication_including_research_view_can_be_committed_and_pushed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            remote = Path(temp) / "remote.git"
            binary = git_binary()
            def git(*args):
                return subprocess.run([binary, "-C", str(root), *args], capture_output=True,
                                      text=True, check=True).stdout.strip()
            git("init", "-b", "main")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            (root / ".gitignore").write_text(".private-stock-data/\n")
            git("add", ".gitignore")
            git("commit", "-m", "source baseline")
            subprocess.run([binary, "init", "--bare", str(remote)], capture_output=True, check=True)
            git("remote", "add", "origin", str(remote))
            git("push", "-u", "origin", "main")
            def small(*, data_dir):
                return validate_latest(data_dir=data_dir, policy_min_universe_size=3)
            with patch("china_stock_engine.wind_publish.verify_candidate", return_value=self.status), \
                 patch("china_stock_engine.wind_publish.validate_latest", side_effect=small):
                publish(self.candidate, root / "data")
            with patch("china_stock_engine.wind_git.validate_latest", side_effect=small):
                result = DataPublisher(root, root / ".private-stock-data/wind",
                                       allowed_origins=(str(remote), remote.as_posix())).sync()
            self.assertTrue(result["pushed"])
            self.assertEqual(git("status", "--porcelain"), "")
            paths = git("show", "--format=", "--name-only", "HEAD").splitlines()
            self.assertTrue(any("research_view=" in path for path in paths))
            self.assertTrue(all(path.startswith("data/") for path in paths))

    def test_local_daily_rotates_only_managed_wind_versions_after_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            production = root / "data"
            old = production / "facts/prior-provider/keep.bin"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"immutable-original")
            def validate_small(*, data_dir):
                return validate_latest(data_dir=data_dir, policy_min_universe_size=3)
            with patch("china_stock_engine.wind_publish.verify_candidate", return_value=self.status), \
                 patch("china_stock_engine.wind_publish.validate_latest", side_effect=validate_small):
                baseline = publish(self.candidate, production)
                migration = production / "facts/wind" / ("vintage=" + baseline["vintage"])
                protected = inventory(migration)
                first_candidate = root / "first"
                second_candidate = root / "second"
                for path, note in ((first_candidate, "first daily"), (second_candidate, "second daily")):
                    shutil.copytree(self.candidate, path)
                    # Distinct valid candidate inventory, with the same tiny
                    # real 300-session fixture and unchanged quality contracts.
                    (path / "operator_note.json").write_text(json.dumps({"note": note}))
                first = publish(first_candidate, production, rolling=True)
                first_path = production / "facts/wind" / ("vintage=" + first["vintage"])
                before = inventory(production)
                with patch("china_stock_engine.wind_publish.tree_bytes", return_value=900_000_000):
                    with self.assertRaises(ArtifactContractError):
                        publish(second_candidate, production, rolling=True)
                self.assertEqual(inventory(production), before)
                second = publish(second_candidate, production, rolling=True)
            self.assertFalse(first_path.exists())
            self.assertEqual(inventory(migration), protected)
            self.assertEqual(old.read_bytes(), b"immutable-original")
            self.assertEqual(second["retention_class"], "local_daily_rolling")
            self.assertEqual(second["retired_local_daily_vintages"], [first_path.relative_to(production).as_posix()])
            self.assertTrue((production / second["source_snapshot_path"] / "manifest.json").exists())

    def test_symlink_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "file").write_bytes(b"value")
            try:
                (root / "link").symlink_to(root / "file")
            except OSError as exc:
                self.skipTest(f"OS does not allow test symlinks: {exc}")
            with self.assertRaises(ArtifactContractError):
                inventory(root)

    def test_wind_factor_selection_never_uses_legacy_fallback(self):
        from china_stock_engine.data_reference import load_adjustment_snapshot
        from china_stock_engine.storage import atomic_write_parquet
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = pd.DataFrame([{"thscode": "600000.SH", "trade_date": "2026-01-05",
                                 "adj_factor": 7., "known_at": "2026-01-05T18:00:00Z",
                                 "source_provider": "ifind_http"}])
            new = old.assign(adj_factor=2., source_provider="windpy")
            atomic_write_parquet(root / "facts/adjustment/as_of_date=2026-01-05/adjustment_factors.parquet", old)
            atomic_write_parquet(root / "facts/wind/vintage=one/adjustment/as_of_date=2026-01-05/vintage=factor/adjustment_factors.parquet", new)
            result = load_adjustment_snapshot(root, "2026-01-05", provider="windpy")
            self.assertEqual(result.adj_factor.tolist(), [2.])
            self.assertEqual(len(load_adjustment_snapshot(root, "2026-01-05")), 2)
