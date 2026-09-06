"""Read-only truncation comparison against frozen Git artifacts; no API calls."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from china_stock_engine.screen_selection import (
    SELECTION_POLICY_VERSION,
    selection_order,
)


def audit(baseline, dates):
    # Resolve once to a local immutable commit, never execute a user-built shell.
    commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", baseline + "^{commit}"], cwd=REPO, text=True
    ).strip()
    results = []
    for day in dates:

        def read(name):
            return json.loads(
                subprocess.check_output(
                    ["git", "show", f"{commit}:data/snapshots/{day}/{name}"], cwd=REPO
                )
            )

        screens = read("opportunity_inputs_latest.json")["deterministic_screens"]
        old = read("opportunity_radar_latest.json")["candidate_union"]
        facts = {
            row["thscode"]: row for screen in screens.values() for row in screen["rows"]
        }

        def counts(codes):
            return {
                "moves_ge_9_5pct": sum(
                    facts[code]["change_ratio"] >= 9.5
                    for code in codes
                    if facts[code].get("change_ratio") is not None
                ),
                "highest_amount_captured": sum(
                    row["thscode"] in codes for row in screens["highest_amount"]["rows"]
                ),
                "nonempty_screens_unrepresented": sum(
                    bool(screen["rows"])
                    and not any(row["thscode"] in codes for row in screen["rows"])
                    for screen in screens.values()
                ),
            }

        results.append(
            {
                "trade_date": day,
                "full_captured_union": len(facts),
                "before": counts({row["thscode"] for row in old}),
                "after": counts(set(selection_order(screens)[:100])),
            }
        )
    return {
        "baseline_commit": commit,
        "selection_policy_version": SELECTION_POLICY_VERSION,
        "method": "frozen screen rows; representation only, no performance evaluation",
        "dates": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline", default="a1ccb330e5f6f00b8199e989a06565c2a1c1d11b"
    )
    parser.add_argument(
        "--dates",
        nargs="+",
        default=[
            "2026-08-28",
            "2026-08-31",
            "2026-09-01",
            "2026-09-02",
            "2026-09-03",
            "2026-09-04",
        ],
    )
    arguments = parser.parse_args()
    print(json.dumps(audit(arguments.baseline, arguments.dates), indent=2))
