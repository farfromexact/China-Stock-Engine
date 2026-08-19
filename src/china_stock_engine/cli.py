"""Command-line interface for collection, probing, and validation."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from getpass import getpass
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .dashboard import build_dashboard
from .ifind_http import IFindHTTPClient, QUOTE_FIELDS
from .pipeline import CollectionConfig, collect_and_publish, validate_latest


SHANGHAI = ZoneInfo("Asia/Shanghai")


def default_trade_date() -> str:
    now = datetime.now(SHANGHAI)
    candidate = now.date() if now.hour >= 18 else now.date() - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate.isoformat()


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

    validate = subparsers.add_parser("validate", help="validate the latest snapshot")
    validate.add_argument("--data-dir", type=Path, default=Path("data"))

    dashboard = subparsers.add_parser(
        "dashboard", help="build an offline HTML dashboard from latest"
    )
    dashboard.add_argument("--data-dir", type=Path, default=Path("data"))
    dashboard.add_argument("--output", type=Path)
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


def _run(args: argparse.Namespace) -> int:
    client = _client(args)
    config = CollectionConfig(
        data_dir=args.data_dir,
        min_universe_size=args.min_universe_size,
        min_quote_coverage=args.min_quote_coverage,
        min_reference_coverage=args.min_reference_coverage,
        min_extended_field_coverage=args.min_extended_field_coverage,
        reference_batch_size=args.reference_batch_size,
        quote_batch_size=args.batch_size,
    )

    def progress(phase: str, done: int, total: int) -> None:
        print(f"iFinD {phase} batches: {done}/{total}", flush=True)

    result = collect_and_publish(client, args.date, config=config, progress=progress)
    print(json.dumps(result.status, ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


def _dashboard(args: argparse.Namespace) -> int:
    ok, validation = validate_latest(data_dir=args.data_dir)
    if not ok:
        print(json.dumps(validation, ensure_ascii=False, indent=2))
        return 1
    output = args.output or args.data_dir / "latest" / "market_dashboard.html"
    built = build_dashboard(args.data_dir, output)
    print(
        json.dumps(
            {
                "ok": True,
                "trade_date": validation.get("trade_date"),
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
    if args.command == "run":
        return _run(args)
    if args.command == "validate":
        ok, payload = validate_latest(data_dir=args.data_dir)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    if args.command == "dashboard":
        return _dashboard(args)
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
