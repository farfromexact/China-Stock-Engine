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
    run.add_argument("--batch-size", type=int, default=300)

    validate = subparsers.add_parser("validate", help="validate the latest snapshot")
    validate.add_argument("--data-dir", type=Path, default=Path("data"))
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
                "volume": "volume",
                "amount": "amount",
                "changeRatio": "change_ratio",
            }.items()
        }
        ok = len(frame) > 0 and args.date in source_dates
        payload = {
            "auth_ok": bool(client.access_token),
            "quote_ok": ok,
            "requested_code": str(args.code).upper(),
            "requested_trade_date": args.date,
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
        quote_batch_size=args.batch_size,
    )

    def progress(done: int, total: int) -> None:
        print(f"iFinD quote batches: {done}/{total}", flush=True)

    result = collect_and_publish(client, args.date, config=config, progress=progress)
    print(json.dumps(result.status, ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


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
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
