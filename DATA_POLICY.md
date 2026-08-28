# Public repository data and credential policy

1. iFinD refresh tokens and access tokens are secrets. They may exist only in a hidden local prompt, a process environment, or an encrypted repository secret.
2. The collector must not persist raw iFinD responses. Local artifacts contain only normalized fields, provenance, quality metrics, and compact derived summaries.
3. A successful login does not prove entitlement to a dataset, field, market, history range, or redistribution right. Every new module requires a narrow live canary.
4. `latest` is the last promoted valid snapshot. A failed or stale attempt is recorded in `last_run_status.json` and must not replace it.
5. This repository retains normalized generated artifacts in `data/` for downstream automation. Raw iFinD responses, credentials, and unnormalized vendor payloads are never committed.
6. GitHub Actions writes verified `data/` artifacts back to this repository. It verifies push access and the repository capacity gate before any iFinD request; a failed preflight must not consume provider quota.
7. Committing data to a public repository is not evidence of redistribution rights. The account contract and vendor must explicitly permit the intended publication and downstream use before data is collected.
8. Adjustment, classification, index-membership, tradability, fundamental, event, valuation, and flow modules require independent entitlement and point-in-time schema canaries. Missing inputs remain missing and must not be imputed as facts.
9. Published outputs are factual datasets, quality metadata, field coverage, deterministic derived fields, and explicitly defined deterministic ranking screens only. The engine does not publish subjective security selections, composite scores, market opinions, trade advice, or performance claims.
10. Credentials exposed in chat, logs, screenshots, or shell history should be rotated.
