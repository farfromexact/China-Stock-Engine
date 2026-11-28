from datetime import datetime
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from china_stock_engine import wind_daily
from china_stock_engine.storage import ArtifactContractError, atomic_write_json, load_json_object
from china_stock_engine.weak_to_strong import MAX_CANDIDATES, scan
from china_stock_engine.wind_git import GitPublishError, ReversalPublisher, git_binary
from china_stock_engine.wind_provider import WindProfile
from china_stock_engine.wind_weak_to_strong import (
    BAR_COLUMNS, PACKAGE, Warehouse, collect, completed_bound, observe, validate_publication,
)

DAYS = pd.bdate_range("2024-03-01", periods=30).strftime("%Y-%m-%d").tolist()
CODES = ["600000.SH", "000001.SZ", "920001.BJ"]
PROFILE_HASH = "a" * 64


def observed(day):
    return f"{day}T16:18:00+08:00"


def universe(codes):
    return pd.DataFrame({"thscode": codes, "security_name": codes, "listing_date": DAYS[0]})


def bars_for(code, *, hit=None):
    frame = pd.DataFrame({"trade_date": DAYS, "thscode": code, "open": 100.0, "high": 101.0, "low": 99.0,
                          "close": 100.0, "volume": 10.0, "amount": 1000.0, "adj_factor": 1.0,
                          "known_at": [observed(day) for day in DAYS]})
    if hit is not None:
        prices = pd.Series([100, 99, 98, 97, 96, 95, 100], index=range(hit - 6, hit + 1), dtype=float)
        for field, offset in (("close", 0), ("open", 0), ("high", 1), ("low", -1)):
            frame.loc[prices.index, field] = prices + offset
        frame.loc[hit, "amount"] = 1300.0
    return frame


def scanned(*frames):
    bars = pd.concat(frames, ignore_index=True)
    return scan(bars, universe(bars.thscode.unique().tolist()), DAYS)


class ReversalSignalTests(unittest.TestCase):
    def test_exact_weakness_and_amount_thresholds_and_strict_watch(self):
        result = scanned(bars_for(CODES[0], hit=29))
        event = result["candidates"][0]
        self.assertEqual(event["weak_return_pct"], -5)
        self.assertEqual(event["amount_ratio"], 1.3)
        self.assertEqual(event["watch_price_at_trigger_basis"], 98)
        self.assertEqual(event["state"], "new")

        at_watch = bars_for(CODES[0], hit=29)
        at_watch.loc[29, ["open", "high", "low", "close"]] = [98, 99, 97, 98]
        self.assertEqual(scanned(at_watch)["counts"]["matched"], 0)
        at_watch.loc[29, ["open", "high", "low", "close", "amount"]] = [100, 101, 99, 100, 1299.99]
        self.assertEqual(scanned(at_watch)["counts"]["matched"], 0)

    def test_followup_holding_failure_and_gap_precedence(self):
        holding = bars_for(CODES[0], hit=27)
        failed = bars_for(CODES[1], hit=27)
        uncertain = bars_for(CODES[2], hit=27)
        holding.loc[28, ["open", "high", "low", "close"]] = [98, 99, 97, 98]
        failed.loc[29, ["open", "high", "low", "close"]] = [97, 98, 96, 97]
        failed = failed.drop(28)  # A known later breach still invalidates an earlier trigger.
        uncertain.loc[28, ["amount", "volume"]] = 0
        result = scanned(holding, failed, uncertain)
        events = {event["code"]: event for event in result["events"]}
        self.assertEqual(events[CODES[0]]["state"], "holding")
        self.assertEqual(events[CODES[1]]["failure_date"], DAYS[-1])
        self.assertEqual(events[CODES[1]]["state"], "failed")
        self.assertIsNone(events[CODES[1]]["close_index_path"][-2][1])
        self.assertEqual(events[CODES[2]]["state"], "unconfirmed")

    def test_adjustment_and_holes_do_not_create_signals(self):
        split = bars_for(CODES[0], hit=29)
        split.loc[23:28, ["open", "high", "low", "close"]] = [50, 51, 49, 50]
        split.loc[23:28, "adj_factor"] = 2
        gap = bars_for(CODES[1], hit=29).drop(26)
        ipo = bars_for(CODES[2], hit=29).iloc[-6:]
        self.assertEqual(scanned(split, gap, ipo)["counts"]["matched"], 0)

    def test_most_recent_trigger_and_gap_up_close_below_open(self):
        series = bars_for(CODES[0], hit=28)
        series.loc[22:23, ["open", "high", "low", "close"]] = [120, 121, 119, 120]
        series.loc[29, ["open", "high", "low", "close", "amount"]] = [103, 104, 100, 102, 1500]
        result = scanned(series)
        self.assertEqual(result["counts"]["total_triggers"], 2)
        self.assertEqual(result["counts"]["matched"], 1)
        self.assertEqual(result["candidates"][0]["trigger_date"], DAYS[-1])
        self.assertTrue(result["candidates"][0]["gap_up_close_below_open"])

    def test_broad_market_and_budget_preserve_stable_rank_and_totals(self):
        codes = [f"{number:06}.SH" for number in range(600001, 600035)]
        result = scanned(*(bars_for(code, hit=29) for code in reversed(codes)))
        self.assertEqual(result["market"][-1]["up"], len(codes))
        self.assertGreater(result["market"][-1]["median_return_pct"], 0)
        self.assertEqual(len(result["events"]), len(codes))
        self.assertEqual([event["code"] for event in result["candidates"]], codes[:MAX_CANDIDATES])
        self.assertEqual(result["counts"]["omitted_candidates"], len(codes) - MAX_CANDIDATES)


class ReversalWarehouseTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data = directory / "data"
        self.store = Warehouse(directory / "private" / "weak_to_strong")
        self.pool = universe(CODES)
        self.rows = pd.concat([bars_for(code) for code in CODES], ignore_index=True)
        self.enterContext(patch("china_stock_engine.wind_weak_to_strong.MIN_UNIVERSE", len(CODES)))

    def stage(self, rows=None, *, asof=None):
        return self.store.stage(self.rows if rows is None else rows, self.pool, DAYS,
                                observed_at=asof or observed(DAYS[-1]), profile_hash=PROFILE_HASH,
                                incremental={"audited_codes": [], "repairs": {}})

    def promote(self):
        return self.store.promote(self.data, PROFILE_HASH, DAYS[-1])

    def test_daily_default_selects_reversal_and_preserves_explicit_rollback(self):
        root = self.data.parent
        config = root / "config.json"
        atomic_write_json(config, {"enabled": False})
        self.assertEqual(wind_daily.settings(config, root)["product"], "weak_to_strong")
        self.assertEqual(wind_daily.settings(config, root, product="legacy_300d")["product"], "legacy_300d")
        with patch.object(wind_daily, "reversal_tick") as new_tick, \
             patch.object(wind_daily, "legacy_tick") as old_tick:
            wind_daily.tick(wind_daily.settings(config, root))
            wind_daily.tick(wind_daily.settings(config, root, product="legacy_300d"))
            new_tick.assert_called_once()
            old_tick.assert_called_once()

    def test_zero_signals_and_full_validator(self):
        candidate = self.stage()
        self.promote()
        path = self.data / PACKAGE
        self.assertEqual(load_json_object(path)["candidates"], [])
        self.assertTrue(validate_publication(self.data, self.store.workspace)[0])
        self.assertEqual(self.store.current()["vintage"], candidate["vintage"])
        self.assertEqual(len(list(self.data.rglob("*.json"))), 1)
        path.write_text(path.read_text(encoding="utf-8").replace('"candidates":[]', '"candidates":[0]'),
                        encoding="utf-8")
        self.assertFalse(validate_publication(self.data)[0])

    def test_failed_promotion_keeps_latest_and_resumes_same_candidate(self):
        first = self.stage()
        self.promote()
        published = (self.data / PACKAGE).read_bytes()
        second = self.stage(asof=f"{DAYS[-1]}T17:00:00+08:00")
        with patch("china_stock_engine.wind_weak_to_strong.atomic_copy", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.promote()
        self.assertEqual((self.data / PACKAGE).read_bytes(), published)
        self.assertEqual(self.store.current()["vintage"], first["vintage"])
        self.assertTrue(self.store.pending.is_file())
        self.promote()
        self.assertEqual(self.store.current()["vintage"], second["vintage"])
        self.assertFalse(self.store.directory(first["vintage"]).exists())

    def test_insufficient_observation_turnover_and_bad_factor_are_distinct(self):
        no_turnover = self.rows.copy()
        no_turnover.loc[0, ["volume", "amount"]] = 0
        observed_rows, _ = observe(no_turnover, self.pool, DAYS, observed(DAYS[-1]))
        self.assertEqual(observed_rows.state.value_counts()["no_turnover"], 1)
        with self.assertRaisesRegex(ArtifactContractError, "coverage"):
            self.stage(self.rows.drop(0))
        bad = self.rows.copy()
        bad.loc[0, "adj_factor"] = 0
        with self.assertRaisesRegex(ArtifactContractError, "adjustment"):
            self.stage(bad)

    def test_tampered_private_receipt_does_not_reach_publication(self):
        receipt = self.stage()
        (self.store.directory(receipt["vintage"]) / "events.json").write_text("{}")
        with self.assertRaisesRegex(ArtifactContractError, "artifact"):
            self.promote()
        self.assertFalse((self.data / PACKAGE).exists())

    def test_preclose_and_future_known_at_are_rejected(self):
        with self.assertRaisesRegex(ArtifactContractError, "finality"):
            self.stage(asof=f"{DAYS[-1]}T15:29:59+08:00")
        future = self.rows.copy()
        future.loc[0, "known_at"] = f"{DAYS[-1]}T17:30:00+08:00"
        with self.assertRaisesRegex(ArtifactContractError, "observation time"):
            self.stage(future)
        self.assertEqual(completed_bound(datetime.fromisoformat(f"{DAYS[-1]}T15:30:00+08:00")), DAYS[-1])

    def test_daily_retries_candidate_before_opening_sdk(self):
        self.stage()
        root = self.store.workspace.parent
        profile = root / "profile.json"
        atomic_write_json(profile, {"units_verified": True})
        config = {"enabled": True, "auto_git_push": False, "retry_minutes": 15,
                  "product": "weak_to_strong", "data_dir": self.data, "workspace": root, "profile": profile}
        with patch.object(wind_daily, "DAILY_LOCK", root / "daily.lock"), \
             patch.object(wind_daily, "json_sha256", return_value=PROFILE_HASH), \
             patch.object(wind_daily, "WindSession") as session:
            result = wind_daily.tick(config)
        self.assertEqual(result["state"], "published")
        session.assert_not_called()


    def test_uncommitted_push_retry_precedes_profile_check_and_sdk(self):
        self.stage()
        self.promote()
        root = self.store.workspace.parent
        atomic_write_json(self.store.workspace / "local_daily_status.json", {"state": "git_push_pending"})
        config = {"enabled": True, "auto_git_push": True, "retry_minutes": 15,
                  "product": "weak_to_strong", "data_dir": self.data, "workspace": root,
                  "profile": root / "profile.json"}
        with patch.object(wind_daily, "DAILY_LOCK", root / "daily.lock"), \
             patch.object(wind_daily, "profile_from") as profile, \
             patch.object(wind_daily, "WindSession") as session, \
             patch.object(wind_daily, "sync_reversal", side_effect=GitPublishError("offline")) as sync:
            self.assertEqual(wind_daily.tick(config, check_only=True)["state"], "git_push_pending")
            sync.assert_not_called()
            self.assertEqual(wind_daily.tick(config)["state"], "git_push_pending")
            sync.assert_called_once()
        profile.assert_not_called()
        session.assert_not_called()
        self.assertTrue(validate_publication(self.data, self.store.workspace)[0])


class ReversalCollectionTests(unittest.TestCase):
    def test_verified_basis_probes_both_closes_and_fetches_only_gap_for_unaudited_code(self):
        class Provider:
            profile = WindProfile(units_verified=True)
            source_endpoints = ("tdays", "wset", "wss", "wsd")
            provenance = {}

            def __init__(self, rows):
                self.rows = rows
                self.calls = []

            def fetch_universe(self, trade_date):
                return universe(CODES)

            def fetch_security_reference(self, codes, trade_date, **kwargs):
                raise AssertionError("Listing dates should have been reused")

            def history_many(self, codes, start, end, *, adjusted=False):
                self.calls.append(("history", tuple(codes), start, end, adjusted))
                return {code: self.rows.loc[self.rows.thscode.eq(code) & self.rows.trade_date.between(start, end)]
                        .drop(columns=["known_at", "adj_factor"]).copy() for code in codes}

            def close_many(self, codes, start, end, *, adjusted=False):
                self.calls.append(("close", tuple(codes), start, end, adjusted))
                return {code: self.rows.loc[self.rows.thscode.eq(code) & self.rows.trade_date.between(start, end),
                                            ["trade_date", "thscode", "close"]].copy() for code in codes}

        rows = pd.concat([bars_for(code) for code in CODES], ignore_index=True)
        provider = Provider(rows)
        prior = rows.loc[rows.trade_date.lt(DAYS[-1])]
        basis = prior, universe(CODES), {"vintage": "b" * 24, "incremental": {"next_audit_bucket": 3}}
        with patch("china_stock_engine.wind_weak_to_strong.MIN_UNIVERSE", len(CODES)), \
             patch("china_stock_engine.wind_incremental.audit_bucket", side_effect=lambda code: 3 if code == CODES[0] else 4):
            result, _, receipt = collect(provider, DAYS, basis)
        self.assertEqual(len(result), len(rows))
        self.assertEqual(set(result.columns), set(BAR_COLUMNS))
        self.assertEqual(receipt["audited_codes"], [CODES[0]])
        self.assertEqual(receipt["gap_rows_downloaded"], len(CODES))
        self.assertEqual(receipt["reused_rows"], 2 * (len(DAYS) - 1))
        self.assertEqual(len([call for call in provider.calls if call[0] == "close"]), 2)
        self.assertTrue(all(call[2] == DAYS[-1] for call in provider.calls
                            if call[0] == "history" and CODES[1] in call[1]))
        reused = result.loc[result.thscode.eq(CODES[1]) & result.trade_date.eq(DAYS[0]), "known_at"].iloc[0]
        self.assertEqual(reused, observed(DAYS[0]))


class ReversalGitTests(unittest.TestCase):
    def test_only_package_is_committed_and_push_retries_same_commit(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        root, remote = directory / "repo", directory / "remote.git"
        root.mkdir()
        git = git_binary()

        def run(*args):
            return subprocess.run([git, "-C", str(root), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()

        run("init", "-b", "main")
        run("config", "user.name", "Fixture")
        run("config", "user.email", "fixture@example.invalid")
        (root / ".gitignore").write_text(".private-stock-data/\n")
        run("add", ".gitignore")
        run("commit", "-m", "baseline")
        base = run("rev-parse", "HEAD")
        run("init", "--bare", str(remote))
        run("remote", "add", "origin", str(remote))
        run("push", "-u", "origin", "main")
        store = Warehouse(root / ".private-stock-data" / "wind" / "weak_to_strong")
        with patch("china_stock_engine.wind_weak_to_strong.MIN_UNIVERSE", len(CODES)):
            bars = pd.concat([bars_for(code) for code in CODES], ignore_index=True)
            store.stage(bars, universe(CODES), DAYS, observed_at=observed(DAYS[-1]),
                        profile_hash=PROFILE_HASH, incremental={})
            store.promote(root / "data", PROFILE_HASH, DAYS[-1])
            publisher = ReversalPublisher(root, store.workspace,
                                          allowed_origins=(str(remote), remote.as_posix()))
            real_run = publisher.run

            def offline(*args):
                if args[0] == "push":
                    raise GitPublishError("offline")
                return real_run(*args)

            with patch.object(publisher, "run", side_effect=offline):
                with self.assertRaisesRegex(GitPublishError, "offline"):
                    publisher.sync()
            commit = run("rev-parse", "HEAD")
            self.assertEqual(run("diff", "--name-only", base, commit), "data/" + PACKAGE)
            self.assertTrue(publisher.journal.exists())
            self.assertEqual(publisher.sync()["commit"], commit)
            self.assertEqual(run("rev-parse", "HEAD"), commit)
            self.assertEqual(publisher.sync()["state"], "no_changes")
