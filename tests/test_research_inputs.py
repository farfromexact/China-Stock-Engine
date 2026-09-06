import copy
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import pandas as pd

from china_stock_engine.research_inputs import (
    build_research_artifacts,
    financial_features,
    import_research_batch,
    normalize_research_batch,
    event_price_response,
)
from china_stock_engine.storage import (
    ArtifactContractError,
    atomic_write_json,
    serialize_json,
)


def record(
    period="2026-06-30",
    profit=30.0,
    *,
    module="financials",
    known="2026-08-28T10:00:00+00:00",
):
    row = {
        "thscode": "600001.SH",
        "published_at": known,
        "first_seen_at": known,
        "known_at": known,
        "revision": "v1",
        "source_url": "https://example.org/synthetic-filing",
        "document_sha256": None,
    }
    if module == "financials":
        row.update(
            report_period=period,
            period_start=period[:4] + "-01-01",
            period_basis="YTD",
            accounting_scope="consolidated",
            currency="CNY",
            unit="CNY",
            values={
                "revenue": 100.0,
                "net_profit_parent": profit,
                "operating_cash_flow": -1.0,
                "equity_parent": 200.0,
            },
        )
    else:
        row.update(
            event_id="synthetic-event-1",
            event_type="buyback",
            event_date="2026-09-30",
            status="announced",
            title="Synthetic plan, not a completed transaction",
        )
    return row


def batch(
    rows, module="financials", complete=True, completed="2026-08-28T10:30:00+00:00"
):
    return {
        "schema_version": 1,
        "module": module,
        "collection_started_at": "2026-08-28T09:00:00+00:00",
        "collection_completed_at": completed,
        "coverage": {
            "thscodes": ["600001.SH"],
            "complete": complete,
            "period_start": "2024-01-01",
            "period_end": "2026-08-28",
        },
        "records": rows,
    }


def source_and_frames():
    source = {
        "trade_date": "2026-08-28",
        "source_snapshot_sha256": "a" * 64,
        "pit_timing": {
            "effective_pit_cutoff": "2026-08-28T10:30:00+00:00",
            "configured_decision_cutoff": "2026-08-28T20:15:00+08:00",
        },
    }
    state = pd.DataFrame({"thscode": ["600001.SH"], "total_market_cap": [600.0]})
    history = pd.DataFrame(
        {
            "trade_date": ["2026-08-27", "2026-08-28"],
            "thscode": ["600001.SH"] * 2,
            "change_ratio": [1.0, 2.0],
        }
    )
    reference = pd.DataFrame(
        {"thscode": ["600001.SH", "000001.SZ"], "security_name": ["A", "B"]}
    )
    return source, state, history, reference


