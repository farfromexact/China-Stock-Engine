"""Local Wind daily/catch-up entry point. No remote dispatch or dashboard control."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import platform
import shutil
from zoneinfo import ZoneInfo

import pandas as pd

from .storage import (atomic_write_json, json_sha256, load_json_object,
                      load_manifest, sha256_file, verify_latest_artifacts)
from .wind_collect import ROOT, profile_from
from .wind_incremental import collect_incremental
from .wind_provider import WindProvider
from .wind_publish import publish, verify_candidate
from .wind_coordination import RequestPacer
from .wind_session import (WindBusyError, WindError, WindSession,
                           collector_lock, dashboard_running)

SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_LOCK = Path.home() / ".cache/china-stock-engine/wind-daily.lock"


def clock():
    return datetime.now(timezone.utc)


def completed_bound(at: datetime, close_hour: int = 18) -> str:
    """Upper bound only; the actual sessions MUST come from Wind's calendar."""
    local = at.astimezone(SHANGHAI)
    day = local.date() if local.hour >= close_hour else local.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


def instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Local readiness/retry timestamps must include a timezone")
    return result


def readiness(path: Path, at: datetime, allow_dashboard: bool = False) -> bool:
    """Dot/operator attestation, NOT a Wind login probe. Never imports the SDK."""
    hint = load_json_object(path)
    if not hint:
        return False
    if (hint.get("host") != platform.node() or hint.get("login_confirmed") is not True
            or hint.get("dashboard_coexistence" if allow_dashboard else "exclusive_window") is not True):
        return False
    expires = instant(hint["expires_at"])
    return at < expires <= at + timedelta(hours=1)


def release_candidates(workspace: Path, candidate: Path):
    """Retain one local-daily build; never sweep manual canaries/checkpoints."""
    index = workspace / "local_daily_candidates.json"
    previous = load_json_object(index).get("candidate")
    atomic_write_json(index, {"candidate": str(candidate.resolve())})
    if previous:
        old = Path(previous).resolve()
        if old != candidate.resolve() and (workspace / "candidates").resolve() in old.parents:
            shutil.rmtree(old, ignore_errors=True)


def settings(path: Path, root: Path) -> dict:
    config = load_json_object(path, missing_ok=False)
    if type(config.get("enabled")) is not bool:
        raise ValueError("Local configuration requires an explicit enabled boolean")
    if type(config.setdefault("allow_dashboard", False)) is not bool:
        raise ValueError("allow_dashboard must be boolean")
    RequestPacer(config.setdefault("request_interval_seconds", 2.0))
    for name, default in (("close_hour_shanghai", 18), ("batch_size", 100), ("retry_minutes", 15)):
        value = config.setdefault(name, default)
        if type(value) is not int:
            raise ValueError(f"{name} must be an integer")
    if not (18 <= config["close_hour_shanghai"] <= 23 and 1 <= config["batch_size"] <= 100
            and 5 <= config["retry_minutes"] <= 1440):
        raise ValueError("Invalid close hour, Wind batch size or retry interval")
    for name, default in (("profile", ".private-stock-data/wind/profile.json"),
                          ("workspace", ".private-stock-data/wind"), ("data_dir", "data"),
                          ("ready_file", ".private-stock-data/wind/local-ready.json")):
        config[name] = (root / config.get(name, default)).resolve()
    data, workspace = config["data_dir"], config["workspace"]
    if data == workspace or data in workspace.parents or workspace in data.parents:
        raise ValueError("Local workspace must be separate from production data")
    return config


def certified_days(calendar: dict, bound: str) -> list[str] | None:
    if calendar.get("through", "") < bound:
        return None
    days = calendar.get("days")
    if (not isinstance(days, list) or not days or days != sorted(set(days))
            or any(not isinstance(day, str) or len(day) != 10 for day in days)):
        raise ValueError("Invalid locally cached Wind trading calendar")
    return [day for day in days if day <= bound][-300:]


def coverage(data: Path, days: list[str], profile_hash: str) -> tuple[str | None, list[str]]:
    manifest = load_manifest(data / "latest/manifest.json")
    last = manifest.get("trade_date")
    source_profile = manifest.get("provenance", {}).get("profile")
    if manifest.get("provider") != "windpy" or json_sha256(source_profile) != profile_hash:
        return last, days
    # Only the currently published Wind vintage counts; legacy facts cannot
    # conceal a missing date or silently supply another provider's bars.
    vintage = str(manifest.get("wind_vintage", ""))
    if len(vintage) != 24 or any(c not in "0123456789abcdef" for c in vintage):
        raise ValueError("Latest has no valid Wind vintage")
    _, errors = verify_latest_artifacts(data)
    if errors:
        raise ValueError("Published latest artifacts failed their hash contract")
    missing = []
    for day in days:
        directory = data / "facts/wind" / f"vintage={vintage}" / "market" / f"trade_date={day}"
        receipt = load_json_object(directory / "market_receipt.json")
        valid = (receipt.get("verified") is True and receipt.get("trade_date") == day
                 and receipt.get("provider") == "windpy")
        for name in ("daily_quotes.parquet", "daily_security_status.parquet"):
            path = directory / name
            valid = valid and path.exists() and (receipt.get("artifacts", {}).get(name, {}).get("sha256") == sha256_file(path))
        if not valid:
            missing.append(day)
    return last, missing


