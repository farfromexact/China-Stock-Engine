"""Versioned, canary-gated iFinD research enrichment on the existing facts store.

Request templates must come from the provider's documented SuperCommand catalog.
No guessed indicators, raw payload storage, automatic token rotation or opinions.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
from string import Formatter
from zoneinfo import ZoneInfo

import pandas as pd

from .ifind_http import _tables_frame
from .ifind_supplemental import BoundedClient, CANARY_CODES, _timestamp, _document_link, _save_batch, utc_now, safe_failure
from .research_inputs import FINANCIAL_V2_FIELDS, FORECAST_FIELDS, EVENT_DETAIL_FIELDS, normalize_research_batch
from .storage import ArtifactContractError, atomic_write_json, atomic_write_parquet, json_sha256, load_json_object, load_manifest, sha256_file

VERSION = "ifind_enrichment_v1"
MODULES = {"financial_history", "industry", "tradability", "index_prices", "structured_events", "forecasts"}
PLACEHOLDERS = {"codes", "trade_date", "compact_date", "as_of_date", "report_period", "compact_period", "start", "end", "forecast_year"}
ENDPOINTS = {"basic_data_service", "data_pool", "cmd_history_quotation", "report_query"}
FINANCIAL_METADATA = {"thscode", "published_at"}
FRAME_FIELDS = {
    "industry": {"thscode", "sw1_code", "sw1_name", "sw2_code", "sw2_name"},
    "tradability": {"thscode", "is_st", "is_suspended", "daily_price_limit_pct", "lot_size", "limit_up_price", "limit_down_price", "margin_eligible", "short_sell_eligible"},
    "index_prices": {"thscode", "trade_date", "close", "open", "high", "low"},
}
EVENT_METADATA = {"thscode", "published_at", "event_id", "event_type", "event_date", "status", "title", "report_period", "source_url"}
FORECAST_METADATA = {"thscode", "published_at", "institution_id", "contributor_count", "forecast_year", "estimate_basis"}


def _templates(value, context=None):
    if isinstance(value, dict):
        return {key: _templates(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_templates(item, context) for item in value]
    if isinstance(value, str):
        for _, key, spec, conversion in Formatter().parse(value):
            if key is not None and (key not in PLACEHOLDERS or spec or conversion):
                raise ArtifactContractError("unapproved request template placeholder")
        return value.format(**context) if context is not None else value
    if value is None or type(value) in {bool, int, float}:
        return value
    raise ArtifactContractError("unsupported request template value")


def load_config(path: Path) -> dict:
    config = load_json_object(path, missing_ok=False)
    if set(config) != {"schema_version", "publication_permitted", "financial_periods", "profiles"} or config["schema_version"] != 1:
        raise ArtifactContractError("incompatible enrichment configuration")
    if config["publication_permitted"] is not True:
        raise ArtifactContractError("publication permission must be explicitly confirmed")
    if type(config["financial_periods"]) is not int or not 1 <= config["financial_periods"] <= 8:
        raise ArtifactContractError("financial history must be 1..8 report periods")
    if not isinstance(config["profiles"], dict) or set(config["profiles"]) - MODULES:
        raise ArtifactContractError("invalid enrichment profiles")
    for module, profile in config["profiles"].items():
        if profile is None:
            continue
        required = {"endpoint", "request", "fields", "source_documentation", "scope_codes", "ttl_days"}
        if set(profile) != required or profile["endpoint"] not in ENDPOINTS:
            raise ArtifactContractError("profile requires an explicit documented request and field map")
        if _document_link(profile["source_documentation"])["source_url"] != profile["source_documentation"]:
            raise ArtifactContractError("profile documentation URL is invalid")
        if type(profile["ttl_days"]) is not int or not 0 <= profile["ttl_days"] <= 7:
            raise ArtifactContractError("invalid enrichment cache TTL")
        if module != "financial_history" and profile["ttl_days"]:
            raise ArtifactContractError("only financial statements support multi-day cache reuse")
        explicit = profile["scope_codes"]
        if explicit is not None and (module != "index_prices" or not isinstance(explicit, list)
                                    or not 1 <= len(explicit) <= 8
                                    or any(not re.fullmatch(r"\d{6}\.(SH|SZ|CSI)", code) for code in explicit)):
            raise ArtifactContractError("only benchmark profiles may override the bounded security scope")
        _templates(profile["request"])
        allowed = (set(FINANCIAL_V2_FIELDS) | FINANCIAL_METADATA if module == "financial_history" else
                   EVENT_METADATA | EVENT_DETAIL_FIELDS if module == "structured_events" else
                   FORECAST_METADATA | FORECAST_FIELDS if module == "forecasts" else FRAME_FIELDS[module])
        if not isinstance(profile["fields"], dict) or set(profile["fields"]) - allowed:
            raise ArtifactContractError("unknown normalized field in profile")
        if "thscode" not in profile["fields"]:
            raise ArtifactContractError("profile must map an explicit security identity")
        for field, spec in profile["fields"].items():
            if not isinstance(spec, dict) or set(spec) - {"source", "constant", "type", "scale", "enum"}:
                raise ArtifactContractError("invalid typed field mapping")
            if ("source" in spec) == ("constant" in spec) or spec.get("type") not in {"number", "integer", "text", "boolean"}:
                raise ArtifactContractError("field needs source or constant and a supported type")
            if "constant" in spec and field not in {"estimate_basis", "event_type"}:
                raise ArtifactContractError("observed financial, timing and trading facts cannot be constants")
            if spec["type"] == "boolean" and "enum" not in spec:
                raise ArtifactContractError("provider booleans need an explicit true/false mapping")
            scale = spec.get("scale", 1)
            if type(scale) not in {int, float} or scale not in {1, 100, 10000, 100000000, .01, .0001}:
                raise ArtifactContractError("unit scale must be explicit and supported")
    return config


def _decode(frame: pd.DataFrame, fields: dict) -> list[dict]:
    needed = {name for spec in fields.values() for name in
              ([spec["source"]] if isinstance(spec.get("source"), str) else spec.get("source", []))}
    if not needed <= set(frame.columns):
        raise ArtifactContractError("provider response is missing mapped fields")
    rows = []
    for raw in frame.to_dict("records"):
        row = {}
        for field, spec in fields.items():
            source = spec.get("source")
            if isinstance(source, list):
                pieces = [raw[key] for key in source]
                value = None if any(pd.isna(item) for item in pieces) else sum(float(item) for item in pieces)
            else:
                value = spec.get("constant") if source is None else raw[source]
            if value is None or pd.isna(value) or value in ("", "--", "N/A"):
                row[field] = None
                continue
            if "enum" in spec:
                if str(value) not in spec["enum"]:
                    raise ArtifactContractError("unrecognized provider enum; unknown is not false")
                value = spec["enum"][str(value)]
            kind = spec["type"]
            if kind == "boolean":
                if value is not None and type(value) is not bool:
                    raise ArtifactContractError("invalid mapped nullable boolean")
            elif kind in {"number", "integer"}:
                if type(value) is bool:
                    raise ArtifactContractError("boolean is not an amount")
                value = float(value) * spec.get("scale", 1)
                if not pd.notna(value) or abs(value) == float("inf"):
                    raise ArtifactContractError("non-finite provider number")
                if kind == "integer":
                    if not value.is_integer():
                        raise ArtifactContractError("provider integer is fractional")
                    value = int(value)
            else:
                value = str(value).strip()
            row[field] = value
        rows.append(row)
    return rows


def report_periods(trade_date: str, count: int) -> list[str]:
    last = pd.Timestamp(trade_date).to_period("Q") - 1
    return [(last - offset).end_time.date().isoformat() for offset in range(count)]


def _research_batch(module, rows, codes, context, started, completed, doc):
    records = []
    for row in rows:
        if not row.get("published_at"):
            continue
        published = _timestamp(row["published_at"])
        if pd.Timestamp(published) > pd.Timestamp(completed):
            continue
        common = {"thscode": row["thscode"], "published_at": published,
                  "first_seen_at": completed, "known_at": completed,
                  "revision": "enrich_" + json_sha256(row)[:40], "document_sha256": None,
                  "source_url": doc, "source_url_kind": "provider_query_documentation", "document_access": "not_resolved"}
        if module == "financial_history":
            values = {field: row.get(field) for field in FINANCIAL_V2_FIELDS}
            if not any(value is not None for value in values.values()):
                continue
            records.append({**common, "report_period": context["report_period"],
                            "period_start": context["report_period"][:4] + "-01-01", "period_basis": "YTD",
                            "accounting_scope": "consolidated", "currency": "CNY", "unit": "CNY", "values": values})
        elif module == "forecasts":
            records.append({**common, "forecast_year": row.get("forecast_year"),
                            "institution_id": row.get("institution_id"), "estimate_basis": row.get("estimate_basis"),
                            "contributor_count": row.get("contributor_count"), "unit": "CNY",
                            "values": {field: row.get(field) for field in sorted(FORECAST_FIELDS)}})
        else:
            if row.get("source_url"):
                common.update(_document_link(row["source_url"]))
            records.append({**common, "event_id": row.get("event_id"), "event_type": row.get("event_type", "filing"),
                            "event_date": row.get("event_date"), "status": row.get("status") or "unknown",
                            "title": row.get("title"), "report_period": row.get("report_period"),
                            "details": {field: row[field] for field in sorted(EVENT_DETAIL_FIELDS & row.keys())},
                            "details_source": "provider_structured_fields"})
    target = {"financial_history": "financials", "structured_events": "events", "forecasts": "forecasts"}[module]
    if not records:
        raise ArtifactContractError("no usable dated observations; empty is not confirmed absence")
    return normalize_research_batch({
        "schema_version": 2, "module": target, "collection_started_at": started, "collection_completed_at": completed,
        "coverage": {"thscodes": codes, "complete": False,
                     "period_start": context["report_period"] if module == "financial_history" else context["start"],
                     "period_end": context["report_period"] if module == "financial_history" else context["end"]},
        "records": records,
    })


def _save_frame(root, module, rows, trade_date, completed):
    category, filename = {"industry": ("classification", "industry_membership.parquet"),
                          "tradability": ("tradability", "provider_tradability.parquet"),
                          "index_prices": ("index", "index_quotes.parquet")}[module]
    facts = []
    for row in rows:
        common = {"thscode": row["thscode"], "known_at": completed, "collection_completed_at": completed}
        if module == "industry":
            for level in (1, 2):
                code, name = row.get(f"sw{level}_code"), row.get(f"sw{level}_name")
                if code is not None and name is not None:
                    facts.append({**common, "classification_system": "SW", "level": str(level),
                                  "industry_code": code, "industry_name": name,
                                  "effective_from": trade_date, "effective_to": trade_date})
        elif module == "tradability":
            if not any(row.get(field) is not None for field in FRAME_FIELDS[module] - {"thscode"}):
                continue
            facts.append({**{field: row.get(field) for field in FRAME_FIELDS[module]}, **common, "as_of_date": trade_date})
        else:
            if row.get("close") is None or row["close"] <= 0 or row.get("trade_date") is None:
                raise ArtifactContractError("invalid benchmark date/close")
            facts.append({**row, **common})
    if not facts:
        raise ArtifactContractError("no usable reference observations")
    as_of = pd.Timestamp(completed).tz_convert("Asia/Shanghai").date().isoformat()
    facts.sort(key=lambda row: (row["thscode"], row.get("trade_date", ""), row.get("level", "")))
    digest = json_sha256(facts)
    path = root / "facts" / category / f"as_of_date={as_of}" / f"vintage={digest}" / filename
    if not path.exists():
        atomic_write_parquet(path, pd.DataFrame(facts))
    return {"path": path.relative_to(root).as_posix(), "sha256": sha256_file(path), "rows": len(facts), "completed_at": completed}


def _verify(root, ref):
    path = (root / ref["path"]).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or sha256_file(path) != ref["sha256"]:
        raise ArtifactContractError("enrichment checkpoint artifact missing or corrupt")


def _cached_query(root, identity):
    path = root / "facts/enrichment_receipts" / (json_sha256(identity) + ".json")
    if not path.exists():
        return None
    receipt = load_json_object(path, missing_ok=False)
    if receipt.get("schema_version") != 1 or receipt.get("identity") != identity:
        raise ArtifactContractError("incompatible enrichment receipt")
    _verify(root, receipt["artifact"])
    return receipt["artifact"]


def _financial_cache(root, profile_hash, codes, period, ttl_days):
    """Issuer/report-period reuse; a new filing invalidates only that issuer."""
    if not ttl_days:
        return {}, []
    now = pd.Timestamp(utc_now())
    cached = {}
    for path in sorted((root / "facts/enrichment_receipts").glob("*.json")):
        receipt = load_json_object(path, missing_ok=False)
        identity = receipt.get("identity", {})
        if identity.get("profile_sha256") != profile_hash or identity.get("context", {}).get("report_period") != period:
            continue
        if receipt.get("schema_version") != 1 or json_sha256(identity) != path.stem:
            raise ArtifactContractError("financial receipt identity mismatch")
        ref = receipt["artifact"]
        _verify(root, ref)
        batch = normalize_research_batch(load_json_object(root / ref["path"], missing_ok=False))
        for row in batch["records"]:
            if (row["thscode"] in codes and row["report_period"] == period
                    and now - pd.Timedelta(days=ttl_days) <= pd.Timestamp(row["known_at"]) <= now):
                previous = cached.get(row["thscode"])
                if previous is None or row["known_at"] > previous[0]["known_at"]:
                    cached[row["thscode"]] = (row, ref)
    for path in sorted((root / "facts/research/events").glob("*.json")):
        batch = normalize_research_batch(load_json_object(path, missing_ok=False))
        if json_sha256(batch) != path.stem:
            raise ArtifactContractError("event cache hash mismatch")
        for row in batch["records"]:
            if row["thscode"] in cached:
                known = pd.Timestamp(cached[row["thscode"]][0]["known_at"])
                if known < pd.Timestamp(row["published_at"]) <= now:
                    cached.pop(row["thscode"])
    refs = {ref["path"]: ref for _, ref in cached.values()}
    return {code: row for code, (row, _) in cached.items()}, list(refs.values())


def _index_cache(root, profile_hash, codes, end):
    sessions = sorted(path.name.removeprefix("trade_date=") for path in (root / "facts/market").glob("trade_date=*")
                      if path.is_dir() and path.name.removeprefix("trade_date=") <= end)[-21:]
    if not sessions:
        raise ArtifactContractError("index collection requires existing market session dates")
    wanted = {(code, day) for code in codes for day in sessions}
    found, refs = set(), []
    for path in sorted((root / "facts/enrichment_receipts").glob("*.json")):
        receipt = load_json_object(path, missing_ok=False)
        identity = receipt.get("identity", {})
        if identity.get("profile_sha256") != profile_hash:
            continue
        if json_sha256(identity) != path.stem or receipt.get("schema_version") != 1:
            raise ArtifactContractError("benchmark receipt identity mismatch")
        ref = receipt["artifact"]
        _verify(root, ref)
        frame = pd.read_parquet(root / ref["path"])
        found.update(zip(frame["thscode"], frame["trade_date"]))
        refs.append(ref)
    missing_dates = sorted(day for code, day in wanted - found)
    return missing_dates, refs


def run_enrichment(client, root: Path, config: dict, codes: list[str], trade_date: str, as_of_date: str,
                   *, canary=False, modules=None) -> dict:
    if not codes or len(codes) > (3 if canary else 100) or any(not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code) for code in codes):
        raise ArtifactContractError("enrichment scope must be <=3 canary or <=100 explicit A-share codes")
    selected = sorted(MODULES if modules is None else modules)
    if not selected or set(selected) - MODULES:
        raise ArtifactContractError("unknown enrichment module")
    pointer_path = root / "latest/enrichment_manifest.json"
    previous = load_json_object(pointer_path)
    if pointer_path.exists():
        if previous.get("schema_version") != 1 or not isinstance(previous.get("modules"), dict):
            raise ArtifactContractError("incompatible enrichment manifest")
        for status in previous["modules"].values():
            for ref in status.get("artifacts", []):
                _verify(root, ref)
    status = {}
    for module in selected:
        profile = config["profiles"].get(module)
        if profile is None:
            status[module] = {"state": "not_configured", "reason": "documented_indicator_mapping_required"}
            continue
        profile_hash = json_sha256({"adapter": VERSION, "module": module, "profile": profile})
        evidence_path = root / "facts/enrichment_canaries" / f"{profile_hash}.json"
        if not canary:
            gate = load_json_object(evidence_path)
            if gate.get("state") != "validated" or gate.get("profile_sha256") != profile_hash:
                status[module] = {"state": "not_ready", "reason": "matching_live_canary_required"}
                continue
            for ref in gate.get("artifacts", []):
                _verify(root, ref)
        scope = profile["scope_codes"] or sorted(set(codes))
        periods = report_periods(trade_date, config["financial_periods"]) if module == "financial_history" else [""]
        refs = []
        try:
            for period in periods:
                query_codes = scope
                if module == "financial_history":
                    cached, old_refs = _financial_cache(root, profile_hash, scope, period, profile["ttl_days"])
                    refs.extend(old_refs)
                    query_codes = sorted(set(scope) - set(cached))
                    if not query_codes:
                        continue
                context = {"codes": ",".join(query_codes), "trade_date": trade_date, "compact_date": trade_date.replace("-", ""),
                           "as_of_date": as_of_date, "report_period": period, "compact_period": period.replace("-", ""),
                           "start": (pd.Timestamp(trade_date) - pd.Timedelta(days=40 if module == "index_prices" else 7)).date().isoformat(),
                           "end": trade_date, "forecast_year": trade_date[:4]}
                if module == "index_prices":
                    missing_dates, cached_refs = _index_cache(root, profile_hash, query_codes, trade_date)
                    refs.extend(cached_refs)
                    if not missing_dates:
                        continue
                    context["start"], context["end"] = missing_dates[0], missing_dates[-1]
                identity = {"profile_sha256": profile_hash, "context": context}
                existing = _cached_query(root, identity)
                if existing:
                    refs.append(existing)
                    continue
                started = utc_now()
                response = client.request(profile["endpoint"], _templates(profile["request"], context))
                completed = utc_now()
                frame = _tables_frame(response)
                rows = _decode(frame, profile["fields"])
                for row in rows:
                    for field in ("trade_date", "event_date", "report_period"):
                        if row.get(field) is not None:
                            row[field] = pd.Timestamp(_timestamp(row[field], date_only_end=False)).tz_convert("Asia/Shanghai").date().isoformat()
                for row in rows:
                    if row.get("thscode") not in query_codes:
                        raise ArtifactContractError("provider returned securities outside requested scope")
                if module in FRAME_FIELDS and len({row["thscode"] for row in rows}) < 1:
                    raise ArtifactContractError("empty reference response is not ready")
                if module in {"financial_history", "industry", "tradability"} and len({row["thscode"] for row in rows}) != len(rows):
                    raise ArtifactContractError("ambiguous per-security reference rows")
                if module == "index_prices":
                    wanted = {(code, day) for code in query_codes for day in missing_dates}
                    observed = {(row["thscode"], row["trade_date"]) for row in rows if row.get("close") is not None}
                    if not wanted <= observed:
                        raise ArtifactContractError("benchmark coverage does not match missing session dates")
                    rows = [row for row in rows if (row["thscode"], row["trade_date"]) in wanted]
                if module in {"financial_history", "structured_events", "forecasts"}:
                    batch = _research_batch(module, rows, query_codes, context, started, completed, profile["source_documentation"])
                    ref = _save_batch(root, batch)
                else:
                    ref = _save_frame(root, module, rows, trade_date, completed)
                ref["field_non_null_counts"] = {field: sum(row.get(field) is not None for row in rows)
                                                for field in sorted(profile["fields"])}
                atomic_write_json(root / "facts/enrichment_receipts" / (json_sha256(identity) + ".json"),
                                  {"schema_version": 1, "identity": identity, "artifact": ref})
                refs.append(ref)
            refs = sorted({ref["path"]: ref for ref in refs}.values(), key=lambda ref: ref["path"])
            unmapped_observations = [field for field, spec in profile["fields"].items()
                                    if "source" in spec and field not in {"thscode", "published_at"}
                                    and not any(ref.get("field_non_null_counts", {}).get(field, 0) for ref in refs)]
            if canary and unmapped_observations:
                raise ArtifactContractError("canary field coverage incomplete; mapping remains disabled")
            status[module] = {"state": "validated", "profile_sha256": profile_hash, "artifacts": refs,
                              "scope_codes": scope, "collection_completed_at": max(ref["completed_at"] for ref in refs)}
            if canary:
                atomic_write_json(evidence_path, {"schema_version": 1, **status[module]})
        except Exception as exc:
            status[module] = {**safe_failure(exc), "artifacts": refs}
            if status[module].get("api_error_code") in {-1300, -1301, -1302, -1303, -1305, -4301, -4302, -4303, -4317, -4318}:
                for remaining in selected:
                    if remaining not in status:
                        status[remaining] = {"state": "not_ready", "reason": "authentication_or_quota_stop"}
                break
    completed = max((ref["completed_at"] for item in status.values() for ref in item.get("artifacts", [])), default=None)
    successful = any(item["state"] == "validated" for item in status.values())
    failed = any(item["state"] == "failed" for item in status.values())
    result = {"schema_version": 1, "adapter_version": VERSION, "market_trade_date": trade_date,
              "as_of_date": as_of_date, "generated_at": completed, "config_sha256": json_sha256(config),
              "ok": successful and not failed, "state": "ready" if all(item["state"] == "validated" for item in status.values()) else "partial",
              "scope": "canary" if canary else "radar", "modules": status, "requests": client.audit,
              "data_mode": "facts_and_deterministic_features_only", "raw_payload_persisted": False}
    atomic_write_json(root / "enrichment_last_attempt.json", result)
    # Unconfigured/unknown are visible; a failing configured query never replaces last valid latest.
    if result["ok"] and previous.get("as_of_date", "") <= as_of_date:
        atomic_write_json(pointer_path, {key: value for key, value in result.items() if key != "requests"})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--config", type=Path, default=Path("config/ifind_enrichment.json"))
    parser.add_argument("--canary", action="store_true")
    parser.add_argument("--modules", default=",".join(sorted(MODULES)))
    parser.add_argument("--max-requests", type=int, default=24)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    market = load_manifest(args.data_dir / "latest/manifest.json", missing_ok=False)
    radar = load_json_object(args.data_dir / "latest/opportunity_radar_latest.json", missing_ok=False)
    if radar.get("source_snapshot_sha256") != market.get("source_snapshot_sha256") or radar.get("trade_date") != market["trade_date"]:
        raise ArtifactContractError("radar and source market manifest do not match")
    codes = CANARY_CODES if args.canary else [row["thscode"] for row in radar["candidate_union"]]
    result = run_enrichment(BoundedClient(max_requests=args.max_requests), args.data_dir, config, codes,
                            market["trade_date"], datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
                            canary=args.canary, modules=args.modules.split(","))
    print(json.dumps({key: value for key, value in result.items() if key != "modules"}, ensure_ascii=False, indent=2))
    print(json.dumps({"modules": {key: value["state"] for key, value in result["modules"].items()}}))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
