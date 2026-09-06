# Live supplemental facts

The `Supplemental iFinD Facts` workflow validates three explicit securities by
default. It is independent of the market collector: it never downloads the
universe, raw daily prices, or PDFs. `--scope radar` explicitly widens the scope
to the existing deterministic 100-security union, not an investment ranking.

```powershell
python -m china_stock_engine.ifind_supplemental --scope canary
```

Credentials must be process-scoped `IFIND_REFRESH_TOKEN` or Actions Secrets.
The adapter performs at most 12 authenticated data requests and disables
automatic transport retries. The attempt status records endpoint, success,
and provider-reported data volume only, never the vendor payload or credentials.
Normalized successful checkpoints survive a different module's failure; exact
reruns verify and reuse their hashes without new data requests. Corrupt
checkpoints fail closed. The supplemental pointer moves only on success.

## Initial contracts

- Financials: `ths_np_atoopc_pit_stock(as_of_date, report_period, 1)` and
  `ths_regular_report_actual_dd_stock(report_period)`. Only consolidated YTD
  parent net profit is enabled initially; other financial metrics remain null.
  Date-only publication is conservatively placed at 23:59:59 China time.
  First-seen/known time is actual collection completion, never the historical
  report date. `source_url` currently identifies the provider's query
  documentation, **not** an original statement PDF. Document hash stays null.
  Reuse is per issuer/report period for seven days unless a newer published
  announcement invalidates it; scope growth queries only missing issuers.
- Announcements: `report_query`, no category filter. Preserve the provider
  identity, publication timestamp, title and PDF URL; event category is the
  generic `filing` and status stays `unknown`. A title is not proof that a
  transaction completed. No PDF content is downloaded. An empty response is
  **not** proof of no announcements; complete coverage is not asserted.
  Signed/authenticated PDF URLs are omitted entirely, not stored with tokens or
  presented as working links after removing a signature. `source_url_kind` and
  `document_access` distinguish the provider query reference from a document.
- Adjustments: daily `CPS=2` (dividend reinvestment), explicit base date; adjusted
  closes are joined against cached raw closes over at most 21 stored sessions.
  Factors are derived as adjusted/raw close. Immutable vintages are partitioned
  by **actual collection date**, not the historical price date. These prices
  do not constitute a separately collected corporate-action ledger.

Successful normalized records live in `facts/research/{financials,events}/` and
`facts/adjustment/as_of_date=.../vintage=.../`. The small
`latest/supplemental_manifest.json` points to verified facts; the independent
`supplemental_last_attempt.json` reports the most recent attempt. Existing
historical close-time snapshots must not be retroactively fed today's knowledge.

Setting repository variable `IFIND_SUPPLEMENTAL_SCOPE=radar` enables the bounded
daily step after market validation and before derived report publication. The
scope remains at most 100 securities, not all-market entitlement or coverage.
The normal workflow never renews credentials. The manual workflow offers an
explicit `renew_access_token` input, off by default; it performs one
`update_access_token` call with transport retries disabled. This invalidates
older access tokens but leaves the refresh token unchanged. Error -1303 means
the access token reached its IP binding limit (official HTTP manual, error table).

Official references:

- [Financial PIT and HTTP examples](https://quantapi.51ifind.com/gwstatic/static/ds_web/quantapi-web/example.html)
- [Daily CPS parameters and announcement output fields](https://quantapi.51ifind.com/gwstatic/static/ds_web/quantapi-web/help-center/manual.html)
