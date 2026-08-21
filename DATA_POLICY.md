# Public repository data and credential policy

1. iFinD refresh tokens and access tokens are secrets. They may exist only in a hidden local prompt, a process environment, or an encrypted repository secret.
2. The collector must not persist raw iFinD responses. Local artifacts contain only normalized fields, provenance, quality metrics, and compact derived summaries.
3. A successful login does not prove entitlement to a dataset, field, market, history range, or redistribution right. Every new module requires a narrow live canary.
4. `latest` is the last promoted valid snapshot. A failed or stale attempt is recorded in `last_run_status.json` and must not replace it.
5. This public repository contains code and synthetic test fixtures only. Generated iFinD artifacts are ignored by Git and are never committed or attached to a public release.
6. Private persistence is disabled by default. GitHub Actions may write normalized artifacts to the separate private data repository only when `IFIND_PRIVATE_STORAGE_APPROVED=true` and a repository-scoped `STOCK_DATA_REPO_TOKEN` is configured.
7. Enabling a technical storage gate is not evidence of a license. The account contract and vendor must explicitly permit the intended private-cloud storage and downstream use before the gate is enabled.
8. Adjustment, classification, index-membership, tradability, fundamental, event, valuation, and flow modules require independent entitlement and point-in-time schema canaries. Missing inputs remain missing and must not be imputed as facts.
9. Published outputs are factual datasets, quality metadata, field coverage, and deterministic derived fields only. The engine does not publish security selections, composite scores, market opinions, or performance claims.
10. Credentials exposed in chat, logs, screenshots, or shell history should be rotated.
