import copy
import unittest

from china_stock_engine.screen_selection import selection_order, selection_diagnostics
from china_stock_engine.storage import ArtifactContractError


def screen(codes, definition):
    return {
        "metric": "change_ratio",
        "definition": definition,
        "eligible_count": len(codes),
        "rows": [
            {
                "thscode": code,
                "trigger": {"rank": i},
                "board": "TEST",
                "change_ratio": 1.0,
            }
            for i, code in enumerate(codes, 1)
        ],
    }


class ScreenSelectionTests(unittest.TestCase):
    def test_equivalent_rule_cannot_gain_a_second_family_slot(self):
        duplicate = screen(["A", "B"], "identical definition")
        with self.assertRaisesRegex(ArtifactContractError, "multiple families"):
            selection_order(
                {
                    "highest_amount": duplicate,
                    "largest_positive_moves": copy.deepcopy(duplicate),
                }
            )

    def test_families_interleave_not_trigger_count(self):
        screens = {
            "largest_positive_moves": screen(["A", "B", "C"], "positive"),
            "highest_amount": screen(["D", "A", "B"], "amount"),
            "weak_5d_positive_1d_reversal": screen(["E", "A", "B"], "reversal"),
        }
        self.assertEqual(selection_order(screens)[:3], ["A", "D", "E"])
        self.assertEqual(
            selection_order(dict(reversed(list(screens.items())))),
            selection_order(screens),
        )

    def test_alias_has_no_extra_capacity_and_conflicting_alias_fails(self):
        screens = {
            "highest_amount": screen(["A", "B"], "same rule"),
            "turnover_expansion": screen(["C", "D"], "another rule"),
        }
        original = selection_order(screens)
        screens["amount_expansion"] = copy.deepcopy(screens["highest_amount"])
        self.assertEqual(original, selection_order(screens))
        screens["amount_expansion"]["rows"].reverse()
        self.assertEqual(original, selection_order(screens))
        screens["amount_expansion"]["rows"][0]["thscode"] = "X"
        with self.assertRaises(ArtifactContractError):
            selection_order(screens)

    def test_direct100_is_prefix150_and_diagnostics_include_excluded_rules(self):
        codes = [f"{i:06}.SH" for i in range(200)]
        screens = {
            "largest_positive_moves": screen(codes[:100], "up"),
            "highest_amount": screen(codes[100:], "amount"),
            "weak_close_location": screen([], "close"),
        }
        order = selection_order(screens)
        self.assertEqual(order[:100], order[:150][:100])
        diagnostics = selection_diagnostics(screens, order[:100])
        self.assertEqual(diagnostics["before"]["count"], 200)
        self.assertEqual(diagnostics["after"]["count"], 100)
        self.assertEqual(diagnostics["screens"]["highest_amount"]["selected"], 50)
        self.assertIn("weak_close_location", diagnostics["screens"])
