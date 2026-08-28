"""Command-line interface for collection, probing, and validation."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from getpass import getpass
import json
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .dashboard import build_dashboard
from .ifind_http import IFindHTTPClient, QUOTE_FIELDS
from .pipeline import CollectionConfig, collect_and_publish, validate_latest
from .report_dashboard import build_data_reference_dashboard
from .data_reference import build_data_reference_outputs
from .storage import (
    atomic_write_json,
    atomic_write_parquet,
    load_manifest,
    write_run_status,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def default_trade_date() -> str:
    now = datetime.now(SHANGHAI)
    trade_day = now.date() if now.hour >= 18 else now.date() - timedelta(days=1)
    while trade_day.weekday() >= 5:
        trade_day -= timedelta(days=1)
    return trade_day.isoformat()


def _token(prompt_token: bool) -> str | None:
    value = os.environ.get("IFIND_REFRESH_TOKEN")
    if value:
        return value
    if prompt_token:
        return getpass("iFinD refresh token (hidden): ").strip()
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser("probe", help="run a no-write single-stock canary")
    probe.add_argument("--code", default="000001.SZ")
    probe.add_argument("--date", default=default_trade_date())
    probe.add_argument("--prompt-token", action="store_true")

    canary = subparsers.add_parser(
        "canary", help="run a narrow entitlement or schema canary"
    )
    canary.add_argument(
        "--module",
        required=True,
        choices=("history", "adjustment", "industry", "index-membership", "tradability"),
    )
    canary.add_argument("--code", default="000001.SZ")
    canary.add_argument("--date", default=default_trade_date())
    canary.add_argument("--start")
    canary.add_argument(
        "--spec",
        type=Path,
        help="JSON indicator spec for modules without stable public indicator codes",
    )
    canary.add_argument("--prompt-token", action="store_true")
    canary.add_argument("--record-status", action="store_true")
    canary.add_argument("--data-dir", type=Path, default=Path("data"))

    run = subparsers.add_parser("run", help="collect and promote a verified snapshot")
    run.add_argument("--date", default=default_trade_date())
    run.add_argument("--data-dir", type=Path, default=Path("data"))
    run.add_argument("--prompt-token", action="store_true")
    run.add_argument("--min-universe-size", type=int, default=5000)
    run.add_argument("--min-quote-coverage", type=float, default=0.98)
    run.add_argument("--min-reference-coverage", type=float, default=0.98)
    run.add_argument("--min-extended-field-coverage", type=float, default=0.95)
    run.add_argument("--reference-batch-size", type=int, default=100)
    run.add_argument("--batch-size", type=int, default=300)
    run.add_argument("--refresh-adjustments", action="store_true")
    run.add_argument("--adjustment-start")
    run.add_argument("--adjustment-batch-size", type=int, default=100)

    backfill = subparsers.add_parser(
        "backfill", help="collect a point-in-time weekday range into durable partitions"
    )
    backfill_range = backfill.add_mutually_exclusive_group(required=True)
    backfill_range.add_argument("--start")
    backfill_range.add_argument(
        "--sessions", type=int, help="use the last N SSE trading sessions"
    )
    backfill.add_argument("--end", required=True)
    backfill.add_argument("--data-dir", type=Path, default=Path("data"))
    backfill.add_argument("--prompt-token", action="store_true")
    backfill.add_argument("--min-universe-size", type=int, default=5000)
    backfill.add_argument("--min-quote-coverage", type=float, default=0.98)
    backfill.add_argument("--min-reference-coverage", type=float, default=0.98)
    backfill.add_argument("--min-extended-field-coverage", type=float, default=0.95)
    backfill.add_argument("--reference-batch-size", type=int, default=1000)
    backfill.add_argument("--batch-size", type=int, default=300)
    backfill.add_argument("--with-adjustment-snapshot", action="store_true")
    backfill.add_argument("--adjustment-batch-size", type=int, default=100)

    validate = subparsers.add_parser("validate", help="validate the latest snapshot")
    validate.add_argument("--data-dir", type=Path, default=Path("data"))

    dashboard = subparsers.add_parser(
        "dashboard", help="build an offline HTML dashboard from latest"
    )
    dashboard.add_argument("--data-dir", type=Path, default=Path("data"))
    dashboard.add_argument("--output", type=Path)

    build_state = subparsers.add_parser(
        "build-state", help="build point-in-time stock state and data reference"
    )
    build_state.add_argument("--date")
    build_state.add_argument("--data-dir", type=Path, default=Path("data"))
    build_state.add_argument("--min-adt20", type=float, default=20_000_000.0)

    build_report = subparsers.add_parser(
        "build-report", help="rebuild the compact data reference from verified facts"
    )
    build_report.add_argument("--date")
    build_report.add_argument("--data-dir", type=Path, default=Path("data"))
    build_report.add_argument("--min-adt20", type=float, default=20_000_000.0)
    return parser


def _client(args: argparse.Namespace) -> IFindHTTPClient:
    token = _token(bool(getattr(args, "prompt_token", False)))
    if not token:
        raise RuntimeError(
            "IFIND_REFRESH_TOKEN is missing; use a process environment or --prompt-token"
        )
    return IFindHTTPClient(refresh_token=token)


def _probe(args: argparse.Namespace) -> int:
    client = _client(args)
    try:
        calendar = client.fetch_trade_calendar(args.date, offset=-3)
        calendar_dates = sorted(
            calendar.get("trade_date", pd.Series(dtype=str))
            .dropna()
            .astype(str)
            .unique()
            .tolist()
        )
        reference = client.fetch_security_reference(
            [str(args.code).upper()],
            args.date,
            batch_size=1,
            request_interval_seconds=0,
        )
        frame = client.fetch_daily_quotes(
            [str(args.code).upper()],
            args.date,
            batch_size=1,
            request_interval_seconds=0,
        )
        source_dates = sorted(
            frame.get("trade_date", pd.Series(dtype=str))
            .dropna()
            .astype(str)
            .unique()
            .tolist()
        )
        non_null = {
            field: int(frame.get(target, pd.Series(dtype=float)).notna().sum())
            for field, target in {
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "preClose": "pre_close",
                "avgPrice": "avg_price",
                "volume": "volume",
                "amount": "amount",
                "turnoverRatio": "turnover_ratio",
                "changeRatio": "change_ratio",
            }.items()
        }
        reference_non_null = {
            field: int(reference.get(field, pd.Series(dtype=object)).notna().sum())
            for field in ("listing_date", "total_shares", "float_a_shares")
        }
        calendar_ok = args.date in calendar_dates
        reference_ok = len(reference) == 1 and all(reference_non_null.values())
        quote_ok = len(frame) > 0 and args.date in source_dates
        ok = calendar_ok and reference_ok and quote_ok
        payload = {
            "auth_ok": bool(client.access_token),
            "calendar_ok": calendar_ok,
            "reference_ok": reference_ok,
            "quote_ok": quote_ok,
            "requested_code": str(args.code).upper(),
            "requested_trade_date": args.date,
            "calendar_dates": calendar_dates,
            "reference_rows": int(len(reference)),
            "reference_non_null_fields": reference_non_null,
            "rows": int(len(frame)),
            "source_dates": source_dates,
            "requested_fields": list(QUOTE_FIELDS),
            "non_null_fields": non_null,
            "raw_payload_persisted": False,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        for secret in (client.refresh_token, client.access_token):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        print(
            json.dumps(
                {"auth_ok": False, "quote_ok": False, "error": message[:500]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1


def _replace_spec_placeholders(value: Any, trade_date: str) -> Any:
    if isinstance(value, dict):
        return {
            key: _replace_spec_placeholders(item, trade_date)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_spec_placeholders(item, trade_date) for item in value]
    if isinstance(value, str):
        return value.replace("{date}", trade_date).replace(
            "{compact_date}", trade_date.replace("-", "")
        )
    return value


def _safe_client_error(exc: Exception, client: IFindHTTPClient) -> str:
    message = f"{type(exc).__name__}: {exc}"
    for secret in (client.refresh_token, client.access_token):
        if secret:
            message = message.replace(str(secret), "[REDACTED]")
    return message[:500]


def _is_entitlement_error(message: str) -> bool:
    lowered = message.lower()
    markers = (
        "not entitled",
        "permission denied",
        "no permission",
        "not licensed",
        "未购买",
        "无权限",
        "没有权限",
        "权限不足",
    )
    return any(marker in lowered for marker in markers)


def _record_canary_status(
    data_dir: Path, module: str, trade_date: str, state: str
) -> None:
    output = (
        data_dir
        / "facts"
        / "module_status"
        / f"as_of_date={trade_date}"
        / "module_status.json"
    )
    payload: dict[str, Any] = {}
    if output.exists():
        loaded = json.loads(output.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            payload = loaded
    modules = dict(payload.get("modules") or {})
    modules[module] = {
        "state": state,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "raw_payload_persisted": False,
    }
    atomic_write_json(
        output,
        {
            "schema_version": 1,
            "as_of_date": trade_date,
            "modules": modules,
            "raw_payload_persisted": False,
        },
    )


def _canary(args: argparse.Namespace) -> int:
    client = _client(args)
    start = args.start or (pd.Timestamp(args.date) - pd.Timedelta(days=10)).date().isoformat()
    try:
        if args.module == "history":
            frame = client.fetch_daily_history(
                [str(args.code).upper()],
                start,
                args.date,
                batch_size=1,
                request_interval_seconds=0,
            )
            payload = {
                "module": args.module,
                "state": "ready" if not frame.empty else "missing",
                "rows": int(len(frame)),
                "source_dates": sorted(frame.get("trade_date", pd.Series(dtype=str)).dropna().astype(str).unique().tolist()),
                "columns": list(frame.columns),
                "raw_payload_persisted": False,
            }
        elif args.module == "adjustment":
            frame = client.fetch_adjustment_factors(
                [str(args.code).upper()],
                start,
                args.date,
                known_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                batch_size=1,
                request_interval_seconds=0,
            )
            factors = pd.to_numeric(frame.get("adj_factor"), errors="coerce")
            payload = {
                "module": args.module,
                "state": "ready" if not frame.empty and factors.gt(0).all() else "missing",
                "rows": int(len(frame)),
                "source_dates": sorted(frame.get("trade_date", pd.Series(dtype=str)).dropna().astype(str).unique().tolist()),
                "factor_min": None if factors.empty else float(factors.min()),
                "factor_max": None if factors.empty else float(factors.max()),
                "method": "ifind_forward1_dividend_plan",
                "raw_payload_persisted": False,
            }
        else:
            if args.spec is None:
                payload = {
                    "module": args.module,
                    "state": "not_configured",
                    "ok": False,
                    "reason": "official public documentation does not expose a stable indicator id; provide --spec",
                    "raw_payload_persisted": False,
                }
                if args.record_status:
                    _record_canary_status(
                        args.data_dir, args.module, args.date, "not_configured"
                    )
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 2
            loaded = json.loads(args.spec.read_text(encoding="utf-8"))
            specs = _replace_spec_placeholders(loaded.get("indicators") or [], args.date)
            frame = client.fetch_basic_indicators([str(args.code).upper()], specs)
            requested = [str(item.get("indicator")) for item in specs]
            payload = {
                "module": args.module,
                "state": "ready" if not frame.empty else "missing",
                "rows": int(len(frame)),
                "requested_indicators": requested,
                "returned_columns": list(frame.columns),
                "non_null": {
                    field: int(frame.get(field, pd.Series(dtype=object)).notna().sum())
                    for field in requested
                },
                "raw_payload_persisted": False,
            }
        payload["ok"] = payload.get("state") == "ready"
        if args.record_status:
            _record_canary_status(
                args.data_dir, args.module, args.date, str(payload["state"])
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 1
    except Exception as exc:
        error = _safe_client_error(exc, client)
        state = "not_entitled" if _is_entitlement_error(error) else "failed"
        if args.record_status:
            _record_canary_status(
                args.data_dir, args.module, args.date, state
            )
        print(
            json.dumps(
                {
                    "module": args.module,
                    "state": state,
                    "ok": False,
                    "error": error,
                    "raw_payload_persisted": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1


def _run(args: argparse.Namespace) -> int:
    client = _client(args)
    config = _collection_config(args)

    def progress(phase: str, done: int, total: int) -> None:
        print(f"iFinD {phase} batches: {done}/{total}", flush=True)

    result = collect_and_publish(client, args.date, config=config, progress=progress)
    if (
        result.ok
        and result.status.get("state") != "market_closed"
        and args.refresh_adjustments
    ):
        try:
            adjustment = _refresh_adjustments(
                client,
                args.data_dir,
                args.adjustment_start
                or (pd.Timestamp(args.date) - pd.Timedelta(days=400)).date().isoformat(),
                args.date,
                args.adjustment_batch_size,
            )
            result.status["adjustment_refresh"] = adjustment
        except Exception as exc:
            result.status["adjustment_refresh"] = {
                "ok": False,
                "error": _safe_client_error(exc, client),
            }
            print(json.dumps(result.status, ensure_ascii=False, indent=2))
            return 1
    print(json.dumps(result.status, ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


def _collection_config(args: argparse.Namespace) -> CollectionConfig:
    return CollectionConfig(
        data_dir=args.data_dir,
        min_universe_size=args.min_universe_size,
        min_quote_coverage=args.min_quote_coverage,
        min_reference_coverage=args.min_reference_coverage,
        min_extended_field_coverage=args.min_extended_field_coverage,
        reference_batch_size=args.reference_batch_size,
        quote_batch_size=args.batch_size,
    )


def _refresh_adjustments(
    client: IFindHTTPClient,
    data_dir: Path,
    start_date: str,
    end_date: str,
    requested_batch_size: int,
) -> dict[str, Any]:
    if start_date > end_date:
        raise ValueError("adjustment start cannot be after end")
    partition_universe = (
        data_dir
        / "facts"
        / "reference"
        / f"as_of_date={end_date}"
        / "universe.parquet"
    )
    universe_path = (
        partition_universe
        if partition_universe.exists()
        else data_dir / "latest" / "universe.parquet"
    )
    if not universe_path.exists():
        raise FileNotFoundError("latest universe is required before adjustment refresh")
    universe = pd.read_parquet(universe_path)
    codes = universe["thscode"].dropna().astype(str).tolist()
    calendar_days = max((pd.Timestamp(end_date) - pd.Timestamp(start_date)).days + 1, 1)
    safe_batch_size = max(1, min(requested_batch_size, 45_000 // calendar_days))

    def progress(done: int, total: int) -> None:
        print(f"iFinD adjustment batches: {done}/{total}", flush=True)

    known_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    factors = client.fetch_adjustment_factors(
        codes,
        start_date,
        end_date,
        known_at=known_at,
        batch_size=safe_batch_size,
        request_interval_seconds=0.15,
        progress=progress,
    )
    output = (
        data_dir
        / "facts"
        / "adjustment"
        / f"as_of_date={end_date}"
        / "adjustment_factors.parquet"
    )
    atomic_write_parquet(output, factors)
    data_reference = build_data_reference_outputs(data_dir, end_date)
    return {
        "ok": True,
        "rows": int(len(factors)),
        "source_start": start_date,
        "source_end": end_date,
        "known_at": known_at,
        "batch_size": safe_batch_size,
        "output": str(output.resolve()),
        "data_reference": data_reference,
    }


class _PreparedBackfillClient:
    """Serve prefetched PIT frames through the daily publisher contract."""

    def __init__(
        self,
        base: IFindHTTPClient,
        universes: dict[str, pd.DataFrame],
        references: dict[str, pd.DataFrame],
        quotes: pd.DataFrame,
    ) -> None:
        self.base = base
        self.universes = universes
        self.references = references
        self.quotes = quotes
        self.collection_started_at: str | None = None
        self.collection_completed_at: str | None = None

    @property
    def refresh_token(self) -> str | None:
        return self.base.refresh_token

    @property
    def access_token(self) -> str | None:
        return self.base.access_token

    def fetch_trade_calendar(self, trade_date: str, *, offset: int) -> pd.DataFrame:
        del offset
        return pd.DataFrame(
            {
                "as_of_date": [trade_date],
                "trade_date": [trade_date],
                "calendar": ["SSE"],
                "market_code": ["212001"],
                "is_open": [True],
                "source_provider": ["ifind_http"],
                "source_endpoint": ["get_trade_dates"],
            }
        )

    def fetch_universe(self, trade_date: str) -> pd.DataFrame:
        return self.universes[trade_date].copy()

    def fetch_security_reference(
        self,
        codes: list[str],
        trade_date: str,
        *,
        batch_size: int,
        request_interval_seconds: float,
        progress=None,
    ) -> pd.DataFrame:
        del batch_size, request_interval_seconds
        if progress:
            progress(1, 1)
        frame = self.references[trade_date]
        return frame.loc[frame["thscode"].astype(str).isin(codes)].copy()

    def fetch_daily_quotes(
        self,
        codes: list[str],
        trade_date: str,
        *,
        batch_size: int,
        request_interval_seconds: float,
        progress=None,
    ) -> pd.DataFrame:
        del batch_size, request_interval_seconds
        if progress:
            progress(1, 1)
        return self.quotes.loc[
            self.quotes["trade_date"].eq(trade_date)
            & self.quotes["thscode"].astype(str).isin(codes)
        ].copy()


def _resolve_backfill_dates(
    client: IFindHTTPClient, args: argparse.Namespace
) -> tuple[list[str], list[str], str]:
    if args.sessions is not None and args.sessions < 1:
        raise ValueError("backfill sessions must be positive")
    if args.start and args.start > args.end:
        raise ValueError("backfill start cannot be after end")
    if args.sessions is not None:
        offset = -max(args.sessions + 40, 60)
    else:
        business_count = len(pd.date_range(args.start, args.end, freq="B"))
        offset = -max(business_count * 2, 60)
    calendar = client.fetch_trade_calendar(args.end, offset=offset)
    available = sorted(
        calendar.loc[
            calendar["is_open"].fillna(False).astype(bool), "trade_date"
        ]
        .dropna()
        .astype(str)
        .loc[lambda values: values.le(args.end)]
        .unique()
        .tolist()
    )
    if args.sessions is not None:
        if len(available) < args.sessions:
            raise RuntimeError(
                f"SSE calendar returned {len(available)} sessions; "
                f"{args.sessions} required"
            )
        dates = available[-args.sessions :]
        requested_start = dates[0]
        non_trading_dates: list[str] = []
    else:
        dates = [value for value in available if value >= args.start]
        requested_start = args.start
        requested_weekdays = {
            value.date().isoformat()
            for value in pd.date_range(args.start, args.end, freq="B")
        }
        non_trading_dates = sorted(requested_weekdays.difference(dates))
    if not dates:
        raise ValueError("backfill range contains no SSE trading sessions")
    return dates, non_trading_dates, requested_start


def _prepare_backfill_client(
    client: IFindHTTPClient,
    dates: list[str],
    args: argparse.Namespace,
) -> _PreparedBackfillClient:
    universes: dict[str, pd.DataFrame] = {}
    references: dict[str, pd.DataFrame] = {}
    required_quote_columns = {
        "trade_date",
        "thscode",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "avg_price",
        "volume",
        "amount",
        "turnover_ratio",
        "change_ratio",
        "source_provider",
        "source_endpoint",
    }
    # Authentication is cached before the pool starts; at most two dates are
    # in flight, matching the conservative iFinD concurrency default.
    if hasattr(client, "get_access_token"):
        client.get_access_token()

    def cached_date(
        trade_date: str,
    ) -> tuple[pd.DataFrame, pd.DataFrame] | None:
        candidate_dirs = [
            args.data_dir
            / "facts"
            / "reference"
            / f"as_of_date={trade_date}",
            args.data_dir / "snapshots" / trade_date,
        ]
        cached_paths: tuple[Path, Path] | None = None
        for reference_dir in candidate_dirs:
            universe_path = reference_dir / "universe.parquet"
            reference_path = reference_dir / "security_reference.parquet"
            if universe_path.exists() and reference_path.exists():
                cached_paths = universe_path, reference_path
                break
        if cached_paths is None:
            return None
        try:
            universe = pd.read_parquet(cached_paths[0])
            reference = pd.read_parquet(cached_paths[1])
        except (OSError, ValueError):
            return None
        universe_required = {
            "as_of_date",
            "thscode",
            "security_name",
            "security_name_in_time",
        }
        reference_required = {
            "as_of_date",
            "thscode",
            "listing_date",
            "total_shares",
            "float_a_shares",
        }
        if (
            universe.empty
            or reference.empty
            or not universe_required.issubset(universe.columns)
            or not reference_required.issubset(reference.columns)
        ):
            return None
        return universe, reference

    def cached_quotes(trade_date: str) -> pd.DataFrame | None:
        candidates = [
            args.data_dir
            / "facts"
            / "market"
            / f"trade_date={trade_date}"
            / "daily_quotes.parquet",
            args.data_dir / "snapshots" / trade_date / "daily_quotes.parquet",
        ]
        path = next((item for item in candidates if item.exists()), None)
        if path is None:
            return None
        try:
            frame = pd.read_parquet(path)
        except (OSError, ValueError):
            return None
        if frame.empty or not required_quote_columns.issubset(frame.columns):
            return None
        observed_dates = set(
            frame["trade_date"].dropna().astype(str).unique().tolist()
        )
        if observed_dates != {trade_date}:
            return None
        return frame.copy()

    def fetch_date(
        trade_date: str,
    ) -> tuple[pd.DataFrame, pd.DataFrame, bool]:
        cached = cached_date(trade_date)
        if cached is not None:
            return cached[0], cached[1], True
        universe = client.fetch_universe(trade_date)
        codes = universe["thscode"].dropna().astype(str).str.upper().tolist()
        reference = client.fetch_security_reference(
            codes,
            trade_date,
            batch_size=args.reference_batch_size,
            request_interval_seconds=0.15,
        )
        if not universe.empty and not reference.empty:
            reference_dir = (
                args.data_dir / "facts" / "reference" / f"as_of_date={trade_date}"
            )
            atomic_write_parquet(reference_dir / "universe.parquet", universe)
            atomic_write_parquet(
                reference_dir / "security_reference.parquet", reference
            )
        return universe, reference, False

    worker_count = min(2, len(dates))
    with ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="ifind-pit"
    ) as executor:
        future_dates = {
            executor.submit(fetch_date, trade_date): (index, trade_date)
            for index, trade_date in enumerate(dates, start=1)
        }
        for future in as_completed(future_dates):
            index, trade_date = future_dates[future]
            universe, reference, from_cache = future.result()
            universes[trade_date] = universe
            references[trade_date] = reference
            print(
                f"iFinD PIT universe/reference dates: {index}/{len(dates)} "
                f"({trade_date}) {'[cached]' if from_cache else ''}",
                flush=True,
            )
    quote_frames: list[pd.DataFrame] = []
    cached_quote_dates: set[str] = set()
    for trade_date in dates:
        frame = cached_quotes(trade_date)
        if frame is None:
            continue
        quote_frames.append(frame)
        cached_quote_dates.add(trade_date)
        print(f"iFinD history date: {trade_date} [cached]", flush=True)

    missing_groups: list[list[str]] = []
    active_group: list[str] = []
    for trade_date in dates:
        if trade_date in cached_quote_dates:
            if active_group:
                missing_groups.append(active_group)
                active_group = []
            continue
        active_group.append(trade_date)
    if active_group:
        missing_groups.append(active_group)
    date_windows = [
        group[start : start + 63]
        for group in missing_groups
        for start in range(0, len(group), 63)
    ]
    for window_index, window in enumerate(date_windows, start=1):
        window_codes = sorted(
            {
                str(code).strip().upper()
                for trade_date in window
                for code in universes[trade_date]["thscode"].dropna().astype(str)
                if str(code).strip()
            }
        )
        safe_batch = max(
            1,
            min(
                args.batch_size,
                45_000 // max(len(window) * len(QUOTE_FIELDS), 1),
            ),
        )

        def progress(done: int, total: int, *, number: int = window_index) -> None:
            print(
                f"iFinD history window {number}/{len(date_windows)} batches: "
                f"{done}/{total}",
                flush=True,
            )

        quote_frames.append(
            client.fetch_daily_history(
                window_codes,
                window[0],
                window[-1],
                batch_size=safe_batch,
                request_interval_seconds=0.15,
                progress=progress,
            )
        )
    quotes = (
        pd.concat(quote_frames, ignore_index=True)
        if quote_frames
        else pd.DataFrame()
    )
    missing_quote_columns = sorted(required_quote_columns.difference(quotes.columns))
    if missing_quote_columns:
        raise RuntimeError(
            "range history response missing columns: "
            + ", ".join(missing_quote_columns)
        )
    quotes = quotes.loc[quotes["trade_date"].isin(dates)].copy()
    return _PreparedBackfillClient(client, universes, references, quotes)


def _backfill(args: argparse.Namespace) -> int:
    client = _client(args)
    config = _collection_config(args)
    initial_manifest_path = args.data_dir / "latest" / "manifest.json"
    try:
        initial_manifest = load_manifest(initial_manifest_path)
    except Exception as exc:
        error = _safe_client_error(exc, client)
        print(
            json.dumps(
                {"ok": False, "state": "invalid_latest_manifest", "error": error},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    initial_latest_date = initial_manifest.get("trade_date")
    completed: list[str] = []

    def fail_backfill(
        error: str, *, failed_date: str | None = None
    ) -> int:
        status = {
            "schema_version": 2,
            "state": "failed_backfill",
            "data_fresh": False,
            "requested_start": args.start,
            "requested_sessions": args.sessions,
            "requested_end": args.end,
            "failed_date": failed_date,
            "completed_trade_dates_not_published": completed,
            "last_valid_trade_date": initial_latest_date,
            "provider": "ifind_http",
            "completed_at_utc": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "raw_payload_persisted": False,
            "error": error,
        }
        write_run_status(args.data_dir, status)
        print(json.dumps({"ok": False, "status": status}, ensure_ascii=False, indent=2))
        return 1

    try:
        dates, non_trading_dates, requested_start = _resolve_backfill_dates(
            client, args
        )
        collection_started_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        prepared = _prepare_backfill_client(client, dates, args)
        prepared.collection_started_at = collection_started_at
        prepared.collection_completed_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
    except Exception as exc:
        return fail_backfill(_safe_client_error(exc, client))

    def progress(phase: str, done: int, total: int) -> None:
        print(f"iFinD {phase} batches: {done}/{total}", flush=True)

    for trade_date in dates:
        print(f"Backfill date: {trade_date}", flush=True)
        date_config = replace(
            config, build_data_reference=trade_date == dates[-1]
        )
        result = collect_and_publish(
            prepared, trade_date, config=date_config, progress=progress
        )
        if not result.ok:
            return fail_backfill(
                str(result.status.get("error") or result.status.get("state")),
                failed_date=trade_date,
            )
        completed.append(trade_date)
    adjustment: dict[str, Any] | None = None
    if args.with_adjustment_snapshot and completed:
        try:
            adjustment = _refresh_adjustments(
                client,
                args.data_dir,
                requested_start,
                completed[-1],
                args.adjustment_batch_size,
            )
        except Exception as exc:
            return fail_backfill(
                _safe_client_error(exc, client), failed_date=completed[-1]
            )
    latest_manifest_path = args.data_dir / "latest" / "manifest.json"
    try:
        latest_manifest = load_manifest(latest_manifest_path)
    except Exception as exc:
        return fail_backfill(_safe_client_error(exc, client))
    aggregate_state = "success_backfill" if completed else "market_closed"
    write_run_status(
        args.data_dir,
        {
            "schema_version": 2,
            "state": aggregate_state,
            "data_fresh": bool(completed),
            "requested_start": requested_start,
            "requested_end": args.end,
            "completed_trade_dates": completed,
            "completed_count": len(completed),
            "non_trading_dates": non_trading_dates,
            "last_valid_trade_date": latest_manifest.get("trade_date"),
            "provider": "ifind_http",
            "completed_at_utc": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "raw_payload_persisted": False,
        },
    )
    payload = {
        "ok": True,
        "state": aggregate_state,
        "requested_start": requested_start,
        "requested_end": args.end,
        "completed_trade_dates": completed,
        "completed_count": len(completed),
        "non_trading_dates": non_trading_dates,
        "adjustment_snapshot": adjustment,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _build_data_reference(args: argparse.Namespace) -> int:
    payload = build_data_reference_outputs(
        args.data_dir,
        args.date,
        min_adt20=args.min_adt20,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _dashboard(args: argparse.Namespace) -> int:
    ok, validation = validate_latest(data_dir=args.data_dir)
    if not ok:
        print(json.dumps(validation, ensure_ascii=False, indent=2))
        return 1
    output = args.output or args.data_dir / "latest" / "data_reference.html"
    if (args.data_dir / "latest" / "data_reference_latest.json").exists():
        built = build_data_reference_dashboard(args.data_dir, output)
        dashboard_type = "data_reference"
    else:
        built = build_dashboard(args.data_dir, output)
        dashboard_type = "market_snapshot"
    print(
        json.dumps(
            {
                "ok": True,
                "trade_date": validation.get("trade_date"),
                "dashboard_type": dashboard_type,
                "output": str(built.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "probe":
        return _probe(args)
    if args.command == "canary":
        return _canary(args)
    if args.command == "run":
        return _run(args)
    if args.command == "backfill":
        return _backfill(args)
    if args.command == "validate":
        ok, payload = validate_latest(data_dir=args.data_dir)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    if args.command == "dashboard":
        return _dashboard(args)
    if args.command in {"build-state", "build-report"}:
        return _build_data_reference(args)
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
