"""Auditable full-universe price/amount rules. Signals are not executions."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

STRATEGY_VERSION = "price-volume-v1"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config/technical_signals.json"


@dataclass(frozen=True)
class TechnicalConfig:
    history_sessions: int = 300
    min_listing_sessions: int = 120
    min_prior_adt20: float = 50_000_000.0
    breakout_amount_ratio: float = 1.5
    min_close_location: float = 0.75
    max_watch_distance_atr: float = 1.0
    max_atr5_to_atr20: float = 0.70
    contraction_tag_ratio: float = 0.80
    max_entry_distance_atr: float = 1.0
    candidate_limit: int = 100

    def __post_init__(self):
        for field in ("history_sessions", "min_listing_sessions", "candidate_limit"):
            value = getattr(self, field)
            if type(value) is not int or value < 1:
                raise ValueError(f"{field} must be a positive integer")
        if self.history_sessions < 61 or self.candidate_limit > 100:
            raise ValueError("history_sessions >= 61 and candidate_limit <= 100 required")
        for field, value in asdict(self).items():
            if isinstance(value, bool) or not math.isfinite(float(value)) or value <= 0:
                raise ValueError(f"invalid technical parameter: {field}")
        if self.min_close_location > 1:
            raise ValueError("min_close_location must be <= 1")


def load_technical_config(path: Path | None = None) -> TechnicalConfig:
    selected = path or DEFAULT_CONFIG_PATH
    if not selected.exists() and path is None:
        return TechnicalConfig()
    return TechnicalConfig(**json.loads(selected.read_text(encoding="utf-8")))


TECHNICAL_METRIC_COLUMNS = (
    "adj_multiplier", "adj_offset",
    "adjusted_open", "adjusted_high", "adjusted_low", "adjusted_close",
    "prior_high60", "prior_adt20", "amount_ratio20", "ma20", "ma60",
    "atr5", "atr20", "prior_atr20", "atr5_to_atr20", "amount5_to_amount20",
    "breakout_distance_atr", "watch_distance_atr", "technical_close_location",
    "technical_window_ready", "valid_ohlc_sessions", "current_bar_valid",
)


def _ratio(a: pd.Series, b: pd.Series) -> pd.Series:
    return a / b.where(b.gt(0))


def add_technical_metrics(history: pd.DataFrame) -> pd.DataFrame:
    """Input is a session-reindexed, single-factor-vintage OHLC panel.

    Invalid/zero-trade/unverified bars break windows; they are never compressed
    out of the calendar. ATR is the simple mean of true ranges, not Wilder EMA.
    """
    out = history.sort_values(["thscode", "trade_date"]).copy()
    prices = out[[f"adjusted_{f}" for f in ("open", "high", "low", "close")]].apply(pd.to_numeric, errors="coerce")
    finite = prices.apply(lambda values: values.map(lambda v: pd.notna(v) and math.isfinite(float(v))))
    amount = pd.to_numeric(out["amount"], errors="coerce")
    volume = pd.to_numeric(out["volume"], errors="coerce")
    o, h, l, c = (prices[f"adjusted_{f}"] for f in ("open", "high", "low", "close"))
    trade_valid = (amount.gt(0) & volume.gt(0)
             & amount.map(lambda v: pd.notna(v) and math.isfinite(float(v)))
             & volume.map(lambda v: pd.notna(v) and math.isfinite(float(v))))
    verified = out.get("history_verified", pd.Series(False, index=out.index)).fillna(False).eq(True)
    trade_valid &= verified
    valid = (finite.all(axis=1) & prices.gt(0).all(axis=1)
             & h.ge(pd.concat([o, c, l], axis=1).max(axis=1))
             & l.le(pd.concat([o, c, h], axis=1).min(axis=1)) & trade_valid)
    out["current_bar_valid"] = valid
    groups = out["thscode"]

    def roll(values, window, operation="mean", prior=False):
        return values.groupby(groups).transform(lambda s: getattr((s.shift(1) if prior else s).rolling(window, min_periods=window), operation)())

    c, h, l = (s.where(valid) for s in (c, h, l))
    amount = amount.where(trade_valid)
    previous = c.groupby(groups).shift(1)
    tr = pd.concat([h - l, (h - previous).abs(), (l - previous).abs()], axis=1).max(axis=1).where(previous.notna() & valid)
    out["prior_high60"] = roll(h, 60, "max", prior=True)
    out["prior_adt20"] = roll(amount, 20, prior=True)
    out["amount_ratio20"] = _ratio(amount, out["prior_adt20"])
    out["ma20"], out["ma60"] = roll(c, 20), roll(c, 60)
    out["atr5"], out["atr20"] = roll(tr, 5), roll(tr, 20)
    out["prior_atr20"] = roll(tr, 20, prior=True)
    out["atr5_to_atr20"] = _ratio(out["atr5"], out["atr20"])
    out["amount5_to_amount20"] = _ratio(roll(amount, 5), roll(amount, 20))
    out["breakout_distance_atr"] = _ratio(c - out["prior_high60"], out["prior_atr20"])
    out["watch_distance_atr"] = _ratio(out["prior_high60"] - c, out["atr20"])
    out["technical_close_location"] = _ratio(c - l, h - l)
    out["technical_window_ready"] = roll(valid.astype(float), 61, "sum").eq(61)
    out["valid_ohlc_sessions"] = valid.groupby(groups).cumsum()
    return out


def number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def boolean(value: Any) -> bool | None:
    return None if value is None or pd.isna(value) else bool(value)


def scan_technical_signals(state: pd.DataFrame, reference: pd.DataFrame, sessions: list[str],
                           trade_date: str, config: TechnicalConfig) -> tuple[pd.DataFrame, dict]:
    """Scan every PIT reference security, including missing-quote exclusions."""
    refs = reference.sort_values("as_of_date").drop_duplicates("thscode", keep="last").set_index("thscode")
    states = state.drop_duplicates("thscode").set_index("thscode")
    dates = sorted(set(day for day in sessions if day <= trade_date))
    from .storage import json_sha256
    config_hash = json_sha256(asdict(config))
    rows = []
    for code, ref in refs.sort_index().iterrows():
        row = states.loc[code] if code in states.index else pd.Series(dtype=object)
        reasons, pending = [], []
        listing = ref.get("listing_date")
        age = sum(day >= str(listing) for day in dates) if pd.notna(listing) else None
        observed_age = number(row.get("valid_ohlc_sessions"))
        if age is None and observed_age is not None and observed_age >= config.min_listing_sessions:
            # Observed distinct certified trading bars establish an age lower
            # bound even when the exchange listing-date field is unavailable.
            age = int(observed_age)
        if age is None:
            pending.append("listing_age_unknown")
        elif age < config.min_listing_sessions:
            # This is a lower bound if the cached calendar begins after listing.
            if dates and str(listing) >= dates[0]:
                reasons.append("listing_age_below_minimum")
            else:
                pending.append("listing_age_not_certified")
        if row.empty:
            reasons.append("current_quote_missing")
        elif boolean(row.get("current_bar_valid")) is not True:
            reasons.append("current_bar_invalid_or_unverified")
        if boolean(row.get("technical_window_ready")) is not True:
            reasons.append("history_or_adjustment_gap_61_sessions")
        quote_reasons = row.get("tradability_reason_codes")
        if isinstance(quote_reasons, (list, tuple)) and "quote_observation_not_traded" in quote_reasons:
            reasons.append("current_observation_not_traded")
        adt = number(row.get("prior_adt20"))
        if adt is None:
            reasons.append("prior_adt20_missing")
        elif adt < config.min_prior_adt20:
            reasons.append("prior_adt20_below_minimum")
        for field, excluded in (("is_st", "st_security"), ("is_suspended", "suspended_security")):
            value = boolean(row.get(field))
            if value is True:
                reasons.append(excluded)
            elif value is None:
                pending.append(f"{field}_unknown")
        delisting = boolean(ref.get("is_delisting", row.get("is_delisting")))
        name = str(ref.get("security_name") or "")
        if name.upper().startswith(("ST", "*ST", "S*ST")):
            reasons.append("st_security")
        # A disclosed delisting-period name is sufficient positive evidence;
        # absence of that name is not a provider confirmation of false.
        if delisting is True or "退" in name:
            reasons.append("delisting_security")
        elif delisting is None:
            pending.append("delisting_status_unknown")
        # Listed status remains independent of mathematical pattern matching.
        close = number(row.get("adjusted_close"))
        high = number(row.get("prior_high60"))
        ratio = number(row.get("amount_ratio20"))
        location = number(row.get("technical_close_location"))
        atr = number(row.get("prior_atr20"))
        price_break = close is not None and high is not None and close > high
        activity = ratio is not None and ratio >= config.breakout_amount_ratio
        strong = location is not None and location >= config.min_close_location
        ma20, ma60 = number(row.get("ma20")), number(row.get("ma60"))
        distance, contraction = number(row.get("watch_distance_atr")), number(row.get("atr5_to_atr20"))
        trend = close is not None and ma20 is not None and ma60 is not None and close > ma20 > ma60
        near = distance is not None and 0 <= distance <= config.max_watch_distance_atr
        narrow = contraction is not None and contraction <= config.max_atr5_to_atr20
        signal = "NONE"
        if not reasons:
            if price_break and activity and strong:
                signal = "BREAKOUT"
            elif trend and near and narrow:
                signal = "WATCH"
        eligibility = "excluded" if reasons else "pending" if pending else "eligible"
        entry = "not_applicable"
        entry_reasons = []
        extension = number(row.get("breakout_distance_atr"))
        if signal == "BREAKOUT":
            if atr is None or atr <= 0 or extension is None:
                entry_reasons.append("prior_atr20_missing_or_zero")
            elif extension > config.max_entry_distance_atr:
                entry_reasons.append("extended_above_breakout")
            for field in ("limit_up", "one_word_limit"):
                flag = boolean(row.get(field))
                if flag is True:
                    entry_reasons.append(field)
                elif flag is None:
                    pending.append(f"{field}_unknown")
            for field in ("daily_price_limit_pct", "lot_size"):
                if number(row.get(field)) is None:
                    pending.append(f"{field}_unknown")
            if pending:
                eligibility = "pending"
            entry = "wait" if entry_reasons else "pending_verification" if pending else "review_next_session"
        factor = number(row.get("adj_factor"))
        multiplier = number(row.get("adj_multiplier")) or factor
        offset = number(row.get("adj_offset")) or 0.
        rows.append({
            "trade_date": trade_date, "thscode": str(code), "security_name": name,
            "strategy_version": STRATEGY_VERSION, "strategy_config_sha256": config_hash, "eligibility": eligibility,
            "exclusion_reasons": sorted(set(reasons)), "pending_reasons": sorted(set(pending)),
            "signal": signal, "listing_age_sessions_lower_bound": age,
            "a0": not reasons and price_break, "a1": not reasons and price_break and activity,
            "a2": signal == "BREAKOUT", "watch_trend": trend, "watch_near_high": near,
            "watch_contraction": narrow, "breakout_price_condition": price_break,
            "breakout_amount_condition": activity, "breakout_close_condition": strong,
            "entry_state": entry, "entry_reason_codes": entry_reasons,
            "shrink_amount_tag": (number(row.get("amount5_to_amount20")) is not None
                                  and number(row.get("amount5_to_amount20")) <= config.contraction_tag_ratio),
            "raw_close": number(row.get("raw_close")), "adj_factor": factor,
            "breakout_level_raw": (high - offset) / multiplier if high is not None and multiplier and multiplier > 0 else None,
            **{key: number(row.get(key)) for key in TECHNICAL_METRIC_COLUMNS
               if key not in ("technical_window_ready", "current_bar_valid")},
        })
    scan = pd.DataFrame(rows)
    counts = Counter(row["signal"] for row in rows)
    excluded = Counter(reason for row in rows for reason in row["exclusion_reasons"])
    pending_counts = Counter(reason for row in rows for reason in row["pending_reasons"])
    summary = {
        "strategy_version": STRATEGY_VERSION, "config": asdict(config),
        "universe_count": len(rows), "scanned_count": len(rows),
        "eligibility_counts": dict(Counter(row["eligibility"] for row in rows)),
        "signal_counts": dict(counts), "exclusion_reason_counts": dict(sorted(excluded.items())),
        "pending_reason_counts": dict(sorted(pending_counts.items())),
        "comparison_counts": {key.upper(): sum(bool(row[key]) for row in rows) for key in ("a0", "a1", "a2")},
        "atr_method": "simple_mean_true_range_adjusted_ohlc; previous_close_required",
        "window_policy": "prior_high60 and prior_adt20 exclude today; WATCH ATR/MA include today; entry ATR excludes today",
        "execution_policy": "end_of_day_signal; next_session_execution_unverified; WATCH_is_observation_only",
        "price_basis": "single_PIT_native_adjusted_OHLC_or_factor_vintage; raw_level=(adjusted_level-offset)/multiplier",
        "all_results_path": f"features/technical_signals/trade_date={trade_date}/technical_scan.parquet",
    }
    return scan, summary


def technical_candidates(scan: pd.DataFrame) -> list[dict]:
    """Full matching set. Transport priority is explicit and never a score."""
    if scan.empty:
        return []
    matches = scan.loc[scan["signal"].isin(["BREAKOUT", "WATCH"])].copy()
    matches["_order"] = matches["entry_state"].map({"review_next_session": 0, "not_applicable": 1, "pending_verification": 2, "wait": 3})
    matches = matches.sort_values(["_order", "signal", "thscode"])
    keys = ("thscode", "security_name", "signal", "eligibility", "entry_state", "entry_reason_codes", "pending_reasons",
            "raw_close", "breakout_level_raw", "prior_high60", "prior_adt20", "amount_ratio20", "technical_close_location",
            "atr5", "atr20", "prior_atr20", "breakout_distance_atr", "watch_distance_atr", "atr5_to_atr20",
            "ma20", "ma60", "shrink_amount_tag", "lifecycle", "anchor_date", "anchor_level_raw", "anchor_level_adjusted")
    return [{key: (number(row[key]) if isinstance(row[key], (int, float)) and not isinstance(row[key], bool) else row[key])
             for key in keys if key in row} for row in matches.to_dict("records")]


def attach_signal_history(data_dir: Path, scan: pd.DataFrame, adjusted: pd.DataFrame,
                          trade_date: str, cutoff: str, config: TechnicalConfig) -> pd.DataFrame:
    """Persist the original breakout anchor; never substitute a later rolling high.

    Rescale the original raw anchor with the current vintage's factor on the
    anchor date. A missing factor leaves monitoring unknown rather than inventing
    a comparable anchor. Every daily scan includes NONE/excluded rows as well.
    """
    from .storage import load_json_object, sha256_file, ArtifactContractError
    root = data_dir / "features/technical_signals"
    receipts = list(root.glob("trade_date=*/signal_receipt.json")) + list(root.glob("trade_date=*/research_view=*/signal_receipt.json"))
    before = []
    for receipt_path in receipts:
        receipt = load_json_object(receipt_path)
        day = receipt.get("trade_date") or (receipt_path.parent.parent.name if receipt_path.parent.name.startswith("research_view=") else receipt_path.parent.name).removeprefix("trade_date=")
        observed = receipt.get("effective_pit_cutoff")
        if day < trade_date and observed and pd.Timestamp(observed) <= pd.Timestamp(cutoff):
            before.append((str(day), pd.Timestamp(observed), str(receipt_path), receipt))
    previous = pd.DataFrame()
    for _, _, receipt_path, receipt in sorted(before, reverse=True):
        path = Path(receipt_path).parent / "technical_scan.parquet"
        if (receipt.get("artifacts", {}).get(path.name) or {}).get("sha256") != sha256_file(path):
            raise ArtifactContractError("technical history hash mismatch")
        previous = pd.read_parquet(path).set_index("thscode")
        break
    current_path = data_dir / "features/technical_signals" / f"trade_date={trade_date}" / "technical_scan.parquet"
    corrections = pd.read_parquet(current_path).set_index("thscode") if current_path.exists() else pd.DataFrame()
    factors = adjusted.set_index(["thscode", "trade_date"]) if not adjusted.empty else pd.DataFrame()
    rows = []
    for row in scan.to_dict("records"):
        old = previous.loc[row["thscode"]] if row["thscode"] in previous.index else pd.Series(dtype=object)
        anchor_date = old.get("anchor_date")
        active = (str(old.get("lifecycle")) in ("first_breakout", "continuation", "monitoring_unknown")
                  and old.get("strategy_config_sha256") == row["strategy_config_sha256"])
        anchor_raw = number(old.get("anchor_level_raw")) if active else None
        row.update(lifecycle="none", anchor_date=None, anchor_level_raw=None, anchor_level_adjusted=None,
                   data_correction=False, parameter_change=False)
        if active and pd.notna(anchor_date) and anchor_raw is not None:
            key = (row["thscode"], str(anchor_date))
            anchor_row = factors.loc[key] if key in factors.index else pd.Series(dtype=object)
            anchor_factor = number(anchor_row.get("adj_multiplier")) or number(anchor_row.get("adj_factor"))
            anchor_offset = number(anchor_row.get("adj_offset")) or 0.
            anchor = anchor_raw * anchor_factor + anchor_offset if anchor_factor else None
            row.update(anchor_date=str(anchor_date), anchor_level_raw=anchor_raw, anchor_level_adjusted=anchor)
            close = number(row.get("adjusted_close"))
            row["lifecycle"] = ("monitoring_unknown" if anchor is None or close is None or row["eligibility"] == "excluded"
                                else "failed" if close <= anchor else "continuation")
            if row["signal"] == "BREAKOUT" and anchor is not None:
                prior_atr = number(row.get("prior_atr20"))
                row["breakout_distance_atr"] = (close - anchor) / prior_atr if prior_atr and prior_atr > 0 else None
                entry_reasons = [reason for reason in row["entry_reason_codes"] if reason != "extended_above_breakout"]
                if row["breakout_distance_atr"] is None:
                    entry_reasons.append("anchor_distance_unknown")
                elif row["breakout_distance_atr"] > config.max_entry_distance_atr:
                    entry_reasons.append("extended_above_breakout")
                row["entry_reason_codes"] = sorted(set(entry_reasons))
                row["entry_state"] = "wait" if entry_reasons else "pending_verification" if row["pending_reasons"] else "review_next_session"
        elif row["signal"] == "BREAKOUT":
            row.update(lifecycle="first_breakout", anchor_date=trade_date,
                       anchor_level_raw=row["breakout_level_raw"], anchor_level_adjusted=row["prior_high60"])
        elif row["signal"] == "WATCH":
            row["lifecycle"] = "watch"
        if row["thscode"] in corrections.index:
            former = corrections.loc[row["thscode"]]
            row["parameter_change"] = former.get("strategy_config_sha256") != row["strategy_config_sha256"]
            row["data_correction"] = not row["parameter_change"] and (any(
                (number(former.get(key)) != number(row.get(key)) if key in ("raw_close", "prior_high60", "amount_ratio20")
                 else str(former.get(key)) != str(row.get(key)))
                for key in ("signal", "eligibility", "raw_close", "prior_high60", "amount_ratio20")
            ) or boolean(former.get("data_correction")) is True)
        rows.append(row)
    return pd.DataFrame(rows)
