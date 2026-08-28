from __future__ import annotations

import copy
import unittest

from china_stock_engine.opportunity_radar import (
    DEFAULT_RADAR_CANDIDATE_LIMIT,
    build_opportunity_radar_inputs,
)
from china_stock_engine.storage import ArtifactContractError, serialize_json


TIMING = {
    "collection_started_at": "2026-08-28T09:59:00+00:00",
    "collection_completed_at": "2026-08-28T10:30:00+00:00",
    "configured_decision_cutoff": "2026-08-28T20:15:00+08:00",
    "effective_pit_cutoff": "2026-08-28T10:30:00+00:00",
}


def candidate(
    code: str,
    screens: list[tuple[str, int]],
    *,
    facts: dict | None = None,
) -> dict:
    ranks = {screen: rank for screen, rank in screens}
    return {
        "union_order": 999,
        "thscode": code,
        "security_name": f"Security {code}",
        "triggered_screens": list(reversed([screen for screen, _ in screens])),
        "screen_count": len(screens),
        "best_screen_rank": min(ranks.values()),
        "screen_ranks": ranks,
        "screen_percentiles": {screen: 0.9 for screen in ranks},
        "percentiles": {
            "return_1d_pctile": 0.8,
            "return_3d_pctile": 0.7,
            "return_5d_pctile": 0.6,
            "return_20d_pctile": None,
            "amount_change_pctile": 0.9,
            "turnover_change_pctile": 0.85,
            "close_location_pctile": 0.75,
        },
        "facts": {
            "exchange": "SSE",
            "board": "SSE_MAIN",
            "raw_close": 10.5,
            "change_ratio": 1.0,
            "amount": 100_000_000.0,
            "turnover_ratio": 2.0,
            "raw_return_1d_pct": 1.0,
            "raw_return_3d_pct": 2.0,
            "raw_return_5d_pct": 3.0,
            "raw_return_20d_pct": None,
            "tradability_state": "unknown",
            "is_st": None,
            "is_suspended": None,
            "daily_price_limit_pct": None,
            "limit_up": None,
            "limit_down": None,
            "one_word_limit": None,
            **(facts or {}),
        },
        "contradiction_flags": [],
    }


def source_payload(candidates: list[dict], *, tradability_state: str = "missing") -> dict:
    screen_names = sorted(
        {
            screen
            for item in candidates
            for screen in item["triggered_screens"]
        }
    )
    return {
        "schema_version": 3,
        "trade_date": "2026-08-28",
        "generated_at": TIMING["collection_completed_at"],
        "source_snapshot_sha256": "a" * 64,
        "pit_timing": TIMING,
        "data_mode": {"subjective_view_produced": False},
        "readiness": {"tradability": {"state": tradability_state}},
        "field_coverage": {},
        "market": {},
        "board_summary": [],
        "market_cap_bucket_summary": {},
        "cross_sectional_features": {},
        "deterministic_screens": {
            name: {
                "definition": f"deterministic definition for {name}",
                "metric": "change_ratio",
            }
            for name in screen_names
        },
        "candidate_union": candidates,
        "contradiction_flag_definitions": {},
        "drilldown": {},
    }


class OpportunityRadarTests(unittest.TestCase):
    def test_deterministic_union_order_and_idempotent_bytes(self) -> None:
        rows = [
            candidate("600003.SH", [("highest_amount", 1)]),
            candidate(
                "600002.SH",
                [("highest_amount", 2), ("largest_positive_moves", 1)],
            ),
            candidate(
                "600001.SH",
                [("highest_amount", 2), ("largest_positive_moves", 2)],
            ),
        ]
        first = build_opportunity_radar_inputs(source_payload(rows))
        second = build_opportunity_radar_inputs(
            source_payload(list(reversed(copy.deepcopy(rows))))
        )

        expected = ["600002.SH", "600001.SH", "600003.SH"]
        self.assertEqual(
            [item["thscode"] for item in first["candidate_union"]], expected
        )
        self.assertEqual(
            [item["union_order"] for item in first["candidate_union"]], [1, 2, 3]
        )
        self.assertEqual(
            serialize_json(first, compact=True), serialize_json(second, compact=True)
        )
        self.assertEqual(first["generated_at"], TIMING["collection_completed_at"])
        self.assertEqual(
            first["candidate_union_metadata"]["ordering_policy"],
            [
                "screen_count descending",
                "best_screen_rank ascending",
                "thscode ascending",
            ],
        )
        self.assertNotIn("rank", first["candidate_union"][0])
        self.assertEqual(
            first["candidate_union_metadata"]["limit"],
            DEFAULT_RADAR_CANDIDATE_LIMIT,
        )

    def test_unknown_not_ready_and_confirmed_false_are_distinct(self) -> None:
        empty_facts = candidate("600001.SH", [("highest_amount", 1)])
        not_ready = build_opportunity_radar_inputs(
            source_payload([empty_facts], tradability_state="missing")
        )["candidate_union"][0]["availability"]
        unknown = build_opportunity_radar_inputs(
            source_payload([empty_facts], tradability_state="ready")
        )["candidate_union"][0]["availability"]
        confirmed = build_opportunity_radar_inputs(
            source_payload(
                [
                    candidate(
                        "600001.SH",
                        [("highest_amount", 1)],
                        facts={
                            "tradability_state": "clear",
                            "is_st": False,
                            "is_suspended": True,
                            "daily_price_limit_pct": 10.0,
                            "limit_up": False,
                            "limit_down": False,
                            "one_word_limit": False,
                        },
                    )
                ],
                tradability_state="ready",
            )
        )["candidate_union"][0]

        self.assertEqual(not_ready["is_st"], "not_ready")
        self.assertEqual(unknown["is_st"], "unknown")
        self.assertEqual(confirmed["availability"]["tradability"], "confirmed_clear")
        self.assertEqual(confirmed["availability"]["is_st"], "confirmed_false")
        self.assertEqual(
            confirmed["availability"]["is_suspended"], "confirmed_true"
        )
        self.assertEqual(
            confirmed["availability"]["daily_price_limit_pct"],
            "confirmed_value",
        )
        self.assertEqual(confirmed["facts"]["daily_price_limit_pct"], 10.0)

    def test_hard_limit_fails_instead_of_truncating(self) -> None:
        row = candidate("600001.SH", [("highest_amount", 1)])
        row["security_name"] = "超" * (400 * 1024)
        with self.assertRaisesRegex(ArtifactContractError, "hard limit"):
            build_opportunity_radar_inputs(source_payload([row]))

    def test_generated_at_must_come_from_source_snapshot(self) -> None:
        source = source_payload([candidate("600001.SH", [("highest_amount", 1)])])
        source["generated_at"] = "2026-08-28T11:30:00+00:00"
        with self.assertRaisesRegex(ArtifactContractError, "collection_completed_at"):
            build_opportunity_radar_inputs(source)


if __name__ == "__main__":
    unittest.main()
