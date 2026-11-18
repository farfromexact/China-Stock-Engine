"""As-of DuckDB views for market breadth, candidates and watchlist risks."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
import re

import pyarrow as pa
import pyarrow.parquet as pq

from .schema import SHANGHAI, code_value, completed_bound, day_value, days_between, timestamp, utcnow
from .warehouse import eligible, json_digest

RISK_NAME = re.compile(r"^(?:[＊*]?ST|S[＊*]ST|退市)|退$", re.IGNORECASE)


def dict_rows(connection, sql, parameters=None):
    result = connection.execute(sql, parameters or [])
    names = [column[0] for column in result.description]
    return [dict(zip(names, values)) for values in result.fetchall()]


def versions_at(state, key, cutoff):
    return {day: entry for day, items in state[key].items()
            if (entry := eligible(items, cutoff)) is not None}


def snapshot(warehouse, as_of=None, day=None):
    cutoff = timestamp(as_of or utcnow())
    today = datetime.fromisoformat(cutoff).astimezone(SHANGHAI).date().isoformat()
    state = warehouse.read()
    market = {d: row for d, row in versions_at(state, "market", cutoff).items() if d <= today}
    calendar = {d: row for d, row in versions_at(state, "calendar", cutoff).items() if d <= today}
    if day:
        day = day_value(day)
        if day not in market:
            raise ValueError(f"{day} 在指定观察时间没有已验证行情")
    else:
        active = [d for d in market if calendar.get(d, {}).get("open")]
        day = max(active, default=None)
    if day and not calendar.get(day, {}).get("open"):
        raise ValueError(f"{day} 在指定观察时间的日历不是交易日")
    return cutoff, day, state, market, calendar


def status(warehouse, as_of=None):
    return snapshot_status(*snapshot(warehouse, as_of))


def snapshot_status(cutoff, latest, state, market, calendar):
    bound = completed_bound(datetime.fromisoformat(cutoff))
    certified_through = max((day for day in calendar if day <= bound), default=None)
    start = min((day for day in market if day <= bound), default=bound)
    interior = list(days_between(start, bound))
    unknown_days = [day for day in interior if day not in calendar]
    missing = [day for day in interior if calendar.get(day, {}).get("open") and day not in market]
    announcement_batches = {
        source: entry["known_at"] for source, entries in state["events"].items()
        if (entry := eligible(entries, cutoff))
    }
    if not latest:
        freshness = "empty"
    elif unknown_days:
        freshness = "calendar_unverified"
    elif missing:
        freshness = "missing_sessions"
    else:
        freshness = "ready"
    return {"as_of": cutoff, "latest": latest, "target_bound_shanghai": bound,
            "calendar_through": certified_through, "freshness": freshness, "missing_sessions": missing,
            "uncertified_days": unknown_days, "market_sessions": len(market),
            "latest_coverage": market[latest]["coverage"] if latest else None,
            "event_observations": announcement_batches}


def read_watchlist(path):
    if path is None:
        return {}
    records = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"code", "kind"} <= set(reader.fieldnames or ()):
            raise ValueError("自选 CSV 必须含 code,kind")
        for row in reader:
            code, kind = code_value(row["code"]), row["kind"].strip()
            if kind not in {"holding", "watch"} or code in records:
                raise ValueError("自选 CSV 有重复代码或无效类别")
            records[code] = {"kind": kind, "label": (row.get("label") or "").strip()}
    return records


METRICS_SQL = """
WITH ticks AS (
  SELECT b.*, f.seq
  FROM daily_bars b JOIN day_files f ON replace(b.filename, chr(92), '/') = f.file
), stats AS (
  SELECT *,
    CASE WHEN lag(seq, 20) OVER (PARTITION BY code ORDER BY seq) = seq - 20
             AND count(amount) FILTER (WHERE state = 'traded') OVER prev = 20
         THEN avg(amount) OVER prev END AS prior_avg_amount,
    CASE WHEN lag(seq, 19) OVER (PARTITION BY code ORDER BY seq) = seq - 19
             AND count(return_1d) OVER win_return = 20
         THEN exp(sum(ln(1 + return_1d)) OVER win_return) - 1 END AS return_20d
  FROM ticks
  WINDOW prev AS (PARTITION BY code ORDER BY seq ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING),
         win_return AS (PARTITION BY code ORDER BY seq ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
)
SELECT code, name, state, open, high, low, close, amount, return_1d, prior_avg_amount,
       return_20d, amount / nullif(prior_avg_amount, 0) AS turnover_multiple, industry
FROM stats LEFT JOIN industry_today USING (code)
WHERE seq = ?
ORDER BY code
"""


def industry_snapshot(state, day, cutoff, scheme):
    versions = [row for row in state["industry"].get(scheme, [])
                if row["as_of"] <= day and row["known_at"] <= cutoff]
    return max(versions, key=lambda row: (row["as_of"], row["known_at"]), default=None)


def scan(warehouse, market, calendar, day, industry):
    sessions = sorted(d for d, data in calendar.items() if data["open"] and d <= day)[-21:]
    complete = len(sessions) == 21 and all(d in calendar for d in days_between(sessions[0], day))
    paths = [warehouse.path(market[d]).as_posix() for d in sessions if d in market]
    positions = [i for i, d in enumerate(sessions) if d in market]
    with warehouse.sql() as con:
        con.register("day_files", pa.table({"file": paths, "seq": positions}))
        con.read_parquet(paths, filename=True).create_view("daily_bars")
        if industry:
            con.read_parquet(str(warehouse.path(industry))).create_view("industry_today")
        else:
            con.execute("CREATE TEMP TABLE industry_today(code VARCHAR, industry VARCHAR)")
        rows = dict_rows(con, METRICS_SQL, [len(sessions) - 1])
    if not complete:
        for row in rows:
            row["prior_avg_amount"] = row["return_20d"] = row["turnover_multiple"] = None
    return rows, sessions


def market_summary(items):
    priced = [row["return_1d"] for row in items if row["state"] == "traded" and row["return_1d"] is not None]
    trailing = [row["return_20d"] for row in items
                if row["state"] == "traded" and row["return_20d"] is not None]
    traded = sum(row["state"] == "traded" for row in items)
    advancing = sum(value > 0 for value in priced)
    return {"members": len(items), "traded": traded, "with_returns": len(priced), "advancing": advancing,
            "breadth": advancing / len(priced) if priced else None,
            "amount": sum(row["amount"] or 0 for row in items if row["state"] == "traded"),
            "return_1d": sum(priced) / len(priced) if priced else None, "with_20d": len(trailing),
            "return_20d": sum(trailing) / len(trailing) if trailing else None}


def opportunities(rows, universe, totals, sectors, trade_states, limit):
    found = []
    if universe == "daily":
        for row in rows:
            tradability = trade_states.get(row["code"], {})
            ceiling = tradability.get("limit_up")
            if (tradability.get("suspended") or tradability.get("risk_warning")
                    or (ceiling is not None and row["close"] is not None and row["close"] >= ceiling - 1e-6)
                    or row["state"] != "traded" or RISK_NAME.search(row["name"])
                    or not (row["return_1d"] or 0) > 0 or not (row["return_20d"] or 0) > 0
                    or not (row["prior_avg_amount"] or 0) >= 50_000_000
                    or not (row["turnover_multiple"] or 0) >= 1.5 or row["high"] == row["low"]):
                continue
            peers = sectors.get(row["industry"], {})
            use_industry = peers.get("with_20d", 0) >= 5
            base = peers if use_industry else totals
            relative = row["return_20d"] - base["return_20d"]
            if relative <= 0:
                continue
            expanding = (use_industry and peers["breadth"] is not None and peers["breadth"] >= 0.55
                         and peers["breadth"] > (totals["breadth"] or 0))
            found.append({**row, "relative_20d": relative, "comparison": "industry" if use_industry else "market",
                          "industry_expansion": expanding, "execution": "requires_preopen_check"})
    found.sort(key=lambda row: (-row["industry_expansion"], -row["relative_20d"],
                                -row["turnover_multiple"], row["code"]))
    return len(found), found[:limit]


def recent_events(warehouse, state, cutoff, codes):
    if not codes:
        return [], []
    batches = [entry for records in state["events"].values() for entry in records if entry["known_at"] <= cutoff]
    if not batches:
        return [], []
    paths = [warehouse.path(entry).as_posix() for entry in batches]
    with warehouse.sql() as con:
        con.register("event_files", pa.table({
            "file": paths, "known_at": [entry["known_at"] for entry in batches],
            "source": [entry["source"] for entry in batches],
        }))
        con.read_parquet(paths, filename=True).create_view("events_all")
        records = dict_rows(con, """
            SELECT event_id, code, category, title, url, published_at, effective_date, known_at, source
            FROM events_all e JOIN event_files f ON replace(e.filename, chr(92), '/') = f.file
            WHERE code IN (SELECT * FROM unnest(?))
            QUALIFY row_number() OVER (PARTITION BY source, code, event_id ORDER BY known_at DESC) = 1
            ORDER BY known_at DESC, code, event_id
        """, [sorted(codes)])
    today = datetime.fromisoformat(cutoff).astimezone(SHANGHAI).date()
    since, horizon = (today - timedelta(days=7)).isoformat(), (today + timedelta(days=10)).isoformat()
    relevant = []
    for row in records:
        if row["published_at"] and row["published_at"] > cutoff:
            continue
        seen = datetime.fromisoformat(row["known_at"]).astimezone(SHANGHAI).date().isoformat()
        disclosed = (datetime.fromisoformat(row["published_at"]).astimezone(SHANGHAI).date().isoformat()
                     if row["published_at"] else "")
        if max(seen, disclosed) >= since or today.isoformat() <= (row["effective_date"] or "") <= horizon:
            relevant.append(row)
    return relevant, [[entry["source"], entry["sha256"]] for entry in batches]


def trade_state_snapshot(warehouse, state, day, cutoff):
    entry = eligible(state["tradability"].get(day, []), cutoff)
    records = pq.ParquetFile(warehouse.path(entry)).read().to_pylist() if entry else []
    return entry, {row["code"]: row for row in records}


def make_report(warehouse, *, day=None, as_of=None, watchlist=None, scheme="SW1", limit=15, risk_date=None):
    if not 1 <= limit <= 100:
        raise ValueError("候选展示数须为 1–100")
    cutoff, day, state, market, calendar = snapshot(warehouse, as_of, day)
    if not day:
        raise ValueError("指定观察时间还没有已验证行情")
    watch = read_watchlist(watchlist)
    industry = industry_snapshot(state, day, cutoff, scheme)
    industry_age = ((date.fromisoformat(day) - date.fromisoformat(industry["as_of"])).days
                    if industry else None)
    use_industry = industry if industry_age is not None and industry_age <= 45 else None
    rows, sessions = scan(warehouse, market, calendar, day, use_industry)
    totals = market_summary(rows)
    grouped = {}
    for row in rows:
        if row["industry"]:
            grouped.setdefault(row["industry"], []).append(row)
    sectors = {key: market_summary(items) for key, items in grouped.items()}
    sector_list = sorted(({"industry": name, **summary} for name, summary in sectors.items()),
                         key=lambda summary: (-(summary["return_1d"] or 0), summary["industry"]))
    signal_meta, signal_status = trade_state_snapshot(warehouse, state, day, cutoff)
    risk_day = day_value(risk_date) if risk_date else day
    if risk_day == day:
        risk_meta, risk_status = signal_meta, signal_status
    else:
        risk_meta, risk_status = trade_state_snapshot(warehouse, state, risk_day, cutoff)
    count, selection = opportunities(rows, market[day]["universe"], totals, sectors, signal_status, limit)
    tracked = set(watch) | {row["code"] for row in selection}
    events, event_inputs = recent_events(warehouse, state, cutoff, tracked)
    quotes = {row["code"]: row for row in rows if row["code"] in watch}
    risks = [{"code": code, **watch[code], "name": quotes.get(code, {}).get("name"),
              "quote_state": quotes.get(code, {}).get("state", "unavailable"),
              "risk_name": bool(RISK_NAME.search(quotes[code]["name"])) if code in quotes else None,
              "reported_status": risk_status.get(code),
              "event_count": sum(row["code"] == code for row in events)}
             for code in sorted(watch, key=lambda c: (watch[c]["kind"] != "holding", c))]
    inputs = {"calendar_sessions": sessions, "market": {d: market[d]["sha256"] for d in sessions if d in market},
              "industry": use_industry["sha256"] if use_industry else None,
              "event_partitions": event_inputs,
              "signal_tradability": signal_meta["sha256"] if signal_meta else None,
              "risk_tradability": risk_meta["sha256"] if risk_meta else None}
    return {
        "as_of": cutoff, "trade_date": day, "warehouse": snapshot_status(
            cutoff, max((d for d in market if calendar.get(d, {}).get("open")), default=None),
            state, market, calendar),
        "market_verified_at": market[day]["known_at"], "market_coverage": market[day]["coverage"],
        "universe": market[day]["universe"],
        "source": market[day]["source"], "market": totals, "sectors": sector_list,
        "industry": {"scheme": scheme, "as_of": industry["as_of"] if industry else None,
                     "source": industry["source"] if industry else None, "age_days": industry_age,
                     "used": use_industry is not None, "mapped": sum(len(items) for items in grouped.values())},
        "candidate_count": count, "candidates": selection, "watchlist_configured": watchlist is not None,
        "watchlist_risks": risks, "risk_date": risk_day,
        "tradability_source": risk_meta["source"] if risk_meta else None,
        "events": events, "decision_rule": "daily-strength-volume-v1",
        "inputs": inputs, "report_id": json_digest({"day": day, "cutoff": cutoff, "inputs": inputs,
                                                   "watchlist": watch, "scheme": scheme, "limit": limit,
                                                   "risk_date": risk_day}),
    }


def get_history(warehouse, code, *, as_of=None, days=300):
    code = code_value(code)
    if not 1 <= days <= 2500:
        raise ValueError("历史会话长度须为 1–2500")
    cutoff, end, _state, market, calendar = snapshot(warehouse, as_of)
    sessions = sorted(d for d, info in calendar.items() if info["open"] and end and d <= end)[-days:]
    available = [d for d in sessions if d in market]
    if not available:
        return {"code": code, "as_of": cutoff, "rows": [], "unavailable": sessions}
    paths = [warehouse.path(market[day]).as_posix() for day in available]
    with warehouse.sql() as con:
        con.register("day_files", pa.table({
            "file": paths, "trade_date": available, "known_at": [market[day]["known_at"] for day in available],
        }))
        con.read_parquet(paths, filename=True).create_view("history_data")
        rows = dict_rows(con, """
            SELECT trade_date, known_at, code, name, state, open, high, low, close, pre_close,
                   volume, amount, return_1d
            FROM history_data h JOIN day_files f ON replace(h.filename, chr(92), '/') = f.file
            WHERE code = ?
            ORDER BY trade_date
        """, [code])
    observed_days = {row["trade_date"] for row in rows}
    return {"code": code, "as_of": cutoff, "rows": rows,
            "unavailable": [day for day in sessions if day not in observed_days]}
