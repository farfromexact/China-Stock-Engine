"""Versioned, prefix-stable sampling of factual screen results (not a score)."""

from __future__ import annotations

from collections import Counter, deque
from typing import Any

from .storage import ArtifactContractError, json_sha256

SELECTION_POLICY_VERSION = "family_round_robin_v1"
ORDERING_POLICY = [
    "evidence family ascending, one unseen security per family per round",
    "canonical rule hash ascending, one unseen security per rule per round",
    "within rule: screen rank ascending, thscode ascending",
    "identical rule aliases and identical ordered queues receive no extra slot",
    "global thscode deduplication; take a prefix, never sort by screen_count",
]


def screen_family(screen: str) -> str:
    if screen.startswith("board_neutral_absolute_move__"):
        return "board_neutral_move"
    if screen.startswith("market_cap_neutral_absolute_move__"):
        return "market_cap_neutral_move"
    if screen in {"amount_expansion", "turnover_expansion", "highest_amount"}:
        return "liquidity_activity"
    if screen.startswith(("gap_", "large_intraday_range_")):
        return "gap_intraday_structure"
    if screen.startswith(("large_positive_", "large_negative_")):
        return "return_close_location"
    if screen in {"largest_positive_moves", "largest_negative_moves"}:
        return "directional_price_move"
    if screen in {
        "momentum_acceleration_1d_vs_3d_5d",
        "positive_momentum_1d_3d_5d",
        "strong_5d_negative_1d_pullback",
        "weak_5d_positive_1d_reversal",
    }:
        return "multi_horizon_momentum"
    if screen.startswith(("price_up_activity_", "price_down_activity_")):
        return "price_activity_confirmation"
    if screen.startswith("relative_strength_"):
        return "multi_horizon_relative_strength"
    if screen in {"strong_close_location", "weak_close_location"}:
        return "close_location"
    raise ArtifactContractError(f"unmapped screen family: {screen}")


def _interleave(queues: list[list[str]]) -> list[str]:
    active = [deque(queue) for queue in queues]
    seen: set[str] = set()
    output: list[str] = []
    while any(active):
        for queue in active:
            while queue and queue[0] in seen:
                queue.popleft()
            if queue:
                code = queue.popleft()
                seen.add(code)
                output.append(code)
    return output


def selection_order(screens: dict[str, dict[str, Any]]) -> list[str]:
    """Select from ALL screen rows. The 100-prefix equals the 150-prefix's 100."""
    families: dict[str, dict[str, list[str]]] = {}
    rule_families: dict[str, str] = {}
    for name, screen in sorted(screens.items()):
        family = screen_family(name)
        rows = screen.get("rows") or []
        ranked = sorted(
            rows, key=lambda row: (int(row["trigger"]["rank"]), row["thscode"])
        )
        codes = list(dict.fromkeys(str(row["thscode"]) for row in ranked))
        rule = json_sha256(
            {key: screen.get(key) for key in ("metric", "definition", "scope")}
        )
        if rule in rule_families and rule_families[rule] != family:
            raise ArtifactContractError(
                "equivalent rule aliases cannot occupy multiple families"
            )
        rule_families[rule] = family
        slots = families.setdefault(family, {})
        if rule in slots and slots[rule] != codes:
            raise ArtifactContractError(f"equivalent rule aliases disagree: {name}")
        slots[rule] = codes
    family_queues = []
    for family in sorted(families):
        distinct: set[tuple[str, ...]] = set()
        queues = []
        for _, codes in sorted(families[family].items()):
            signature = tuple(codes)
            if signature not in distinct:
                queues.append(codes)
                distinct.add(signature)
        family_queues.append(_interleave(queues))
    return _interleave(family_queues)


def selection_diagnostics(
    screens: dict[str, dict[str, Any]], selected: list[str]
) -> dict:
    selected_set = set(selected)
    facts = {
        row["thscode"]: row
        for screen in screens.values()
        for row in screen.get("rows") or []
    }

    def distribution(codes: set[str]) -> dict:
        rows = [facts[code] for code in sorted(codes)]
        returns = sorted(
            float(row["change_ratio"])
            for row in rows
            if row.get("change_ratio") is not None
        )
        return {
            "count": len(rows),
            "boards": dict(
                sorted(
                    Counter(str(row.get("board") or "unknown") for row in rows).items()
                )
            ),
            "market_cap_buckets": dict(
                sorted(
                    Counter(
                        str(row.get("market_cap_bucket") or "unknown") for row in rows
                    ).items()
                )
            ),
            "return_1d": {
                "observed": len(returns),
                "positive": sum(x > 0 for x in returns),
                "negative": sum(x < 0 for x in returns),
                "ge_9_5pct": sum(x >= 9.5 for x in returns),
                "le_minus_9_5pct": sum(x <= -9.5 for x in returns),
            },
        }

    return {
        "selection_policy_version": SELECTION_POLICY_VERSION,
        "before": distribution(set(facts)),
        "after": distribution(selected_set),
        "screens": {
            name: {
                "state": screen.get("state"),
                "eligible": screen.get("eligible_count"),
                "captured": len(screen.get("rows") or []),
                "selected": sum(
                    row["thscode"] in selected_set for row in screen.get("rows") or []
                ),
                "dropped": sum(
                    row["thscode"] not in selected_set
                    for row in screen.get("rows") or []
                ),
            }
            for name, screen in sorted(screens.items())
        },
    }
