from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from china_stock_engine.warehouse import Warehouse
from china_stock_engine.wind import collect, cross_section
from china_stock_engine.wind_session import WindBusyError, WindError


class WireTests(unittest.TestCase):
    codes = ["600000.SH", "000001.SZ"]
    day = "2026-11-12"

    def result(self, data):
        return {"codes": list(reversed(self.codes)), "fields": ["CLOSE"],
                "times": [self.day + "T00:00:00"], "data": data}

    def test_single_day_cross_axes_map_by_stock_code(self):
        for data in ([[2, 1]], [[2], [1]]):
            self.assertEqual(cross_section(self.result(data), self.codes, "close", self.day),
                             {"600000.SH": 1, "000001.SZ": 2})

    def test_wrong_date_or_shape_rejected(self):
        result = self.result([[2, 1]])
        with self.assertRaisesRegex(WindError, "日期轴"):
            cross_section(result, self.codes, "close", "2026-11-11")
        with self.assertRaisesRegex(WindError, "形状"):
            cross_section(self.result([[1], [1], [1]]), self.codes, "close", self.day)

    def test_busy_dashboard_rejects_before_native_spawn(self):
        from china_stock_engine.wind_session import WindSession
        with patch("china_stock_engine.wind_session.dashboard_running", return_value=True), \
             patch("china_stock_engine.wind_session.mp.get_context") as spawn:
            with self.assertRaises(WindBusyError), WindSession(exclusive_window=True):
                self.fail("看板运行时不应加载 SDK")
            spawn.assert_not_called()


class FakeWind:
    def __init__(self, sessions):
        self.sessions = sessions
        self.universe_days = []
        self.codes = [f"{n:06d}.SH" for n in range(600001, 604999)] + ["000001.SZ", "920001.BJ"]
        self.bad = None
        self.price = 10

    def __call__(self, **_options):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def request(self, method, *args):
        if method == "tdays":
            return {"data": [[day for day in self.sessions if args[0] <= day <= args[1]]]}
        if method == "wset":
            self.universe_days.append(args[1].split(";")[0].removeprefix("date="))
            return {"fields": ["wind_code", "sec_name"], "data": [self.codes, ["模拟证券"] * len(self.codes)]}
        codes, field, day, end, _options = args
        assert day == end
        values = {"open": 10, "high": 12, "low": 9, "close": self.price, "pre_close": 10,
                  "volume": 10000, "amt": 100000, "pct_chg": (self.price / 10 - 1) * 100}
        codes = codes.split(",")
        return {"codes": codes, "fields": [field],
                "times": ["2001-01-01" if day == self.bad else day],
                "data": [[values[field]] * len(codes)]}


class CollectTests(unittest.TestCase):
    def test_daily_collection_backfills_only_missing_and_repair_is_explicit(self):
        with TemporaryDirectory() as tmp:
            store = Warehouse(Path(tmp))
            first, middle, latest = "2026-11-10", "2026-11-11", "2026-11-12"
            wind = FakeWind([first, middle, latest])
            opts = {"session_factory": wind, "exclusive": True}
            self.assertEqual(collect(store, start=first, end=first, **opts)["published"], [first])
            old = store.path(store.read()["market"][first][0])
            mtime = old.stat().st_mtime_ns
            self.assertEqual(collect(store, end=latest, **opts)["published"], [middle, latest])
            self.assertEqual(wind.universe_days, [first, middle, latest])
            self.assertEqual(collect(store, end=latest)["state"], "up_to_date")
            self.assertEqual(old.stat().st_mtime_ns, mtime)
            wind.price = 10.5
            self.assertEqual(collect(store, start=latest, end=latest, refresh=True, **opts)["published"], [latest])
            self.assertEqual(collect(store, start=latest, end=latest, refresh=True, **opts)["unchanged"], [latest])
            self.assertEqual(store.validate()["market"], 4)

    def test_failed_native_day_does_not_persist_partial_market(self):
        with TemporaryDirectory() as tmp:
            store = Warehouse(Path(tmp))
            day = "2026-11-12"
            wind = FakeWind([day])
            wind.bad = day
            with self.assertRaisesRegex(WindError, "日期轴"):
                collect(store, start=day, end=day, session_factory=wind, exclusive=True)
            self.assertEqual(store.read()["market"], {})
            self.assertEqual(list(store.root.rglob("*.parquet")), [])


if __name__ == "__main__":
    unittest.main()
