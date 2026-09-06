"""Bounded live iFinD adapters with normalized checkpoints and no raw payloads.

Only publicly documented financial identifiers are enabled. Missing fields,
empty announcement queries, and historical knowledge are never fabricated.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .ifind_http import IFindHTTPClient, IFindHTTPError, _tables_frame, _raise_api_error
from .research_inputs import normalize_research_batch
from .storage import (
    ArtifactContractError, atomic_write_json, atomic_write_parquet,
    json_sha256, load_json_object, load_manifest, sha256_file,
)

ADAPTER_VERSION = "ifind_supplemental_v1"
CANARY_CODES = ["000001.SZ", "300033.SZ", "600000.SH"]
FINANCIAL_INDICATOR = "ths_np_atoopc_pit_stock"
PUBLICATION_INDICATOR = "ths_regular_report_actual_dd_stock"
DOC_URL = "https://quantapi.51ifind.com/gwstatic/static/ds_web/quantapi-web/example.html"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_failure(exc: Exception) -> dict:
    # Do not persist vendor errmsg: it may echo authentication or a raw response.
    match = re.search(r"code (-?\d+)", str(exc))
    code = int(match[1]) if match else None
    result = {"state": "failed", "error_type": type(exc).__name__, "api_error_code": code}
    if isinstance(exc, ArtifactContractError):
        result["reason"] = str(exc)[:200]
    else:
        message = str(exc).lower()
        for fragment, reason in (
            ("device exceed limit", "device_ip_binding_limit"),
            ("refresh_token is expired", "refresh_token_rejected"),
            ("budget exhausted", "request_budget_exhausted"),
        ):
            if fragment in message:
                result["reason"] = reason
                break
    return result


class BoundedClient(IFindHTTPClient):
    """Audit only endpoint/count/dataVol; never retain request/response payloads."""

    def __init__(self, *, max_requests=12, **kwargs):
        super().__init__(max_transport_attempts=1, **kwargs)
        self.max_requests = max_requests
        self.audit: list[dict] = []
        self.renewal_attempted = False

    def renew_access_token_once(self):
        """Explicitly authorized recovery; invalidates old access tokens, not refresh."""
        if self.renewal_attempted:
            raise IFindHTTPError("access token renewal was already attempted")
        self.renewal_attempted = True
        refresh = self.refresh_token or os.environ.get("IFIND_REFRESH_TOKEN")
        if not refresh:
            raise IFindHTTPError("refresh token is required for authorized renewal")
        response = self._send(f"{self.base_url}/update_access_token", {"refresh_token": refresh}, None)
        _raise_api_error("update_access_token", response)
        token = (response.get("data") or {}).get("access_token")
        if not token:
            raise IFindHTTPError("renewal response contained no access token")
        self.access_token = str(token)

    def request(self, endpoint: str, payload: dict[str, Any]) -> dict:
        if len(self.audit) >= self.max_requests:
            raise IFindHTTPError("supplemental request budget exhausted")
        entry = {"endpoint": endpoint, "success": False, "data_volume": None}
        self.audit.append(entry)
        result = super().request(endpoint, payload)
        entry["success"] = True
        value = result.get("dataVol")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            entry["data_volume"] = value
        return result


def _timestamp(value: Any, *, date_only_end: bool = True) -> str:
    if value is None or pd.isna(value):
        raise ArtifactContractError("source publication timestamp is missing")
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    stamp = pd.Timestamp(text)
    if pd.isna(stamp):
        raise ArtifactContractError("source publication timestamp is invalid")
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("Asia/Shanghai")
    if date_only_end and re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}", text):
        stamp += pd.Timedelta(hours=23, minutes=59, seconds=59)
    return stamp.tz_convert("UTC").isoformat()


def _envelope(module, codes, start, end, started, completed, rows):
    return normalize_research_batch({
        "schema_version": 1, "module": module,
        "collection_started_at": started, "collection_completed_at": completed,
        "coverage": {"thscodes": codes, "complete": False,
                     "period_start": start, "period_end": end},
        "records": rows,
    })


def collect_financials(client, codes, report_period, as_of_date) -> dict:
    started = utc_now()
    frame = client.fetch_basic_indicators(codes, [
        {"indicator": FINANCIAL_INDICATOR,
         "indiparams": [as_of_date, report_period.replace("-", ""), "1"]},
        {"indicator": PUBLICATION_INDICATOR,
         "indiparams": [report_period.replace("-", "")]},
    ])
    completed = utc_now()
    required = {"thscode", FINANCIAL_INDICATOR, PUBLICATION_INDICATOR}
    if not required.issubset(frame.columns) or frame.duplicated("thscode").any():
        missing = sorted(required - set(frame.columns))
        raise ArtifactContractError("financial schema mismatch; missing=" + ",".join(missing))
    rows = []
    for record in frame.to_dict("records"):
        value = record[FINANCIAL_INDICATOR]
        if pd.isna(value) or pd.isna(record[PUBLICATION_INDICATOR]):
            continue
        published = _timestamp(record[PUBLICATION_INDICATOR])
        if pd.Timestamp(published) > pd.Timestamp(completed):
            continue
        values = {"net_profit_parent": float(value)}
        rows.append({
            "thscode": str(record["thscode"]).upper(),
            "published_at": published, "first_seen_at": completed, "known_at": completed,
            "revision": f"ifind_pit_{as_of_date}_{json_sha256(values)[:16]}",
            "source_url": DOC_URL, "document_sha256": None,
            "report_period": report_period, "period_start": report_period[:4] + "-01-01",
            "period_basis": "YTD", "accounting_scope": "consolidated",
            "currency": "CNY", "unit": "CNY", "values": values,
        })
    if not rows:
        raise ArtifactContractError("financial query returned no usable dated observation")
    return _envelope("financials", codes, report_period, report_period, started, completed, rows)


def collect_events(client, codes, start, end) -> dict:
    started = utc_now()
    # No reportType filter: preserve all categories, not only earnings reports.
    response = client.request("report_query", {
        "codes": ",".join(codes), "functionpara": {},
        "beginrDate": start, "endrDate": end,
        "outputpara": "reportDate:Y,thscode:Y,ctime:Y,reportTitle:Y,pdfURL:Y,seq:Y",
    })
    frame = _tables_frame(response)
    completed = utc_now()
    rows = []
    if not frame.empty:
        required = {"thscode", "reportDate", "ctime", "reportTitle", "pdfURL", "seq"}
        if not required.issubset(frame.columns):
            raise ArtifactContractError("announcement response schema mismatch")
        # No documented pagination-completeness guarantee: never confirm absence.
        for item in frame.to_dict("records"):
            published = _timestamp(item["ctime"])
            published_date = pd.Timestamp(published).tz_convert("Asia/Shanghai").date().isoformat()
            if not start <= published_date <= end or pd.Timestamp(published) > pd.Timestamp(completed):
                continue
            url = str(item["pdfURL"])
            if url.startswith("http://"):
                url = "https://" + url[len("http://"):]
            if pd.isna(item["seq"]) or pd.isna(item["reportTitle"]):
                raise ArtifactContractError("announcement identity/title missing")
            fact = {"title": str(item["reportTitle"]), "url": url, "published": published}
            rows.append({
                "thscode": str(item["thscode"]).upper(), "published_at": published,
                "first_seen_at": completed, "known_at": completed,
                "revision": "ifind_" + json_sha256(fact)[:32],
                "source_url": url, "document_sha256": None,
                "event_id": "ifind_" + str(item["seq"]), "event_type": "filing",
                "event_date": pd.Timestamp(_timestamp(item["reportDate"])).tz_convert("Asia/Shanghai").date().isoformat(),
                "status": "unknown", "title": fact["title"],
            })
    return _envelope("events", codes, start, end, started, completed, rows)


def _save_batch(data_dir, batch):
    digest = json_sha256(batch)
    path = data_dir / "facts" / "research" / batch["module"] / f"{digest}.json"
    if not path.exists():
        atomic_write_json(path, batch, compact=True)
    elif load_json_object(path, missing_ok=False) != batch:
        raise ArtifactContractError("research batch identity conflict")
    return {"path": path.relative_to(data_dir).as_posix(), "sha256": sha256_file(path),
            "rows": len(batch["records"]), "completed_at": batch["collection_completed_at"]}


def _cached_closes(data_dir, trade_date, codes):
    paths = sorted((data_dir / "facts" / "market").glob("trade_date=*/daily_quotes.parquet"))
    paths = [path for path in paths if path.parent.name[11:] <= trade_date][-21:]
    if not paths:
        raise ArtifactContractError("no cached market history; refusing redundant download")
    frames = [pd.read_parquet(path, columns=["trade_date", "thscode", "close"]) for path in paths]
    frame = pd.concat(frames, ignore_index=True)
    return frame.loc[frame["thscode"].isin(codes)].sort_values(["trade_date", "thscode"])


def collect_adjustments(client, data_dir, codes, trade_date, *, start_date=None, batch_size=100):
    raw = _cached_closes(data_dir, trade_date, codes)
    if start_date:
        raw = raw.loc[raw["trade_date"].ge(start_date)]
    if raw.empty:
        raise ArtifactContractError("no cached raw prices in requested adjustment window")
    factors = client.fetch_adjustment_factors(
        codes, str(raw["trade_date"].min()), trade_date, raw_prices=raw,
        batch_size=batch_size, request_interval_seconds=0.5,
    )
    if len(factors) != len(raw) or factors["adj_factor"].isna().any():
        raise ArtifactContractError("adjustment coverage does not match cached raw observations")
    completed = str(factors["known_at"].max())
    # A vintage is partitioned by actual observation date, NOT the price date.
    as_of = pd.Timestamp(completed).tz_convert("Asia/Shanghai").date().isoformat()
    digest = json_sha256(json.loads(factors.to_json(orient="records", double_precision=15)))
    path = data_dir / "facts" / "adjustment" / f"as_of_date={as_of}" / f"vintage={digest}" / "adjustment_factors.parquet"
    if not path.exists():
        atomic_write_parquet(path, factors)
    return {"path": path.relative_to(data_dir).as_posix(), "sha256": sha256_file(path),
            "rows": len(factors), "completed_at": completed, "source_start": str(raw["trade_date"].min()),
            "source_end": trade_date, "codes": len(codes), "raw_prices_reused": True}


def _verify_ref(data_dir, metadata):
    path = (data_dir / metadata["path"]).resolve()
    if not path.is_relative_to(data_dir.resolve()) or not path.is_file() or sha256_file(path) != metadata["sha256"]:
        raise ArtifactContractError("supplemental checkpoint artifact missing or corrupt")


def run_collection(client, data_dir: Path, *, modules, codes, trade_date, as_of_date,
                   event_days=7, report_period=None) -> dict:
    if not codes or len(codes) > 100 or any(not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code) for code in codes):
        raise ValueError("supplemental scope must be 1..100 explicit A-share codes")
    if not 1 <= event_days <= 31:
        raise ValueError("event window must be 1..31 calendar days")
    codes = sorted(set(codes))
    if report_period is None:
        report_period = (pd.Timestamp(trade_date).to_period("Q").start_time - pd.Timedelta(days=1)).date().isoformat()
    start = (pd.Timestamp(trade_date) - pd.Timedelta(days=event_days - 1)).date().isoformat()
    statuses = {}
    for module in modules:
        identity = {"adapter": ADAPTER_VERSION, "module": module, "codes": codes,
                    "trade_date": trade_date, "as_of_date": as_of_date,
                    "event_start": start, "report_period": report_period}
        if module == "adjustment":
            raw = _cached_closes(data_dir, trade_date, codes)
            identity["raw_sha256"] = json_sha256(raw.to_dict("records"))
        key = json_sha256(identity)
        receipt_path = data_dir / "facts" / "collection_receipts" / f"{key}.json"
        try:
            if receipt_path.exists():
                receipt = load_json_object(receipt_path, missing_ok=False)
                if receipt.get("schema_version") != 1 or receipt.get("request") != identity:
                    raise ArtifactContractError("incompatible supplemental checkpoint")
                _verify_ref(data_dir, receipt["artifact"])
                statuses[module] = {"state": "success", "reused": True, **receipt["artifact"]}
                continue
            if module == "financials":
                metadata = _save_batch(data_dir, collect_financials(client, codes, report_period, as_of_date))
            elif module == "events":
                metadata = _save_batch(data_dir, collect_events(client, codes, start, trade_date))
            elif module == "adjustment":
                metadata = collect_adjustments(client, data_dir, codes, trade_date)
            else:
                raise ValueError("unsupported supplemental module")
            atomic_write_json(receipt_path, {"schema_version": 1, "request": identity, "artifact": metadata})
            statuses[module] = {"state": "success", "reused": False, **metadata}
        except Exception as exc:
            statuses[module] = safe_failure(exc)
            # Never retry authentication/quota failures for every module.
            if statuses[module]["api_error_code"] in {-1301, -1303, -4318}:
                break
    ok = len(statuses) == len(modules) and all(row["state"] == "success" for row in statuses.values())
    status = {"schema_version": 1, "ok": ok, "adapter_version": ADAPTER_VERSION,
              "market_trade_date": trade_date, "as_of_date": as_of_date, "scope_codes": codes,
              "modules": statuses, "requests": getattr(client, "audit", []),
              "raw_payload_persisted": False}
    atomic_write_json(data_dir / "supplemental_last_attempt.json", status)
    if ok:
        pointer = {key: value for key, value in status.items() if key != "requests"}
        pointer["modules"] = {key: {k: v for k, v in item.items() if k != "reused"} for key, item in statuses.items()}
        atomic_write_json(data_dir / "latest" / "supplemental_manifest.json", pointer)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--scope", choices=["canary", "radar"], default="canary")
    parser.add_argument("--modules", default="financials,events,adjustment")
    parser.add_argument("--max-requests", type=int, default=12)
    parser.add_argument("--event-days", type=int, default=7)
    parser.add_argument("--auth-only", action="store_true")
    parser.add_argument("--renew-access-token", action="store_true",
                        help="explicit authorization required; invalidates other old access tokens")
    args = parser.parse_args(argv)
    manifest = load_manifest(args.data_dir / "latest" / "manifest.json", missing_ok=False)
    codes = CANARY_CODES
    if args.scope == "radar":
        radar = load_json_object(args.data_dir / "latest" / "opportunity_radar_latest.json", missing_ok=False)
        codes = [row["thscode"] for row in radar["candidate_union"]]
    modules = args.modules.split(",")
    if not modules or len(set(modules)) != len(modules) or set(modules) - {"financials", "events", "adjustment"}:
        raise ValueError("invalid modules")
    client = BoundedClient(max_requests=args.max_requests)
    if args.renew_access_token:
        try:
            client.renew_access_token_once()
            renewal = {"ok": True, "state": "access_token_renewed", "refresh_token_changed": False}
        except Exception as exc:
            renewal = {"ok": False, **safe_failure(exc), "retry_performed": False}
        atomic_write_json(args.data_dir / "supplemental_auth_status.json", renewal)
        print(json.dumps(renewal))
        if not renewal["ok"]:
            return 1
    if args.auth_only:
        try:
            client.get_access_token()
            result = {"ok": True, "state": "access_token_obtained", "data_requests": 0}
        except Exception as exc:
            result = {"ok": False, **safe_failure(exc), "data_requests": 0}
        atomic_write_json(args.data_dir / "supplemental_auth_status.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    result = run_collection(client, args.data_dir, modules=modules, codes=codes,
                            trade_date=manifest["trade_date"],
                            as_of_date=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
                            event_days=args.event_days)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
