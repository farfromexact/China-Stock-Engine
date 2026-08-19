# Data and credential policy

1. iFinD refresh tokens and access tokens are secrets. They may exist only in a hidden local prompt, a process environment, or an encrypted repository secret.
2. The collector must not persist raw iFinD responses. Formal artifacts contain only normalized fields, provenance, quality metrics, and compact derived summaries.
3. A successful login does not prove entitlement to a dataset, field, market, history range, or redistribution right. Every new module requires a narrow live canary.
4. `latest` is the last promoted valid snapshot. A failed or stale attempt is recorded in `last_run_status.json` and must not replace it.
5. Keep the repository private until the account contract and vendor terms explicitly permit the intended redistribution. Removing credentials is not sufficient to make commercial data publishable.
6. Credentials exposed in chat, logs, screenshots, or shell history should be rotated.

