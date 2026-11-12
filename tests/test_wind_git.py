import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from china_stock_engine.storage import atomic_write_json, sha256_file
from china_stock_engine.wind_git import DataPublisher, GitPublishError, git_binary


class WindGitTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp) / "repo"
        self.root.mkdir()
        self.remote = Path(self.temp) / "remote.git"
        self.git = git_binary()
        self.call("init", "-b", "main")
        self.call("config", "user.name", "Fixture")
        self.call("config", "user.email", "fixture@example.invalid")
        (self.root / ".gitignore").write_text(".private-stock-data/\n")
        (self.root / "source.py").write_text("unchanged source\n")
        self.version("a" * 24, "2026-11-11", b"old")
        self.call("add", ".")
        self.call("commit", "-m", "baseline")
        subprocess.run([self.git, "init", "--bare", str(self.remote)], check=True, capture_output=True)
        self.call("remote", "add", "origin", str(self.remote))
        self.call("push", "-u", "origin", "main")
        self.base = self.call("rev-parse", "HEAD")
        self.publisher = DataPublisher(self.root, self.root / ".private-stock-data/wind",
                                       allowed_origins=(str(self.remote), self.remote.as_posix()))
        self.enterContext(patch("china_stock_engine.wind_git.validate_latest", return_value=(True, {})))

    def call(self, *args):
        return subprocess.run([self.git, "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def version(self, vintage, day, content):
        quotes = self.root / "data/latest/daily_quotes.parquet"
        quotes.parent.mkdir(parents=True, exist_ok=True)
        quotes.write_bytes(content)
        atomic_write_json(self.root / "data/latest/manifest.json", {"schema_version": 3,
            "provider": "windpy", "trade_date": day, "wind_vintage": vintage,
            "artifacts": {"daily_quotes.parquet": {"sha256": sha256_file(quotes)}}})
        atomic_write_json(self.root / "data/wind_publication_status.json", {
            "state": "published", "trade_date": day, "vintage": vintage,
            "retired_local_daily_vintages": []})
        path = self.root / "data/facts/wind" / ("vintage=" + vintage) / "daily_quotes.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def new_data(self):
        self.version("b" * 24, "2026-11-12", b"new verified data")

    def test_commits_only_generated_data_and_pushes_once(self):
        self.new_data()
        result = self.publisher.sync()
        self.assertTrue(result["pushed"])
        paths = self.call("diff", "--name-only", self.base, "HEAD").splitlines()
        self.assertTrue(paths and all(p.startswith("data/") for p in paths))
        self.assertEqual(self.call("status", "--porcelain"), "")
        self.assertEqual(self.call("rev-parse", "origin/main"), self.call("rev-parse", "HEAD"))
        self.assertEqual(self.publisher.sync()["state"], "no_changes")
        self.assertFalse(self.publisher.journal.exists())

    def test_push_failure_keeps_commit_and_retry_does_not_make_another(self):
        self.new_data()
        real = self.publisher.run
        def offline(*args):
            if args[0] == "push":
                raise GitPublishError("offline")
            return real(*args)
        with patch.object(self.publisher, "run", side_effect=offline):
            with self.assertRaises(GitPublishError):
                self.publisher.sync()
        commit = self.call("rev-parse", "HEAD")
        self.assertNotEqual(commit, self.base)
        self.assertTrue(self.publisher.journal.exists())
        self.assertEqual(self.publisher.sync()["commit"], commit)
        self.assertEqual(self.call("rev-parse", "HEAD"), commit)

    def test_existing_user_code_edits_are_not_staged(self):
        self.new_data()
        (self.root / "source.py").write_text("user edit")
        with self.assertRaises(GitPublishError):
            self.publisher.sync()
        self.assertEqual(self.call("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.call("diff", "--cached", "--name-only"), "")
        self.assertEqual((self.root / "source.py").read_text(), "user edit")

    def test_staged_user_data_is_preserved(self):
        self.new_data()
        self.call("add", "data/latest/daily_quotes.parquet")
        index = self.call("write-tree")
        with self.assertRaises(GitPublishError):
            self.publisher.sync()
        self.assertEqual(self.call("write-tree"), index)

    def test_unrelated_legacy_fact_edits_are_never_committed(self):
        self.new_data()
        old = self.root / "data/facts/ifind/keep.json"
        old.parent.mkdir(parents=True)
        old.write_text("unrelated")
        with self.assertRaises(GitPublishError):
            self.publisher.sync()
        self.assertEqual(self.call("rev-parse", "HEAD"), self.base)

    def test_validation_failure_keeps_data_and_does_not_commit(self):
        self.new_data()
        with patch("china_stock_engine.wind_git.validate_latest", return_value=(False, {})):
            with self.assertRaises(GitPublishError):
                self.publisher.sync()
        self.assertEqual(self.call("rev-parse", "HEAD"), self.base)
        self.assertTrue((self.root / "data/latest/daily_quotes.parquet").exists())

    def test_remote_advancement_does_not_force_or_overwrite(self):
        other = Path(self.temp) / "other"
        subprocess.run([self.git, "clone", "-b", "main", str(self.remote), str(other)],
                       check=True, capture_output=True)
        for key, value in (("user.name", "Other"), ("user.email", "other@example.invalid")):
            subprocess.run([self.git, "-C", str(other), "config", key, value], check=True)
        (other / "new-code.py").write_text("other code")
        for args in (("add", "."), ("commit", "-m", "remote change"), ("push", "origin", "main")):
            subprocess.run([self.git, "-C", str(other), *args], check=True, capture_output=True)
        self.new_data()
        with self.assertRaises(GitPublishError):
            self.publisher.sync()
        self.assertEqual(self.call("rev-parse", "HEAD"), self.base)
        self.assertNotEqual(self.call("rev-parse", "origin/main"), self.base)

    def test_changed_pending_data_refuses_upload(self):
        self.new_data()
        real = self.publisher.run
        def offline(*args):
            if args[0] == "push":
                raise GitPublishError("offline")
            return real(*args)
        with patch.object(self.publisher, "run", side_effect=offline):
            with self.assertRaises(GitPublishError):
                self.publisher.sync()
        (self.root / "data/latest/daily_quotes.parquet").write_bytes(b"changed after validation")
        with self.assertRaises(GitPublishError):
            self.publisher.sync()
        self.assertTrue(self.publisher.journal.exists())

    def test_wrong_branch_or_push_destination_is_blocked(self):
        self.new_data()
        self.call("switch", "-c", "feature")
        with self.assertRaises(GitPublishError):
            self.publisher.sync()
        self.call("switch", "main")
        self.call("config", "remote.origin.pushurl", str(Path(self.temp) / "wrong.git"))
        with self.assertRaises(GitPublishError):
            self.publisher.sync()
