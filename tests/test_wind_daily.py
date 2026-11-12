from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import platform
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from china_stock_engine.storage import atomic_write_json, json_sha256, sha256_file
from china_stock_engine.wind_daily import completed_bound, readiness, settings, tick
from china_stock_engine.wind_provider import WindProfile
from china_stock_engine.wind_session import collector_lock


class WindLocalDailyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.at = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
        self.days = pd.bdate_range(end="2026-11-11", periods=300).strftime("%Y-%m-%d").tolist()
        profile = self.root / "profile.json"
        atomic_write_json(profile, asdict(WindProfile(units_verified=True, min_interval=0)))
        config_path = self.root / "config.json"
        atomic_write_json(config_path, {"enabled": True, "profile": str(profile),
            "workspace": "private", "data_dir": "data", "ready_file": "ready.json"})
        self.config = settings(config_path, self.root)
        self.workspace = self.config["workspace"]
        self.data = self.config["data_dir"]
        self.hash = json_sha256(asdict(WindProfile(units_verified=True, min_interval=0)))
        self.cache_calendar(self.days)
        self.seed_data(self.days)
        for target, kwargs in [
            ("clock", {"return_value": self.at}),
            ("DAILY_LOCK", {"new": self.root / "daily.lock"}),
            ("verify_latest_artifacts", {"return_value": ({}, [])}),
            ("dashboard_running", {"return_value": False}),
            ("WindSession", {}), ("collect_incremental", {}), ("publish", {})]:
            obj = self.enterContext(patch("china_stock_engine.wind_daily." + target, **kwargs))
            setattr(self, target, obj)

    def cache_calendar(self, days, through="2026-11-11"):
        atomic_write_json(self.workspace / "local_calendar.json", {
            "profile_hash": self.hash, "through": through, "days": days})

    def seed_data(self, days):
        self.vintage = "a" * 24
        atomic_write_json(self.data / "latest/manifest.json", {"schema_version": 3,
            "provider": "windpy", "wind_vintage": self.vintage, "trade_date": days[-1],
            "provenance": {"profile": asdict(WindProfile(units_verified=True, min_interval=0))}})
        for day in days:
            path = self.partition(day)
            path.mkdir(parents=True, exist_ok=True)
            artifacts = {}
            for name in ("daily_quotes.parquet", "daily_security_status.parquet"):
                (path / name).write_bytes(b"hash-protected-history-fixture")
                artifacts[name] = {"sha256": sha256_file(path / name)}
            atomic_write_json(path / "market_receipt.json", {"verified": True, "provider": "windpy",
                              "trade_date": day, "artifacts": artifacts})

    def partition(self, day):
        return self.data / "facts/wind" / f"vintage={self.vintage}" / "market" / f"trade_date={day}"

    def arm(self):
        atomic_write_json(self.config["ready_file"], {"host": platform.node(), "login_confirmed": True,
            "exclusive_window": True, "expires_at": (self.at + timedelta(minutes=30)).isoformat()})

    def prepare_collection(self):
        self.partition(self.days[-1]).joinpath("daily_quotes.parquet").unlink()
        self.arm()
        provider = self.enterContext(patch("china_stock_engine.wind_daily.WindProvider"))
        provider.return_value.fetch_trade_calendar.return_value = pd.DataFrame({"is_open": True, "trade_date": self.days})
        self.candidate = self.workspace / "candidates" / "new"
        self.candidate.mkdir(parents=True)
        self.collect_incremental.return_value = {"candidate": str(self.candidate), "trade_date": self.days[-1]}
        self.publish.return_value = {"trade_date": self.days[-1]}
        return provider

    def test_close_gate_timezone_weekend_and_morning_catchup(self):
        self.assertEqual(completed_bound(datetime.fromisoformat("2026-11-12T09:00:00+08:00")), "2026-11-11")
        self.assertEqual(completed_bound(datetime.fromisoformat("2026-11-12T17:59:59+08:00")), "2026-11-11")
        self.assertEqual(completed_bound(datetime.fromisoformat("2026-11-12T18:00:00+08:00")), "2026-11-12")
        self.assertEqual(completed_bound(datetime.fromisoformat("2026-11-16T00:00:00+08:00")), "2026-11-13")

    def test_current_verified_window_does_not_start_sdk(self):
        self.assertEqual(tick(self.config)["state"], "up_to_date")
        self.WindSession.assert_not_called()
        self.publish.assert_not_called()

    def test_idle_is_explicit_and_never_imports_sdk(self):
        self.config["enabled"] = False
        self.assertEqual(tick(self.config)["state"], "idle")
        self.WindSession.assert_not_called()

    def test_closed_holiday_uses_certified_calendar_and_does_not_redownload(self):
        self.clock.return_value = self.at + timedelta(days=1)
        # Vendor certified 12th as closed, despite it being a weekday.
        self.cache_calendar(self.days, through="2026-11-12")
        self.assertEqual(tick(self.config)["state"], "up_to_date")
        self.WindSession.assert_not_called()

    def test_login_and_dashboard_guard_precede_native_session(self):
        self.partition(self.days[-1]).joinpath("daily_quotes.parquet").unlink()
        self.assertEqual(tick(self.config)["state"], "waiting_login_and_exclusive_window")
        self.arm()
        self.dashboard_running.return_value = True
        self.assertEqual(tick(self.config)["state"], "waiting_dashboard")
        self.WindSession.assert_not_called()

    def test_startup_gap_repair_and_second_tick_is_noop(self):
        self.prepare_collection()
        for day in self.days[-3:]:
            self.partition(day).joinpath("daily_quotes.parquet").unlink(missing_ok=True)
        def publish(candidate, data, *, rolling):
            self.assertTrue(rolling)
            self.seed_data(self.days)
            return {"trade_date": self.days[-1]}
        self.publish.side_effect = publish
        first = tick(self.config)
        self.assertEqual(first["state"], "published")
        self.assertEqual(first["missing_dates"], self.days[-3:])
        self.collect_incremental.assert_called_once()
        self.assertEqual(self.collect_incremental.call_args.kwargs["data_dir"], self.data)
        self.assertEqual(tick(self.config)["state"], "up_to_date")
        self.collect_incremental.assert_called_once()
        self.assertFalse((self.workspace / "local_pending.json").exists())

    def test_unknown_new_day_requires_live_calendar_instead_of_weekday_guess(self):
        self.clock.return_value = self.at + timedelta(days=1)
        self.assertEqual(tick(self.config, check_only=True)["state"], "needs_calendar")
        self.WindSession.assert_not_called()

    def test_newly_certified_holiday_needs_only_one_calendar_request(self):
        self.at += timedelta(days=1)
        self.clock.return_value = self.at
        self.arm()
        with patch("china_stock_engine.wind_daily.WindProvider") as provider:
            provider.return_value.fetch_trade_calendar.return_value = pd.DataFrame({
                "is_open": True, "trade_date": self.days})
            self.assertEqual(tick(self.config)["state"], "up_to_date")
            provider.return_value.fetch_trade_calendar.assert_called_once()
            self.assertEqual(tick(self.config)["state"], "up_to_date")
            self.WindSession.assert_called_once()
            self.collect_incremental.assert_not_called()

    def test_old_provider_cannot_conceal_a_wind_gap(self):
        old = self.data / "facts/market" / f"trade_date={self.days[-1]}" / "daily_quotes.parquet"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"legacy")
        self.partition(self.days[-1]).joinpath("daily_quotes.parquet").unlink()
        result = tick(self.config, check_only=True)
        self.assertEqual(result["state"], "catchup_required")
        self.assertEqual(result["missing_dates"], [self.days[-1]])

    def test_corrupt_quote_hash_is_repaired_even_if_latest_date_is_current(self):
        (self.partition(self.days[-20]) / "daily_quotes.parquet").write_bytes(b"broken")
        self.assertEqual(tick(self.config, check_only=True)["missing_dates"], [self.days[-20]])

    def test_changed_profile_cannot_reuse_prior_units_or_adjustment_vintage(self):
        atomic_write_json(self.config["profile"], asdict(WindProfile(units_verified=True, min_interval=0,
                                                                   amount_multiplier=1000)))
        result = tick(self.config, check_only=True)
        self.assertEqual(result["state"], "needs_calendar")
        self.WindSession.assert_not_called()

    def test_pending_publish_resumes_without_sdk_after_failed_publication(self):
        self.prepare_collection()
        self.publish.side_effect = ValueError("capacity gate")
        self.assertEqual(tick(self.config)["state"], "failed")
        self.assertTrue((self.workspace / "local_pending.json").exists())
        count = self.WindSession.call_count
        self.clock.return_value = self.at + timedelta(minutes=16)
        self.publish.side_effect = None
        with patch("china_stock_engine.wind_daily.verify_candidate", return_value={
                "profile_hash": self.hash, "trade_date": self.days[-1]}):
            result = tick(self.config)
        self.assertEqual(result["state"], "published")
        self.assertTrue(result["resumed"])
        self.assertEqual(self.WindSession.call_count, count)

    def test_failure_backoff_prevents_repeated_login_attempts(self):
        self.prepare_collection()
        self.collect_incremental.side_effect = ValueError("vendor failure")
        before = json.loads((self.data / "latest/manifest.json").read_text())
        self.assertEqual(tick(self.config)["state"], "failed")
        self.assertEqual(tick(self.config)["state"], "retry_wait")
        self.collect_incremental.assert_called_once()
        self.publish.assert_not_called()
        self.assertEqual(json.loads((self.data / "latest/manifest.json").read_text()), before)

    def test_overlapping_tasks_leave_owner_status_untouched(self):
        status_path = self.workspace / "local_daily_status.json"
        status_path.write_text('{"state":"collecting"}')
        with collector_lock(self.root / "daily.lock"):
            self.assertEqual(tick(self.config)["state"], "waiting_exclusive_window")
        self.assertEqual(json.loads(status_path.read_text()), {"state": "collecting"})
        self.WindSession.assert_not_called()

    def test_readiness_is_short_lived_host_specific_and_requires_both_confirmations(self):
        self.arm()
        self.assertTrue(readiness(self.config["ready_file"], self.at))
        self.assertFalse(readiness(self.config["ready_file"], self.at + timedelta(hours=1)))
        hint = json.loads(self.config["ready_file"].read_text())
        for key, value in (("host", "different-machine"), ("login_confirmed", False), ("exclusive_window", False),
                           ("expires_at", (self.at + timedelta(hours=2)).isoformat())):
            atomic_write_json(self.config["ready_file"], {**hint, key: value})
            self.assertFalse(readiness(self.config["ready_file"], self.at))

    def test_revoked_window_aborts_before_next_native_request(self):
        provider = self.prepare_collection()
        def revoked(*args, **kwargs):
            self.config["ready_file"].unlink()
            provider.call_args.args[0]("wsd", "code")
        self.collect_incremental.side_effect = revoked
        self.assertEqual(tick(self.config)["state"], "waiting_exclusive_window")
        self.WindSession.return_value.__enter__.return_value.request.assert_not_called()
        self.publish.assert_not_called()

    def test_unreviewed_windows_profile_never_starts_native_session(self):
        atomic_write_json(self.config["profile"], asdict(WindProfile()))
        self.assertEqual(tick(self.config)["state"], "blocked_unit_review")
        self.WindSession.assert_not_called()

    def test_config_rejects_shared_production_workspace_and_early_close(self):
        path = self.root / "invalid.json"
        for config in ({"enabled": True, "workspace": "data"},
                       {"enabled": True, "close_hour_shanghai": 15}):
            atomic_write_json(path, config)
            with self.assertRaises(ValueError):
                settings(path, self.root)


if __name__ == "__main__":
    unittest.main()

    def test_coexistence_collects_with_dashboard_running(self):
        self.prepare_collection()
        self.config['allow_dashboard']=True
        self.dashboard_running.return_value=True
        atomic_write_json(self.config['ready_file'], {'host':platform.node(),'login_confirmed':True,
            'dashboard_coexistence':True,'exclusive_window':False,
            'expires_at':(self.at+timedelta(minutes=30)).isoformat()})
        self.assertEqual(tick(self.config)['state'],'published')
        self.WindSession.assert_called_once_with(exclusive_window=True,allow_dashboard=True,request_interval=2.0)
        self.dashboard_running.assert_not_called()

    def test_exclusive_attestation_does_not_authorize_coexistence(self):
        self.prepare_collection()
        self.config['allow_dashboard']=True
        self.assertEqual(tick(self.config)['state'],'waiting_login_and_exclusive_window')
        self.WindSession.assert_not_called()
