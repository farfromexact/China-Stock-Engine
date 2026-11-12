"""Three stocks, one completed EOD; never publishes or changes login readiness."""
import argparse
from datetime import date
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from china_stock_engine.wind_session import WindSession
from china_stock_engine.wind_provider import WindProvider, adjustment_pair
from china_stock_engine.wind_collect import profile_from


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--login-confirmed", required=True, action="store_true")
    parser.add_argument("--matrix", action="store_true", help="Also verify single-day raw/adjusted multi-code matrices")
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    day = str(args.date)
    samples = []
    with WindSession(exclusive_window=True, allow_dashboard=True, request_interval=2.0,
                     timeout=30, lock_path=ROOT / ".private-stock-data/wind/coexistence-probe.lock") as session:
        for code in ("600000.SH", "000001.SZ", "600519.SH"):
            result = session.request("wsd", code, "open,high,low,close", day, day, "PriceAdj=U;Fill=Blank")
            if [str(f).lower() for f in result["fields"]] != ["open", "high", "low", "close"]:
                raise ValueError("Unexpected OHLC fields")
            if result["codes"] != [code] or len(result["times"]) != 1 or result["times"][0][:10] != day:
                raise ValueError("Unexpected source date/code")
            if len(result["data"]) != 4 or any(len(c) != 1 for c in result["data"]):
                raise ValueError("Unexpected OHLC shape")
            values = [c[0] for c in result["data"]]
            if any(not isinstance(v, (float, int)) or not math.isfinite(v) or v <= 0 for v in values):
                raise ValueError("Missing/invalid OHLC")
            op, high, low, close = values
            if not low <= min(op, close) <= max(op, close) <= high:
                raise ValueError("Invalid OHLC ordering")
            samples.append({"code": code, "source_date": day, "ohlc": values})
        if args.matrix:
            provider = WindProvider(session.request, profile_from(args.profile))
            codes = [sample["code"] for sample in samples]
            raw = provider.history_many(codes, day, day)
            adjusted = provider.history_many(codes, day, day, adjusted=True)
            for sample in samples:
                code = sample["code"]
                factors = adjustment_pair(raw[code], adjusted[code])
                if len(raw[code]) != 1 or len(factors) != 1:
                    raise ValueError("Matrix canary lacks one paired bar per code")
                if [float(raw[code][field].iloc[0]) for field in ("open", "high", "low", "close")] != sample["ohlc"]:
                    raise ValueError("Matrix and single-code OHLC differ")
                sample["single_day_matrix_verified"] = True
    print(json.dumps({"state": "canary_passed", "samples": samples, "production_changed": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
