"""Read-only WindPy adapter. Field/unit profile requires live canary acceptance."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
import re
import math
import time
from typing import Callable
import pandas as pd

from .wind_session import WindError


QUOTE_MAP = {"open": "open", "high": "high", "low": "low", "close": "close",
             "pre_close": "pre_close", "avg_price": "vwap", "volume": "volume",
             "amount": "amt", "turnover_ratio": "turn", "change_ratio": "pct_chg"}
REFERENCE_MAP = {"listing_date": "ipo_date", "total_shares": "total_shares",
                 "float_a_shares": "float_a_shares"}
CODE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


@dataclass
class WindProfile:
    sector_id: str = "a001010100000000"
    calendar: str = "SSE"
    quote_fields: dict = field(default_factory=lambda: dict(QUOTE_MAP))
    reference_fields: dict = field(default_factory=lambda: dict(REFERENCE_MAP))
    raw_options: str = "PriceAdj=U;Days=Trading;Fill=Blank;unit=1"
    adjusted_options: str = "PriceAdj=B;Days=Trading;Fill=Blank;unit=1"
    volume_multiplier: float = 1.0
    amount_multiplier: float = 1.0
    shares_multiplier: float = 1.0
    units_verified: bool = False
    min_interval: float = 0.25

    def __post_init__(self):
        if type(self.units_verified) is not bool:
            raise ValueError("units_verified must be a reviewed boolean")
        if set(self.quote_fields) != set(QUOTE_MAP) or set(self.reference_fields) != set(REFERENCE_MAP):
            raise ValueError("Wind field profile must map every required normalized field")
        if any(not math.isfinite(v) or v <= 0 for v in
               (self.volume_multiplier, self.amount_multiplier, self.shares_multiplier)):
            raise ValueError("Wind unit multipliers must be positive")
        if not math.isfinite(self.min_interval) or self.min_interval < 0:
            raise ValueError("Wind request interval cannot be negative")
        if "Fill=Blank" not in self.raw_options or "Fill=Blank" not in self.adjusted_options:
            raise ValueError("Wind must preserve missing observations (Fill=Blank)")


def columns(result: dict) -> pd.DataFrame:
    names = [str(name).lower() for name in result["fields"]]
    values = result["data"]
    if len(names) != len(values) or len(set(names)) != len(names):
        raise WindError("Wind field axis is ambiguous")
    if len({len(column) for column in values}) > 1:
        raise WindError("Wind returned ragged columns")
    return pd.DataFrame(dict(zip(names, values)))


def date_values(values):
    return pd.to_datetime(values, errors="coerce").dt.strftime("%Y-%m-%d")


class WindProvider:
    provider = "windpy"
    source_endpoints = ("tdays", "wset:sectorconstituent", "wss", "wsd")

    def __init__(self, request: Callable, profile: WindProfile | None = None, *, request_interval: float | None = None):
        self._request = request
        self.profile = profile or WindProfile()
        self.request_interval = self.profile.min_interval if request_interval is None else request_interval
        if isinstance(self.request_interval, bool) or not isinstance(self.request_interval, (int, float)) or not math.isfinite(self.request_interval) or self.request_interval < 0:
            raise ValueError("request_interval must be finite and nonnegative")
        self.last_request = 0.0
        self.provenance = {"universe_sector": self.profile.sector_id,
                           "calendar": self.profile.calendar,
                           "units_verified": self.profile.units_verified,
                           "profile": asdict(self.profile),
                           "adapter_version": "windpy-v1"}

    def request(self, operation, *args):
        pause = self.request_interval - (time.monotonic() - self.last_request)
        if pause > 0:
            time.sleep(pause)
        self.last_request = time.monotonic()
        return self._request(operation, *args)

    def fetch_universe(self, trade_date):
        result = columns(self.request("wset", "sectorconstituent",
                         f"date={trade_date};sectorid={self.profile.sector_id}"))
        if not {"wind_code", "sec_name"} <= set(result):
            raise WindError("Wind universe lacks wind_code/sec_name")
        output = result.rename(columns={"wind_code": "thscode", "sec_name": "security_name"})
        output["thscode"] = output["thscode"].astype(str).str.upper().str.strip()
        if not output["thscode"].map(lambda value: bool(CODE.fullmatch(value))).all():
            raise WindError("Wind universe includes unsupported/non-A-share codes")
        if output.thscode.duplicated().any():
            raise WindError("Wind universe contains duplicate codes")
        output["as_of_date"] = trade_date
        output["security_name_in_time"] = output["security_name"]
        return output[["as_of_date", "thscode", "security_name", "security_name_in_time"]]

    def fetch_trade_calendar(self, as_of_date, *, offset=-10):
        if offset > 0:
            raise ValueError("calendar offset must not be positive")
        # Request a conservatively wide natural window, then count actual sessions.
        start = (datetime.fromisoformat(as_of_date) - timedelta(days=max(40, abs(offset) * 3))).date().isoformat()
        raw = self.request("tdays", start, as_of_date, f"TradingCalendar={self.profile.calendar}")
        if len(raw["data"]) != 1:
            raise WindError("Wind calendar has an unexpected axis")
        days = pd.to_datetime(pd.Series(raw["data"][0]), errors="raise").dt.strftime("%Y-%m-%d").tolist()
        if not days or days != sorted(set(days)) or any(d < start or d > as_of_date for d in days):
            raise WindError("Wind calendar is empty, duplicated or outside the requested window")
        return pd.DataFrame({"as_of_date": as_of_date, "trade_date": days,
            "calendar": self.profile.calendar, "market_code": self.profile.calendar,
            "is_open": True, "source_provider": self.provider, "source_endpoint": "tdays",
            "calendar_window_start": start, "calendar_window_end": as_of_date,
            "calendar_window_complete": True})

    def fetch_security_reference(self, codes, trade_date, *, batch_size=80,
                                 request_interval_seconds=0, progress=None):
        frames = []
        fields = list(self.profile.reference_fields.values())
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        batches = [codes[i:i + batch_size] for i in range(0, len(codes), batch_size)]
        for index, batch in enumerate(batches, 1):
            raw = self.request("wss", ",".join(batch), ",".join(fields), f"tradeDate={trade_date};unit=1")
            frame = columns(raw)
            returned = [str(code).upper() for code in raw["codes"]]
            if returned != batch or len(frame) != len(batch) or not set(fields) <= set(frame):
                raise WindError("Wind reference code/field axes do not match request")
            frame = frame.rename(columns={v: k for k, v in self.profile.reference_fields.items()})
            frame["thscode"], frame["as_of_date"] = batch, trade_date
            frame["listing_date"] = date_values(frame["listing_date"])
            for name in ("total_shares", "float_a_shares"):
                frame[name] = pd.to_numeric(frame[name], errors="coerce") * self.profile.shares_multiplier
            frame["source_provider"], frame["source_endpoint"] = self.provider, "wss"
            frames.append(frame)
            if progress:
                progress(index, len(batches))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def history(self, code, start, end, *, adjusted=False):
        if not CODE.fullmatch(code) or start > end:
            raise ValueError("invalid Wind code or date range")
        mapping = ({key: self.profile.quote_fields[key] for key in ("open", "high", "low", "close")}
                   if adjusted else self.profile.quote_fields)
        result = self.request("wsd", code, ",".join(mapping.values()), start, end,
                              self.profile.adjusted_options if adjusted else self.profile.raw_options)
        frame = columns(result)
        if not set(mapping.values()) <= set(frame) or len(frame) != len(result["times"]):
            raise WindError("Wind history time/field axes do not match")
        if result["codes"] and [str(c).upper() for c in result["codes"]] != [code]:
            raise WindError("Wind history returned a different code")
        frame = frame.rename(columns={v: k for k, v in mapping.items()})
        frame["trade_date"] = date_values(pd.Series(result["times"]))
        return self._normalize_history(frame, code, start, end, adjusted=adjusted)

    def _matrix(self, codes, mapping, start, end, *, adjusted=False):
        """Wind supports many-code/one-field matrices; preserve both axes."""
        if not codes or len(set(codes)) != len(codes) or any(not CODE.fullmatch(c) for c in codes):
            raise ValueError("invalid/duplicate Wind batch codes")
        if start > end:
            raise ValueError("invalid Wind date range")
        frames = {code: pd.DataFrame() for code in codes}
        expected_times = None
        options = self.profile.adjusted_options if adjusted else self.profile.raw_options
        for name, vendor_field in mapping.items():
            result = self.request("wsd", ",".join(codes), vendor_field, start, end, options)
            returned = [str(code).upper() for code in result["codes"]]
            times = result["times"]
            data = result["data"]
            # Native Wind returns a field-by-code cross section for a single
            # date, versus code-by-time rows for a multi-date matrix. Both axes
            # must still match explicitly; never transpose an ambiguous matrix.
            if (len(times) == 1 and len(returned) > 1 and len(data) == 1
                    and len(data[0]) == len(returned)):
                data = [[value] for value in data[0]]
            if ([str(v).lower() for v in result["fields"]] != [vendor_field.lower()]
                    or len(returned) != len(codes) or set(returned) != set(codes)
                    or len(data) != len(returned)
                    or any(len(column) != len(times) for column in data)):
                raise WindError(f"Wind matrix code/field/time axes do not match request "
                                f"(requested_codes={len(codes)}, returned_codes={len(returned)}, "
                                f"time_points={len(times)}, rows={len(result['data'])})")
            if expected_times is None:
                expected_times = times
            elif times != expected_times:
                raise WindError("Wind matrix time axes differ across fields")
            for code, values in zip(returned, data):
                frames[code][name] = values
        for code, frame in frames.items():
            frame["trade_date"] = date_values(pd.Series(expected_times))
        return frames

    def history_many(self, codes, start, end, *, adjusted=False):
        mapping = ({key: self.profile.quote_fields[key] for key in ("open", "high", "low", "close")}
                   if adjusted else self.profile.quote_fields)
        frames = self._matrix(codes, mapping, start, end, adjusted=adjusted)
        return {code: self._normalize_history(frame, code, start, end, adjusted=adjusted)
                for code, frame in frames.items()}

    def close_many(self, codes, start, end, *, adjusted=False):
        """Small revision probe using the same verified one-field matrix API."""
        frames = self._matrix(codes, {"close": self.profile.quote_fields["close"]},
                              start, end, adjusted=adjusted)
        for code, frame in frames.items():
            if (frame.trade_date.isna().any() or frame.trade_date.duplicated().any()
                    or not frame.trade_date.between(start, end).all()):
                raise WindError("Wind close probe has invalid dates")
            frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
            frame = frame.loc[frame.close.gt(0)].copy()
            if not frame.close.lt(float("inf")).all():
                raise WindError("Wind close probe contains infinite prices")
            frame["thscode"] = code
            frames[code] = frame.reset_index(drop=True)
        return frames

    def _normalize_history(self, frame, code, start, end, *, adjusted=False):
        mapping = ({key: self.profile.quote_fields[key] for key in ("open", "high", "low", "close")}
                   if adjusted else self.profile.quote_fields)
        if frame.trade_date.isna().any() or frame.trade_date.duplicated().any() or not frame.trade_date.between(start, end).all():
            raise WindError("Wind history has duplicate, invalid or out-of-range dates")
        for name in mapping:
            frame[name] = pd.to_numeric(frame[name], errors="coerce")
        # No Fill=Previous; blank prices remain unobserved rather than fake bars.
        frame = frame.loc[frame["close"].gt(0)].copy()
        for name in ("open", "high", "low", "close"):
            if not frame[name].gt(0).all() or not frame[name].lt(float("inf")).all():
                raise WindError("Wind history contains invalid OHLC")
        if ((frame.low > frame[["open", "close"]].min(axis=1))
                | (frame.high < frame[["open", "close"]].max(axis=1))).any():
            raise WindError("Wind history violates OHLC bounds")
        frame["thscode"] = code
        if not adjusted:
            frame["volume"] *= self.profile.volume_multiplier
            frame["amount"] *= self.profile.amount_multiplier
            values = frame[["volume", "amount"]]
            if values.lt(0).any().any() or values.eq(float("inf")).any().any():
                raise WindError("Wind history has negative volume/amount")
            traded = frame.volume.gt(0) & frame.amount.gt(0)
            vwap = frame.amount / frame.volume.where(traded)
            if (traded & (vwap.lt(frame.low / 3) | vwap.gt(frame.high * 3))).any():
                raise WindError("Wind amount/volume unit consistency check failed")
            frame["source_provider"], frame["source_endpoint"] = self.provider, "wsd"
        return frame.reset_index(drop=True)

    def fetch_daily_quotes(self, codes, trade_date, *, batch_size=1,
                           request_interval_seconds=0, progress=None):
        frames = []
        for index, code in enumerate(codes, 1):
            frames.append(self.history(code, trade_date, trade_date))
            if progress:
                progress(index, len(codes))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def adjustment_pair(raw, adjusted, *, known_at=None):
    """Validate a single Wind normalization vintage before deriving multipliers."""
    keys = ["trade_date", "thscode"]
    if raw.duplicated(keys).any() or adjusted.duplicated(keys).any():
        raise WindError("Duplicate Wind adjustment keys")
    if set(map(tuple, raw[keys].values)) != set(map(tuple, adjusted[keys].values)):
        raise WindError("Wind raw/adjusted observations have different keys")
    joined = raw.merge(adjusted[keys + ["open", "high", "low", "close"]], on=keys,
                       validate="one_to_one", suffixes=("", "_adjusted"))
    factor = joined.close_adjusted / joined.close
    if not factor.gt(0).all() or not factor.lt(float("inf")).all():
        raise WindError("Invalid Wind adjustment factor")
    for name in ("open", "high", "low", "close"):
        error = (joined[name] * factor - joined[name + "_adjusted"]).abs()
        if error.isna().any() or error.gt(0.02 + joined[name + "_adjusted"].abs() * 1e-5).any():
            raise WindError("Wind adjusted OHLC is not a validated multiplicative mapping")
    observed = datetime.now(timezone.utc).isoformat()
    if known_at and pd.Timestamp(known_at) > pd.Timestamp(observed):
        raise ValueError("known_at must not be in the future")
    # A caller cannot backdate knowledge of newly fetched corporate actions.
    output = joined[keys].copy()
    output["adj_factor"] = factor
    output["raw_close"] = joined.close
    output["forward_adj_close"] = joined.close_adjusted
    output["base_date"] = str(joined.trade_date.max()) if not joined.empty else pd.NA
    output["known_at"] = observed
    output["collection_completed_at"] = observed
    output["effective_at"] = output.trade_date
    output["published_at"] = pd.NA
    output["source_provider"], output["source_endpoint"] = "windpy", "wsd"
    output["adjustment_method"] = "wind_native_adjusted_ohlc_validated_ratio"
    return output
