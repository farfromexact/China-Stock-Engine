# Candidate-union selection audit — 2026-09-06

Baseline: Git commit `a1ccb330e5f6f00b8199e989a06565c2a1c1d11b`.
New policy: `family_round_robin_v1`, implemented in `screen_selection.py`.

The comparison freezes each baseline day's screen rows, so only the truncation
policy changes. It does not mix changes to missing-history handling into the
selection comparison. No iFinD requests and no return/PnL backtest were performed.

| Trade date | Full captured union | Old / new ≥9.5% moves in 100 | Old / new highest-amount screen coverage | Old / new nonempty screens with no selected security |
| --- | ---: | ---: | ---: | ---: |
| 2026-08-28 | 471 | 59 / 35 | 2 / 5 | 4 / 0 |
| 2026-08-31 | 492 | 59 / 38 | 1 / 5 | 1 / 0 |
| 2026-09-01 | 469 | 62 / 35 | 3 / 6 | 1 / 0 |
| 2026-09-02 | 484 | 53 / 30 | 2 / 5 | 1 / 0 |
| 2026-09-03 | 468 | 46 / 27 | 2 / 5 | 1 / 0 |
| 2026-09-04 | 447 | 51 / 32 | 4 / 10 | 1 / 0 |

The policy changes representation, not expected profitability. Equal family
rotation is an explicit transport sampling convention, not an optimal investment
allocation. Overlap can still affect representation; there is no guaranteed
minimum per screen for every possible dataset. All families here remain TAPE.

Reproduce locally with `python scripts/audit_selection.py` (the baseline commit
must exist locally; no network or vendor API is used). At the baseline commit, read each snapshot's
`opportunity_inputs_latest.json`, pass `deterministic_screens` to
`selection_order`, and use its first 100 codes. Compare with the baseline radar's
100 codes. Count membership in `highest_amount.rows`, moves using the captured
rows' `change_ratio`, and empty intersections with every nonempty screen.

Only 2026-09-04 derived artifacts are rebuilt for this release. Earlier snapshot
files are untouched; the previous latest remains available at the baseline Git
commit. This table is not a claim of alpha or a recommendation of any security.
