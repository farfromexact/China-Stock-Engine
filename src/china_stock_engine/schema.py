"""Canonical fact schemas and validation shared by all input adapters."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pyarrow as pa

SHANGHAI = ZoneInfo("Asia/Shanghai")
CODE = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)")
PRICES = ("open", "high", "low", "close")
VALUES = (*PRICES, "pre_close", "volume", "amount", "return_1d")
STATES = ("traded", "no_turnover", "unobserved")
MARKET = pa.schema([
    ("code", pa.string()), ("name", pa.string()), ("state", pa.string()),
    *((key, pa.float64()) for key in VALUES),
])
INDUSTRY = pa.schema([("code", pa.string()), ("industry", pa.string())])
TRADABILITY = pa.schema([
    ("code", pa.string()), ("suspended", pa.bool_()), ("risk_warning", pa.bool_()),
    ("limit_applies", pa.bool_()), ("limit_up", pa.float64()), ("limit_down", pa.float64()),
])
EVENT = pa.schema([(key, pa.string()) for key in
                   ("event_id", "code", "category", "title", "url", "published_at", "effective_date")])
EVENT_TYPES = {
    "earnings", "earnings_revision", "dividend", "share_reduction", "unlock", "suspension",
    "resumption", "risk_warning", "regulatory", "delisting", "other",
}


def day_value(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期必须为 YYYY-MM-DD")
    return date.fromisoformat(value).isoformat()


def timestamp(value: str | datetime) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def utcnow() -> str:
    return timestamp(datetime.now(timezone.utc))


def completed_bound(now: datetime | None = None, ready_hour: int = 18) -> str:
    if now is None:
        now = datetime.now(SHANGHAI)
    if now.utcoffset() is None or not 15 <= ready_hour <= 23:
        raise ValueError("收盘截止必须带时区，准备时间为 15–23 点")
    local = now.astimezone(SHANGHAI)
    return (local.date() - timedelta(days=int(local.hour < ready_hour))).isoformat()


def days_between(start: str, end: str):
    current, stop = date.fromisoformat(day_value(start)), date.fromisoformat(day_value(end))
    if current > stop:
        raise ValueError("起始日晚于结束日")
    while current <= stop:
        yield current.isoformat()
        current += timedelta(days=1)


def code_value(value) -> str:
    if not isinstance(value, str) or not CODE.fullmatch(value.strip().upper()):
        raise ValueError("证券代码必须形如 600000.SH、000001.SZ 或 920001.BJ")
    return value.strip().upper()


def required_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 不能为空")
    return value.strip()


def number(value, key):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{key} 不是行情数值")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 不是行情数值") from exc
    if math.isnan(result):
        return None
    if not math.isfinite(result):
        raise ValueError(f"{key} 出现无穷值")
    return 0.0 if result == 0 else result


def market_row(row: dict) -> dict:
    code = code_value(row["code"])
    name = required_text(row["name"], "证券名称")
    values = {key: number(row.get(key), key) for key in VALUES}
    raw = [values[key] for key in PRICES]
    volume, amount = values["volume"], values["amount"]
    if values["pre_close"] is not None and values["pre_close"] <= 0:
        raise ValueError(f"{code} 昨收无效")
    if any(x is not None and x < 0 for x in (volume, amount)):
        raise ValueError(f"{code} 成交量或成交额为负")
    if values["return_1d"] is not None and values["return_1d"] <= -1:
        raise ValueError(f"{code} 日收益口径异常")
    if all(x is None for x in raw):
        if any(x is not None and x > 0 for x in (volume, amount)):
            raise ValueError(f"{code} 有成交却无 OHLC")
        state = "unobserved"
    else:
        if any(x is None or x <= 0 for x in raw):
            raise ValueError(f"{code} OHLC 缺字段或非正")
        o, h, low, c = raw
        if not (low <= min(o, c) <= max(o, c) <= h):
            raise ValueError(f"{code} OHLC 顺序无效")
        if volume is None or amount is None or (volume > 0) != (amount > 0):
            raise ValueError(f"{code} 量额缺失或矛盾")
        state = "traded" if volume > 0 else "no_turnover"
        if state == "traded" and not (low / 3 <= amount / volume <= h * 3):
            raise ValueError(f"{code} 成交量/额单位不一致")
    if state != "traded":
        values["return_1d"] = None
    return {"code": code, "name": name, "state": state, **values}


def industry_row(row: dict) -> dict:
    return {"code": code_value(row["code"]), "industry": required_text(row["industry"], "行业")}


def event_row(row: dict) -> dict:
    category = required_text(row["category"], "事件类别")
    url = required_text(row["url"], "公告原文")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("公告原文必须为不含账户信息的 HTTPS 链接")
    if category not in EVENT_TYPES:
        raise ValueError(f"未定义的事件类别: {category}")
    return {
        "event_id": required_text(row["event_id"], "公告 ID"), "code": code_value(row["code"]),
        "category": category, "title": required_text(row["title"], "公告标题"), "url": url,
        "published_at": timestamp(row["published_at"]) if row.get("published_at") else None,
        "effective_date": day_value(row["effective_date"]) if row.get("effective_date") else None,
    }


def optional_flag(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text not in {"1", "0", "true", "false"}:
        raise ValueError("状态字段必须为 true/false、1/0 或留空")
    return text in {"1", "true"}


def tradability_row(row: dict) -> dict:
    flags = {key: optional_flag(row.get(key)) for key in ("suspended", "risk_warning", "limit_applies")}
    up, down = number(row.get("limit_up"), "涨停价"), number(row.get("limit_down"), "跌停价")
    limits = flags["limit_applies"]
    if limits is True and (up is None or down is None or not 0 < down < up):
        raise ValueError("明确存在涨跌幅限制时必须提供一对有效限价")
    if limits is not True and (up is not None or down is not None):
        raise ValueError("提供限价前须明确 limit_applies=true")
    if all(value is None for value in flags.values()):
        raise ValueError("该证券没有任何已观察的状态字段")
    return {"code": code_value(row["code"]), **flags, "limit_up": up, "limit_down": down}
