"""A single completed-session collector using WindPy's verified WSD layouts."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .schema import code_value, completed_bound, day_value, days_between, market_row, number, utcnow
from .wind_session import WindError, WindSession

SECTOR = "a001010100000000"
OPTIONS = "PriceAdj=U;Days=Trading;Fill=Blank;unit=1"
FIELDS = {"open": "open", "high": "high", "low": "low", "close": "close", "pre_close": "pre_close",
          "volume": "volume", "amount": "amt", "return_1d": "pct_chg"}


def wind_day(value) -> str:
    try:
        return day_value(datetime.fromisoformat(value) if isinstance(value, str) else value)
    except (ValueError, TypeError) as exc:
        raise WindError("Wind 返回了无效的日期轴") from exc


def field_columns(result):
    names = [str(name).lower() for name in result["fields"]]
    data = result["data"]
    if len(names) != len(set(names)) or len(names) != len(data) or len({len(col) for col in data}) > 1:
        raise WindError("Wind 返回了不匹配的字段轴")
    return dict(zip(names, data))


def cross_section(result, codes, field, day):
    returned = [code_value(code) for code in result["codes"]]
    if (len(returned) != len(codes) or set(returned) != set(codes)
            or [str(name).lower() for name in result["fields"]] != [field]
            or [wind_day(value) for value in result["times"]] != [day]):
        raise WindError(f"Wind WSD {field} 的代码、字段或日期轴与请求不一致")
    data = result["data"]
    if len(data) == 1 and len(data[0]) == len(codes):
        values = data[0]  # 单日多代码 Wind SDK 返回 1 × N。
    elif len(data) == len(codes) and all(len(row) == 1 for row in data):
        values = [row[0] for row in data]
    else:
        raise WindError(f"Wind WSD {field} 的数据形状与请求不一致")
    return dict(zip(returned, values))


class WindMarket:
    def __init__(self, request, batch_size=100, progress=None):
        if not 1 <= batch_size <= 100:
            raise ValueError("Wind 批量请求大小必须为 1–100")
        self.request, self.batch_size, self.progress = request, batch_size, progress

    def calendar(self, start, end):
        result = self.request("tdays", start, end, "TradingCalendar=SSE")
        if len(result["data"]) != 1:
            raise WindError("Wind SSE 日历没有返回预期的时间轴")
        days = [wind_day(value) for value in result["data"][0]]
        if not days or days != sorted(set(days)) or not all(start <= day <= end for day in days):
            raise WindError("Wind SSE 日历为空或超出请求范围")
        return days

    def universe(self, day):
        columns = field_columns(self.request("wset", "sectorconstituent", f"date={day};sectorid={SECTOR}"))
        if not {"wind_code", "sec_name"} <= set(columns):
            raise WindError("Wind 沪深北股票池没有返回代码和名称")
        codes = [code_value(code) for code in columns["wind_code"]]
        if len(codes) != len(set(codes)):
            raise WindError("Wind 股票池代码重复")
        for key in ("date", "trade_date"):
            if key in columns and any(wind_day(value) != day for value in columns[key]):
                raise WindError("Wind 股票池返回日期与请求不一致")
        return dict(zip(codes, columns["sec_name"]))

    def quotes(self, day, universe):
        codes = sorted(universe)
        for start in range(0, len(codes), self.batch_size):
            batch = codes[start:start + self.batch_size]
            rows = {code: {"code": code, "name": universe[code]} for code in batch}
            for target, field in FIELDS.items():
                result = self.request("wsd", ",".join(batch), field, day, day, OPTIONS)
                values = cross_section(result, batch, field, day)
                for code, value in values.items():
                    numeric = number(value, field)
                    rows[code][target] = numeric / 100 if target == "return_1d" and numeric is not None else numeric
            for row in rows.values():
                yield market_row(row)
            if self.progress:
                self.progress(day, min(start + self.batch_size, len(codes)), len(codes))


def missing_sessions(state, start, end):
    calendar = state["calendar"]
    if any(day not in calendar for day in days_between(start, end)):
        return None
    return [day for day in days_between(start, end) if calendar[day][-1]["open"] and day not in state["market"]]


def collection_range(state, bound: str, start: str | None):
    if start:
        start = day_value(start)
        if start > bound:
            raise ValueError("起始日晚于可采集截止日")
        return start
    if not state["market"]:
        return (date.fromisoformat(bound) - timedelta(days=30)).isoformat()
    # Revisit a real gap from the existing baseline without re-fetching completed partitions.
    last = min(max(state["market"]), bound)
    first = min(min(state["market"]), last)
    gaps = [day for day in days_between(first, last)
            if day not in state["calendar"] or (state["calendar"][day][-1]["open"] and day not in state["market"])]
    return gaps[0] if gaps else last


def collect(warehouse, *, start=None, end=None, refresh=False, exclusive=False, batch_size=100,
            max_sessions=5, ready_hour=18, interval=0.5, session_factory=WindSession, progress=None):
    end_limit = completed_bound(ready_hour=ready_hour)
    bound = day_value(end) if end else end_limit
    if bound > end_limit:
        raise ValueError(f"请求超出已完成日期上限 {end_limit}")
    if refresh and (not start or not end or start != end):
        raise ValueError("修订重取需要 --date 指定单一交易日")
    if max_sessions < 1:
        raise ValueError("最多采集交易日数必须为正")
    state = warehouse.read()
    first = collection_range(state, bound, start)
    if not refresh and missing_sessions(state, first, bound) == []:
        return {"state": "up_to_date", "through": bound, "published": [], "unchanged": [], "remaining": []}
    query_start = min(first, (date.fromisoformat(bound) - timedelta(days=14)).isoformat())
    published, unchanged = [], []
    with session_factory(exclusive_window=exclusive, request_interval=interval) as session:
        provider = WindMarket(session.request, batch_size=batch_size, progress=progress)
        open_days = provider.calendar(query_start, bound)
        with warehouse.edit() as current:
            warehouse.add_calendar(current, query_start, bound, open_days, utcnow())
        state = warehouse.read()
        work = ([bound] if bound in open_days else []) if refresh else missing_sessions(state, first, bound)
        if work is None:
            raise WindError("Wind 日历未覆盖计划区间")
        if not state["market"] and not start and work:
            work = work[-1:]
        for day in work[:max_sessions]:
            universe = provider.universe(day)
            if len(universe) < 5000 or {code[-2:] for code in universe} != {"SH", "SZ", "BJ"}:
                raise WindError(f"{day} 的 Wind 股票池不满足全市场门槛（{len(universe)} 只）")
            rows = list(provider.quotes(day, universe))
            with warehouse.edit() as current:
                if not refresh and day in current["market"]:
                    unchanged.append(day)
                    continue
                written = warehouse.add_market(current, day, rows, utcnow(), source="WindPy", universe="daily")
                (published if written else unchanged).append(day)
    return {"state": "complete" if work else "up_to_date", "through": bound, "published": published,
            "unchanged": unchanged, "remaining": work[max_sessions:]}


def canary(*, exclusive, batch_size=3, session_factory=WindSession):
    bound = completed_bound()
    start = (date.fromisoformat(bound) - timedelta(days=14)).isoformat()
    codes = ("600000.SH", "000001.SZ", "920001.BJ")
    with session_factory(exclusive_window=exclusive, request_interval=0.5) as session:
        provider = WindMarket(session.request, batch_size=batch_size)
        day = provider.calendar(start, bound)[-1]
        universe = provider.universe(day)
        subset = {code: universe[code] for code in codes if code in universe}
        if len(subset) != len(codes):
            raise WindError("沪深北样本代码未全部出现在当天股票池")
        rows = list(provider.quotes(day, subset))
        singles = WindMarket(session.request, batch_size=1)
        individual = list(singles.quotes(day, subset))
        if rows != individual or any(row["state"] != "traded" for row in rows):
            raise WindError("样本未通过逐只与批量一致性或成交观测校验")
        return {"state": "canary_passed", "date": day, "codes": sorted(subset)}
