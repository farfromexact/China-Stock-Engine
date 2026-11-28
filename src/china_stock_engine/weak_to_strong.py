"""Deterministic, daily A-share reversal scan and compact LLM evidence."""
from __future__ import annotations

from collections import Counter

import pandas as pd

PRODUCT = "weak_to_strong"
RULE_VERSION = "weak_to_strong_v1"
HISTORY_SESSIONS = 30
RECENT_SESSIONS = 3
MAX_CANDIDATES = 24
MAX_FAILURES = 6
MAX_PACKAGE_BYTES = 64 * 1024
RULE = {"weak_sessions": 5, "weak_return_max_pct": -5.0,
        "reclaim_sessions": 3, "amount_median_sessions": 5, "amount_ratio_min": 1.3,
        "recent_sessions": RECENT_SESSIONS}
ACTIVE = ("new", "holding")
STATES = (*ACTIVE, "failed", "unconfirmed")


def _number(value, digits=4):
    return round(float(value), digits)


def scan(bars: pd.DataFrame, universe: pd.DataFrame, days: list[str], *, include_history=False) -> dict:
    """Use certified session rows, including holes, so shifts cannot cross a gap."""
    codes = universe.thscode.tolist()
    names = universe.set_index("thscode").security_name.to_dict()
    frame = bars.assign(
        adjusted_close=bars.close * bars.adj_factor,
        adjusted_high=bars.high * bars.adj_factor,
        adjusted_open=bars.open * bars.adj_factor,
        one_price=bars.high.eq(bars.low),
    )

    def matrix(field):
        return frame.pivot(index="trade_date", columns="thscode", values=field).reindex(
            index=days, columns=codes)

    close = matrix("adjusted_close")
    amount = matrix("amount")
    traded = (close.gt(0) & amount.gt(0) & matrix("volume").gt(0)).fillna(False)
    valid = traded.rolling(7, min_periods=7).sum().eq(7)
    prior_close = close.shift(1)
    weak_return = (prior_close / close.shift(6) - 1).round(12)
    watch = matrix("adjusted_high").shift(1).rolling(3, min_periods=3).max()
    margin = (close / watch - 1).round(12)
    amount_ratio = (amount / amount.shift(1).rolling(5, min_periods=5).median()).round(12)
    trigger = (valid & weak_return.le(RULE["weak_return_max_pct"] / 100)
               & margin.gt(0) & amount_ratio.ge(RULE["amount_ratio_min"]))
    daily_return = (close / prior_close - 1).round(12).where(
        traded & traded.shift(1, fill_value=False))
    recent = days[-RECENT_SESSIONS:]
    market, medians = [], {}
    for day in days[6:] if include_history else recent:
        cross_section = daily_return.loc[day].dropna()
        median = None if cross_section.empty else float(cross_section.median())
        medians[day] = median
        market.append({"trade_date": day, "up": int(cross_section.gt(0).sum()),
                       "down": int(cross_section.lt(0).sum()), "flat": int(cross_section.eq(0).sum()),
                       "median_return_pct": None if median is None else _number(median * 100),
                       "eligible_for_signal": int(valid.loc[day].sum()),
                       "triggered": int(trigger.loc[day].sum())})

    open_price, factor, one_price = map(matrix, ("adjusted_open", "adj_factor", "one_price"))
    events = []
    for day in reversed(recent):
        matched = trigger.loc[day]
        window = days[days.index(day) - 6:]
        for code in matched.index[matched]:
            price = close.loc[day, code]
            watch_price = watch.loc[day, code]
            initial_price = close.loc[window[0], code]
            after = close.loc[window[7:], code].where(traded.loc[window[7:], code])
            failed = after.index[(after / watch_price - 1).round(12).lt(0)]
            if day == days[-1]:
                state = "new"
            elif len(failed):
                state = "failed"
            else:
                state = "unconfirmed" if after.isna().any() else "holding"
            delta = daily_return.loc[day, code]
            opened = open_price.loc[day, code]
            events.append({
                "code": code, "name": names[code], "trigger_date": day, "state": state,
                "weak_return_pct": _number(weak_return.loc[day, code] * 100),
                "watch_price_at_trigger_basis": _number(watch_price / factor.loc[day, code], 6),
                "above_watch_pct": _number(margin.loc[day, code] * 100, 10),
                "amount_ratio": _number(amount_ratio.loc[day, code]),
                "trigger_return_pct": _number(delta * 100),
                "above_market_pp": _number((delta - medians[day]) * 100, 10),
                "watch_index": _number(watch_price / initial_price * 100),
                "close_index_path": [
                    [date, _number(close.loc[date, code] / initial_price * 100)
                     if traded.loc[date, code] else None] for date in window
                ],
                "gap_up_close_below_open": bool(opened > prior_close.loc[day, code] and price < opened),
                "one_price": bool(one_price.loc[day, code]),
                "failure_date": failed[0] if len(failed) else None,
            })

    events.sort(key=lambda event: event["code"])
    events.sort(key=lambda event: (event["trigger_date"], event["above_market_pp"]), reverse=True)
    latest = {}
    for event in events:
        latest.setdefault(event["code"], event)
    selected = list(latest.values())
    counts = Counter(event["state"] for event in selected)
    active = [event for event in selected if event["state"] in ACTIVE]
    failures = sorted((event for event in selected if event["state"] == "failed"),
                      key=lambda event: event["failure_date"], reverse=True)
    return {"market": market, "events": events,
            "counts": {"total_triggers": len(events), "matched": len(selected),
                       **{state: counts[state] for state in STATES},
                       "omitted_candidates": max(0, len(active) - MAX_CANDIDATES),
                       "omitted_failures": max(0, len(failures) - MAX_FAILURES)},
            "candidates": active[:MAX_CANDIDATES], "recent_failures": failures[:MAX_FAILURES]}