def tick(config: dict, *, check_only: bool = False) -> dict:
    """One bounded attempt; repeated calls are safe at logon and after close."""
    workspace, data = config["workspace"], config["data_dir"]
    status_path = workspace / "local_daily_status.json"
    at = clock()
    bound = completed_bound(at, config["close_hour_shanghai"])
    base = {"schema_version": 1, "checked_at": at.isoformat(), "completed_bound": bound,
            "host": platform.node(), "remote_dispatch": False, "production_changed": False}

    def finish(state, **extra):
        result = {**base, "state": state, **extra}
        if not check_only:
            atomic_write_json(status_path, result)
        return result

    if not config["enabled"]:
        return finish("idle")
    # The orchestration lock spans collection AND publication; a login watcher,
    # startup task and daily task cannot collect or publish over one another.
    try:
        with collector_lock(DAILY_LOCK):
            try:
                previous = load_json_object(status_path)
                profile = profile_from(config["profile"])
                if not profile.units_verified:
                    return finish("blocked_unit_review")
                profile_hash = json_sha256(asdict(profile))
                cached = load_json_object(workspace / "local_calendar.json")
                if cached.get("profile_hash") != profile_hash:
                    cached = {}
                days = certified_days(cached, bound)
                # The published calendar can certify dates already observed, but
                # never extend beyond its own explicit vendor-certified bound.
                if days is None:
                    path = data / "latest/trading_calendar.parquet"
                    manifest = load_manifest(data / "latest/manifest.json")
                    if (path.exists() and manifest.get("provider") == "windpy"
                            and json_sha256(manifest.get("provenance", {}).get("profile")) == profile_hash):
                        from .storage import read_parquet_file
                        expected = manifest.get("artifacts", {}).get(path.name, {}).get("sha256")
                        if expected != sha256_file(path):
                            raise ValueError("Published trading calendar hash mismatch")
                        frame = read_parquet_file(path)
                        if not frame.empty and frame.calendar_window_complete.eq(True).all():
                            days = certified_days({"through": str(frame.calendar_window_end.min()),
                                "days": sorted(frame.loc[frame.is_open, "trade_date"].unique().tolist())}, bound)
                missing, last = [], None
                if days is not None:
                    last, missing = coverage(data, days, profile_hash)
                    if len(days) == 300 and not missing and last is not None and last >= days[-1]:
                        return finish("up_to_date", trade_date=last, missing_dates=[])
                if check_only:
                    return finish("needs_calendar" if days is None else "catchup_required",
                                  trade_date=last, missing_dates=missing)
                if previous.get("next_retry_at") and at < instant(previous["next_retry_at"]):
                    return {**base, "state": "retry_wait", "next_retry_at": previous["next_retry_at"]}
                # Resume an already validated candidate after a crash/publication
                # failure without logging in or downloading again.
                pending_path = workspace / "local_pending.json"
                pending = load_json_object(pending_path)
                if pending:
                    candidate = Path(pending["candidate"]).resolve()
                    if workspace / "candidates" not in candidate.parents:
                        raise ValueError("Pending candidate must be inside the local candidate workspace")
                    status = verify_candidate(candidate)
                    if status["profile_hash"] != profile_hash or status["trade_date"] > bound:
                        raise ValueError("Pending candidate profile/date differs from current local configuration")
                    current = load_manifest(data / "latest/manifest.json").get("trade_date", "")
                    if status["trade_date"] >= current:
                        receipt = publish(candidate, data, rolling=True)
                        base["production_changed"] = True
                        pending_path.unlink(missing_ok=True)
                        release_candidates(workspace, candidate)
                        return finish("published", trade_date=receipt["trade_date"], resumed=True,
                                      production_changed=True, catchup_still_required=receipt["trade_date"] < bound,
                                      incremental=receipt.get("incremental"))
                    # Another verified producer already advanced beyond this one.
                    pending_path.unlink()
                if not config.get("allow_dashboard", False) and dashboard_running():
                    return finish("waiting_dashboard", next_retry_at=(at + timedelta(minutes=config["retry_minutes"])).isoformat())
                if not readiness(config["ready_file"], at, config.get("allow_dashboard", False)):
                    return finish("waiting_login_and_exclusive_window")

                def guarded_request(operation, *args):
                    if not readiness(config["ready_file"], clock(), config.get("allow_dashboard", False)):
                        raise WindBusyError("Local login/exclusive-window attestation expired")
                    return session.request(operation, *args)

                with WindSession(exclusive_window=True, allow_dashboard=config.get("allow_dashboard", False),
                                 request_interval=config.get("request_interval_seconds", 2.0)) as session:
                    provider = WindProvider(guarded_request, profile)
                    calendar = provider.fetch_trade_calendar(bound, offset=-330)
                    days = sorted(calendar.loc[calendar.is_open & calendar.trade_date.le(bound), "trade_date"].unique())[-300:]
                    if len(days) != 300:
                        raise WindError("Wind did not certify 300 completed sessions")
                    atomic_write_json(workspace / "local_calendar.json", {"through": bound,
                        "profile_hash": profile_hash, "days": days, "observed_at": clock().isoformat()})
                    last, missing = coverage(data, days, profile_hash)
                    if not missing and last is not None and last >= days[-1]:
                        return finish("up_to_date", trade_date=last, missing_dates=[])
                    finish("collecting", trade_date=days[-1], missing_dates=missing)
                    candidate_status = collect_incremental(provider, workspace, data_dir=data,
                        end=days[-1], batch_size=config["batch_size"])
                # Native child is gone before staged production publication.
                atomic_write_json(pending_path, {"candidate": candidate_status["candidate"],
                                  "trade_date": candidate_status["trade_date"]})
                finish("publishing", trade_date=days[-1], missing_dates=missing)
                receipt = publish(Path(candidate_status["candidate"]), data, rolling=True)
                base["production_changed"] = True
                pending_path.unlink(missing_ok=True)
                release_candidates(workspace, Path(candidate_status["candidate"]))
                return finish("published", trade_date=receipt["trade_date"], missing_dates=missing,
                              production_changed=True, resumed=False, incremental=receipt.get("incremental"))
            except WindBusyError:
                return finish("waiting_exclusive_window", next_retry_at=(clock() + timedelta(minutes=config["retry_minutes"])).isoformat())
            except (WindError, ValueError, OSError, KeyError, TypeError) as exc:
                return finish("failed", error_type=type(exc).__name__, error=str(exc),
                              next_retry_at=(clock() + timedelta(minutes=config["retry_minutes"])).isoformat())
    except WindBusyError:
        # Never overwrite the status/progress owned by another invocation.
        return {**base, "state": "waiting_exclusive_window"}
    except (WindError, ValueError, OSError, KeyError, TypeError) as exc:
        # Avoid account details in operator logs; SDK errors are already scrubbed.
        return finish("failed", error_type=type(exc).__name__, error=str(exc),
                      next_retry_at=(clock() + timedelta(minutes=config["retry_minutes"])).isoformat())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "tick", "arm", "disarm"))
    parser.add_argument("--config", type=Path, default=ROOT / ".private-stock-data/wind/local_daily.json")
    parser.add_argument("--login-confirmed", action="store_true")
    parser.add_argument("--exclusive-window", action="store_true")
    parser.add_argument("--dashboard-coexistence", action="store_true",
                        help="Confirm login for an explicitly configured dashboard coexistence run")
    parser.add_argument("--minutes", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        config = settings(args.config, ROOT)
        if args.mode == "arm":
            coexist = config.get("allow_dashboard", False)
            confirmed = args.dashboard_coexistence if coexist else args.exclusive_window
            if not args.login_confirmed or not confirmed or not 1 <= args.minutes <= 60:
                raise ValueError("arm requires confirmed login, matching --dashboard-coexistence or --exclusive-window and 1..60 minutes")
            if not coexist and dashboard_running():
                raise ValueError("Trading desk is running; readiness was not granted")
            result = {"host": platform.node(), "login_confirmed": True, "exclusive_window": not coexist, "dashboard_coexistence": coexist,
                      "expires_at": (clock() + timedelta(minutes=args.minutes)).isoformat(), "state": "armed"}
            atomic_write_json(config["ready_file"], result)
        elif args.mode == "disarm":
            config["ready_file"].unlink(missing_ok=True)
            result = {"state": "disarmed", "production_changed": False}
        else:
            result = tick(config, check_only=args.mode == "check")
    except (WindError, ValueError, OSError) as exc:
        result = {"state": "configuration_error", "error": str(exc), "production_changed": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["state"] in {"failed", "configuration_error", "blocked_unit_review"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