class ResearchInputTests(unittest.TestCase):
    def test_unconfigured_financial_canary_never_constructs_a_client(self):
        from china_stock_engine.cli import main

        with (
            patch(
                "china_stock_engine.cli._client",
                side_effect=AssertionError("must not request credentials"),
            ),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(main(["canary", "--module", "financials"]), 2)

    def test_stale_empty_event_query_does_not_confirm_absence_at_later_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incoming = root / "input.json"
            atomic_write_json(
                incoming,
                batch([], module="events", completed="2026-08-28T10:00:00+00:00"),
            )
            import_research_batch(root, incoming)
            outputs = build_research_artifacts(root, *source_and_frames())
            security = next(
                row
                for name, shard in outputs.items()
                if name != "research_inputs_latest.json"
                for row in shard["securities"]
                if row["thscode"] == "600001.SH"
            )
            self.assertEqual(security["availability"]["events"], "unknown")

    def test_unit_normalization_ytd_quarter_ttm_and_negative_denominator(self):
        rows = [
            record("2025-06-30", 10),
            record("2025-12-31", 40),
            record("2026-03-31", 12),
            record("2026-06-30", 30),
        ]
        for row in rows:
            row["unit"] = "CNY_1e4"
        normalized = normalize_research_batch(batch(rows))["records"]
        result = financial_features(normalized, 6_000_000)
        self.assertEqual(result["quarter_flows"]["net_profit_parent"], 180_000)
        self.assertEqual(result["ttm_flows"]["net_profit_parent"], 600_000)
        self.assertEqual(result["valuation"]["pe_ttm"], 10)
        self.assertEqual(result["yoy_pct"]["net_profit_parent"], 200)
        self.assertTrue(result["profit_positive_cashflow_negative"])
        self.assertIsNone(result["valuation"]["dividend_yield"])
        negative = financial_features(
            normalize_research_batch(batch([record("2025-12-31", -5)]))["records"], 100
        )
        self.assertIsNone(negative["valuation"]["pe_ttm"])
        self.assertEqual(negative["valuation"]["pe_state"], "not_meaningful")

    def test_schema_pit_and_nonfinite_values_fail_closed(self):
        bad = batch([record()])
        bad["records"][0]["first_seen_at"] = "2026-08-27T10:00:00+00:00"
        with self.assertRaisesRegex(ArtifactContractError, "PIT"):
            normalize_research_batch(bad)
        bad = batch([record()])
        bad["raw_payload"] = {}
        with self.assertRaisesRegex(ArtifactContractError, "raw payloads"):
            normalize_research_batch(bad)
        bad = batch([record()])
        bad["records"][0]["values"]["revenue"] = float("inf")
        with self.assertRaisesRegex(ArtifactContractError, "finite"):
            normalize_research_batch(bad)

    def test_append_idempotence_revisions_and_late_information_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incoming = root / "input.json"
            atomic_write_json(incoming, batch([record()]))
            first = import_research_batch(root, incoming)
            second = import_research_batch(root, incoming)
            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            source, state, history, reference = source_and_frames()
            before = build_research_artifacts(root, source, state, history, reference)
            later = record(profit=999, known="2026-08-28T13:00:00+00:00")
            later["revision"] = "v2"
            atomic_write_json(
                incoming, batch([later], completed="2026-08-28T13:10:00+00:00")
            )
            import_research_batch(root, incoming)
            after = build_research_artifacts(root, source, state, history, reference)
            self.assertEqual(
                before, after
            )  # 21:00 correction cannot enter a 20:15 vintage.
            source["pit_timing"][
                "configured_decision_cutoff"
            ] = "2026-08-28T22:00:00+08:00"
            revised = build_research_artifacts(root, source, state, history, reference)
            index = revised["research_inputs_latest.json"]
            self.assertEqual(
                index["market_effective_pit_cutoff"], "2026-08-28T10:30:00+00:00"
            )
            self.assertEqual(
                index["research_effective_pit_cutoff"], "2026-08-28T13:10:00+00:00"
            )
            security = next(
                row
                for name, shard in revised.items()
                if name != "research_inputs_latest.json"
                for row in shard["securities"]
                if row["thscode"] == "600001.SH"
            )
            self.assertEqual(
                security["financials"]["latest_statement"]["values"][
                    "net_profit_parent"
                ],
                999,
            )

    def test_unknown_missing_and_confirmed_empty_events_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = source_and_frames()

            def securities():
                artifacts = build_research_artifacts(root, *args)
                return {
                    row["thscode"]: row
                    for name, value in artifacts.items()
                    if name != "research_inputs_latest.json"
                    for row in value["securities"]
                }

            self.assertEqual(
                securities()["600001.SH"]["availability"]["events"], "not_ready"
            )
            incoming = root / "input.json"
            atomic_write_json(incoming, batch([], module="events"))
            import_research_batch(root, incoming)
            rows = securities()
            self.assertEqual(
                rows["600001.SH"]["availability"]["events"],
                "confirmed_no_events_in_window",
            )
            self.assertEqual(rows["000001.SZ"]["availability"]["events"], "unknown")

    def test_event_response_uses_first_full_session_not_same_day_close(self):
        event = record(module="events", known="2026-08-27T07:00:01+00:00")
        _, _, history, _ = source_and_frames()
        response = event_price_response(event, history, ["2026-08-27", "2026-08-28"])
        self.assertEqual(response["first_full_session"], "2026-08-28")
        self.assertAlmostEqual(response["return_pct"], 2)
        self.assertEqual(event["status"], "announced")

    def test_all_reference_securities_are_queryable_and_shards_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            args = source_and_frames()
            first = build_research_artifacts(Path(directory), *args)
            args[3].sort_values("thscode", ascending=False, inplace=True)
            second = build_research_artifacts(Path(directory), *args)
            self.assertEqual(first, second)
            self.assertEqual(first["research_inputs_latest.json"]["universe_count"], 2)
            for payload in first.values():
                self.assertLess(
                    len(serialize_json(payload, compact=True).encode("utf-8")),
                    300 * 1024,
                )

    def test_conflicting_same_time_revision_is_not_silently_selected(self):
        with self.assertRaisesRegex(ArtifactContractError, "ambiguous"):
            normalize_research_batch(batch([record(profit=1), record(profit=2)]))
