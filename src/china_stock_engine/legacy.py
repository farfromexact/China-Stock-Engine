"""Optional one-time seed from an existing, verified V1 Wind archive."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pyarrow.parquet as pq

from .schema import VALUES, day_value, timestamp
from .warehouse import digest


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def checked_table(path, sha):
    if not sha or digest(path) != sha:
        raise ValueError(f"旧档案校验失败: {path.name}")
    return pq.ParquetFile(path).read().to_pylist()


def snapshot_table(snapshot, manifest, name):
    return checked_table(snapshot / name, manifest["artifacts"][name]["sha256"])


def old_market(vintage, day, universe, reference):
    location = vintage / "market" / f"trade_date={day}"
    receipt = read_json(location / "market_receipt.json")
    if (receipt.get("verified") is not True or receipt["trade_date"] != day
            or receipt["provider"] != "windpy"):
        raise ValueError(f"旧行情缺少日期一致的 Wind 校验收据: {day}")
    quotes = snapshot_table(location, receipt, "daily_quotes.parquet")
    statuses = snapshot_table(location, receipt, "daily_security_status.parquet")
    keys = {row["thscode"] for row in statuses}
    if (len(keys) != len(statuses) or keys != set(universe)
            or any(row["trade_date"] != day for row in quotes + statuses)):
        raise ValueError(f"旧行情股票池或日期不一致: {day}")
    by_code = {row["thscode"]: row for row in quotes}
    if len(by_code) != len(quotes) or not set(by_code) <= keys:
        raise ValueError(f"旧行情代码重复或超出股票池: {day}")
    rows = []
    for code in sorted(keys):
        quote = by_code.get(code, {})
        if reference[code] > day and not quote:
            continue  # Preserve known listing boundaries within the seed universe.
        if reference[code] > day:
            raise ValueError(f"发现上市前行情: {day} {code}")
        values = {key: quote.get(key) for key in VALUES if key != "return_1d"}
        change = quote.get("change_ratio")
        values["return_1d"] = None if change is None else change / 100
        rows.append({"code": code, "name": universe[code], **values})
    return rows, receipt["collection_completed_at"]


def seed_market(warehouse, data_dir: Path):
    data_dir = data_dir.resolve()
    if warehouse.root == data_dir or warehouse.root.is_relative_to(data_dir):
        raise ValueError("新仓必须与旧 data 目录分离")
    publication = read_json(data_dir / "wind_publication_status.json")
    latest = read_json(data_dir / "latest" / "manifest.json")
    end, start = day_value(publication["trade_date"]), day_value(publication["start"])
    vintage_name = publication["vintage"]
    if (not re.fullmatch(r"[a-f0-9]{24}", vintage_name) or publication.get("state") != "published"
            or latest.get("verified") is not True or latest.get("provider") != "windpy"
            or latest.get("wind_vintage") != vintage_name or latest.get("trade_date") != end):
        raise ValueError("旧仓 latest 与 Wind 发布收据无法相互核对")
    vintage = data_dir / "facts" / "wind" / f"vintage={vintage_name}"
    snapshot = vintage / "snapshots" / f"trade_date={end}"
    manifest = read_json(snapshot / "manifest.json")
    if (manifest["trade_date"] != end or manifest.get("verified") is not True
            or manifest.get("wind_vintage") != vintage_name or manifest.get("provider") != "windpy"):
        raise ValueError("旧 Wind 快照身份不符")
    calendar = snapshot_table(snapshot, manifest, "trading_calendar.parquet")
    certificate = {(row["calendar_window_start"], row["calendar_window_end"], row["source_provider"],
                    row["calendar_window_complete"], row["calendar"]) for row in calendar}
    if len(certificate) != 1:
        raise ValueError("旧 Wind 日历未能证明窗口完整")
    low, through, provider, complete, market = next(iter(certificate))
    if complete is not True or provider != "windpy" or market != "SSE" or not low <= start <= end <= through:
        raise ValueError("旧 Wind 日历不覆盖归档日期")
    open_days = sorted(row["trade_date"] for row in calendar if row["is_open"] and start <= row["trade_date"] <= end)
    if len(open_days) != publication["sessions"] or not open_days or open_days[-1] != end:
        raise ValueError("旧 Wind 日历与发布交易日数不一致")
    universe_rows = snapshot_table(snapshot, manifest, "universe.parquet")
    reference_rows = snapshot_table(snapshot, manifest, "security_reference.parquet")
    universe = {row["thscode"]: row["security_name"] for row in universe_rows}
    reference = {row["thscode"]: day_value(row["listing_date"]) for row in reference_rows}
    if len(universe) != len(universe_rows) or len(reference) != len(reference_rows) or set(universe) != set(reference):
        raise ValueError("旧快照股票池和上市日清单不一致")
    observed = timestamp(publication["completed_at"])
    source = f"Wind V1 {vintage_name}"
    new = 0
    with warehouse.edit() as state:
        warehouse.add_calendar(state, start, end, open_days, observed, source=source)
        for day in open_days:
            rows, raw_observed = old_market(vintage, day, universe, reference)
            if timestamp(raw_observed) > observed:
                raise ValueError("旧行情收据晚于发布记录")
            # Older entries reuse the final archive membership. Their prices are
            # suitable for windows, but they cannot certify historical membership.
            quality = "daily" if day == end else "legacy_window"
            if any(version["source"] == source for version in state["market"].get(day, [])):
                continue
            if day in state["market"]:
                raise ValueError(f"{day} 已有市场数据，导入不会覆盖")
            new += warehouse.add_market(state, day, rows, observed, source=source, universe=quality,
                                        min_coverage=0.98 if day == end else 0)
    return {"sessions": len(open_days), "new_sessions": new, "start": start, "end": end,
            "archive_known_at": observed, "source": source}


def seed_industry(warehouse, snapshot: Path):
    info = read_json(snapshot / "manifest.json")
    if info.get("dataset") != "current_a_share_industry_tags" or info.get("classification_system") != "SW":
        raise ValueError("这个快照不是已署名的申万行业资料")
    source = f"历史 iFinD 申万行业快照 {day_value(info['snapshot_date'])}"
    rows = checked_table(snapshot / "a_share_industry.parquet", info["parquet_sha256"])
    if len(rows) != info["row_count"]:
        raise ValueError("申万行业快照行数不符")
    named = [{"code": row["thscode"], "industry": row["sw1_name"]} for row in rows if row.get("sw1_name")]
    return warehouse.add_industry(named, info["snapshot_date"], source, "SW1", info["generated_at"])
