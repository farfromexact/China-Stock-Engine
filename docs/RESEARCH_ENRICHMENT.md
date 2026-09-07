# Research enrichment: first and second batches

The engine produces facts and deterministic features, not security opinions,
opportunity scores or recommendations. Public redistribution of the additional
normalized data was explicitly confirmed by the repository owner on 2026-09-07.
Credentials remain only in the existing Actions Secret or process environment.

## Collection

`python -m china_stock_engine.ifind_enrichment --canary` verifies at most three
securities. `--modules` can restrict verification to one module. A normal run
uses the current deterministic radar union (at most 100 securities) and requires
a successful canary for the exact versioned provider mapping. It never rotates
credentials or retries data requests automatically.

Mappings live in `config/ifind_enrichment.json`: only documented provider
identifiers, explicit units, parameters and boolean enumerations are accepted.
Null profiles are `not_configured`, not `not_entitled`; empty/null responses
cannot confirm permission or absence. A matching successful canary is necessary
but not a promise of future entitlement or all-market coverage.

Financial history targets eight report periods independently of the 20-session
market window. Per-issuer/period caching lasts at most seven days; later published
filings invalidate that issuer. Scope growth only queries missing issuers.
Benchmark queries use only missing dates from existing market-session partitions.
Request receipts contain normalized artifact references, never raw responses.

The manual `Research Data Enrichment` workflow supports bounded canaries.
`IFIND_ENRICHMENT_ENABLED=true` enables the canary-approved daily step before
report building. No new schedule or credential is created. A failed query
preserves the last valid pointer and retains successful immutable checkpoints.
`enrichment_last_attempt.json` records current failures independently.

## Facts and consumers

- `latest/enrichment_manifest.json`: mapping SHA, exact scope, observation time,
  artifact SHA, configured/missing states and field coverage.
- V2 `facts/research/financials`: adds operating costs, receivables, inventory,
  total assets/liabilities. Legacy v1 normalization/content hashes stay unchanged.
- V2 `facts/research/forecasts`: fixed fiscal year, institution, estimate basis,
  contributor count, publication and known times. Compare only matched identities
  and years at 5D/20D cutoffs. Consensus is not individual-institution revisions.
- V2 `facts/research/events`: announced/executed amounts, shares, earnings preview
  intervals and cash dividends. Titles do not establish execution or amounts.
- Industry, trading rules and benchmark prices: immutable observation-date
  vintages subject to knowledge cutoffs. Industry observations establish a dated
  membership point, not fabricated historical membership.
- Existing research shards expose financial quality, PS/PE/PB and forecasts.
  Files retain the 300 KiB limit and use source times, not build wall clocks.

Quality fields include quarter YoY, gross margin, cashflow/profit, debt ratios,
receivable/inventory YoY. Stock values are not subtracted like YTD flows.
These are observations, not investment judgements; bank/insurer-specific
interpretation is not implemented. Peer valuation percentiles require complete
PIT classification and observed positive denominators for every peer (minimum 5).
Missing peers remain `not_ready`. Long-history valuation percentiles and dividend
yield remain unavailable until their own complete source inputs are collected.

## Live coverage is separate from implemented contracts

Initial documented mappings cover parent profit/total assets/disclosure dates
and CSI300/CSI1000 closes. Full core-financial, SW industry, tradability,
structured-event and forecast mappings remain disabled until exact catalog
definitions and narrow live canaries are verified. Schemas and incremental
adapters alone are not evidence that those datasets are populated.
