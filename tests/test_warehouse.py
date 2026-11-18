from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from china_stock_engine.research import get_history, make_report, status
from china_stock_engine.schema import days_between
from china_stock_engine.warehouse import Warehouse

A, B, C = "600000.SH", "000001.SZ", "920001.BJ"
EVIDENCE = "2026-10-01T12:00:00+08:00"
SOURCE = "Authorized test"


def rows(*, active=False):
    result = []
    for code, name in ((A, "测试沪"), (B, "测试深"), (C, "测试北")):
        volume = 15_000_000 if active and code == A else 6_000_000
        result.append({"code": code, "name": name, "open": 10, "high": 11, "low": 9, "close": 10,
                       "pre_close": 10, "volume": volume, "amount": volume * 10,
                       "return_1d": 0.015 if code == A else 0.001})
    return result


class WarehouseTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.warehouse = Warehouse(self.root / "data")
        self.first = "2026-08-03"
        self.second = "2026-08-04"

    def put(self, day, contents=None, known_at=EVIDENCE):
        with self.warehouse.edit() as state:
            self.warehouse.add_calendar(state, day, day, [day], known_at, source=SOURCE)
            return self.warehouse.add_market(state, day, contents or rows(), known_at, source=SOURCE,
                                             universe="daily", min_securities=3)

    def put_window(self, gap=None):
        end_bound = (date.fromisoformat(self.first) + timedelta(days=28)).isoformat()
        sessions = [d for d in days_between(self.first, end_bound) if date.fromisoformat(d).weekday() < 5]
        with self.warehouse.edit() as state:
            self.warehouse.add_calendar(state, self.first, end_bound, sessions, EVIDENCE)
            for day in sessions:
                if day != gap:
                    self.warehouse.add_market(state, day, rows(active=day == sessions[-1]), EVIDENCE,
                                              source=SOURCE, universe="daily", min_securities=3)
        return sessions[-1]

    def test_one_day_append_and_identical_rerun(self):
        self.assertTrue(self.put(self.first))
        partition = self.warehouse.path(self.warehouse.read()["market"][self.first][0])
        previous = (partition.read_bytes(), partition.stat().st_mtime_ns)
        self.assertTrue(self.put(self.second))
        catalog = self.warehouse.catalog.read_bytes()
        self.assertFalse(self.put(self.second, known_at="2026-10-02T12:00:00+08:00"))
        self.assertEqual(self.warehouse.catalog.read_bytes(), catalog)
        self.assertEqual((partition.read_bytes(), partition.stat().st_mtime_ns), previous)
        self.assertEqual(len(list(self.warehouse.root.rglob("*.parquet"))), 2)

    def test_revision_preserves_information_time(self):
        self.put(self.first)
        revised = rows()
        revised[0]["close"] = 10.5
        known = "2026-10-02T12:00:00+08:00"
        self.assertTrue(self.put(self.first, revised, known_at=known))
        before = get_history(self.warehouse, A, as_of="2026-10-01T15:00:00+08:00")
        after = get_history(self.warehouse, A, as_of="2026-10-02T15:00:00+08:00")
        self.assertEqual(before["rows"][0]["close"], 10)
        self.assertEqual(after["rows"][0]["close"], 10.5)
        self.assertEqual(self.warehouse.validate()["market"], 2)

    def test_rejects_invalid_batch_without_changing_catalog(self):
        self.put(self.first)
        original = self.warehouse.catalog.read_bytes()
        with self.assertRaisesRegex(ValueError, "重复"):
            self.put(self.second, rows() + rows()[:1])
        self.assertEqual(self.warehouse.catalog.read_bytes(), original)

    def test_duplicate_content_cannot_hide_disk_corruption(self):
        self.put(self.first)
        entry = self.warehouse.read()["market"][self.first][0]
        self.warehouse.path(entry).write_bytes(b"damaged")
        with self.assertRaisesRegex(ValueError, "SHA256"):
            self.warehouse.validate()
        with self.assertRaisesRegex(ValueError, "SHA256"):
            self.put(self.first)

    def test_unknown_market_days_are_not_treated_as_closed(self):
        self.put(self.first, known_at="2026-08-03T18:15:00+08:00")
        answer = status(self.warehouse, "2026-08-04T19:00:00+08:00")
        self.assertEqual(answer["latest"], self.first)
        self.assertEqual(answer["freshness"], "calendar_unverified")
        self.assertEqual(answer["uncertified_days"], [self.second])

    def test_consecutive_window_prevents_gap_compression(self):
        end = self.put_window(gap=self.second)
        result = make_report(self.warehouse, day=end, as_of=EVIDENCE)
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["market"]["with_20d"], 0)

    def test_watchlist_uses_events_and_trade_states_known_at_cutoff(self):
        end = self.put_window()
        watch = self.root / "watch.csv"
        watch.write_text(f"code,kind\n{A},holding\n", encoding="utf-8")
        self.warehouse.add_industry([{"code": A, "industry": "银行"}],
                                    self.first, SOURCE, "SW1", EVIDENCE)
        event = {"event_id": "filing", "code": A, "category": "suspension", "title": "首次公告",
                 "url": "https://example.org/x.pdf", "published_at": "2026-10-01T20:00:00+08:00",
                 "effective_date": "2026-10-02"}
        observed = "2026-10-01T21:00:00+08:00"
        self.assertEqual(self.warehouse.add_events([event], SOURCE, observed), 1)
        event_files = list(self.warehouse.root.glob("events/**/*.parquet"))
        self.assertEqual(self.warehouse.add_events([event], SOURCE, "2026-10-02T08:00:00+08:00"), 0)
        self.assertEqual(list(self.warehouse.root.glob("events/**/*.parquet")), event_files)
        self.assertEqual(self.warehouse.add_events([{**event, "title": "更正公告"}], SOURCE,
                                                   "2026-10-02T09:00:00+08:00"), 1)
        risk_date = "2026-10-02"
        self.assertTrue(self.warehouse.add_tradability(
            [{"code": A, "suspended": "true"}], risk_date, SOURCE, observed))
        before = make_report(self.warehouse, day=end, as_of="2026-10-01T19:00:00+08:00",
                             watchlist=watch, risk_date=risk_date)
        after = make_report(self.warehouse, day=end, as_of="2026-10-01T23:00:00+08:00",
                            watchlist=watch, risk_date=risk_date)
        correction = make_report(self.warehouse, day=end, as_of="2026-10-02T10:00:00+08:00",
                                 watchlist=watch, risk_date=risk_date)
        self.assertEqual([row["code"] for row in before["candidates"]], [A])
        self.assertEqual(before["events"], [])
        self.assertIsNone(before["watchlist_risks"][0]["reported_status"])
        self.assertEqual(after["events"][0]["title"], "首次公告")
        self.assertTrue(after["watchlist_risks"][0]["reported_status"]["suspended"])
        self.assertEqual(correction["events"][0]["title"], "更正公告")

    def test_risk_warning_is_excluded_from_candidate_list(self):
        end = self.put_window()
        self.warehouse.add_tradability([{"code": A, "risk_warning": True, "limit_applies": True,
                                         "limit_up": 10.5, "limit_down": 9.5}],
                                       end, SOURCE, "2026-10-01T13:00:00+08:00")
        before = make_report(self.warehouse, day=end, as_of=EVIDENCE)
        after = make_report(self.warehouse, day=end, as_of="2026-10-01T14:00:00+08:00")
        self.assertEqual(before["candidate_count"], 1)
        self.assertEqual(after["candidate_count"], 0)


if __name__ == "__main__":
    unittest.main()
