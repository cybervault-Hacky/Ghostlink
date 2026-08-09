# Rate limiting (Phase 13F)

## Status

**IMPLEMENTED & TESTED.** A `RateLimiter` interface with two backends:

- **`InMemoryRateLimiter`** — sliding-window, per-process. Default for local
  development / single-process deployments.
- **`PostgresRateLimiter`** — fixed-window counters in PostgreSQL using atomic
  `INSERT … ON CONFLICT … RETURNING`, correct across many workers/processes.
  Selected with `RATE_LIMIT_BACKEND=postgresql`.

## Fail closed

- In **production**, requesting the in-memory limiter while `DATABASE_URL` is
  PostgreSQL is **rejected at configuration load** (a multi-process deployment
  must not silently degrade to per-process limits).
- If the PostgreSQL limiter cannot reach its backing store it raises
  `RateLimitBackendError`, which the app converts to `503` — a store outage
  never silently widens the rate limit.

## Protected surfaces

`sign_in`, `sign_up`, `password_reset_request`, `email_verify`, `mfa_verify`,
`credential_create`, `credential_rotate`, `credential_revoke`,
`credential_verify`, `device_register`, `session_action`, `webauthn_register`,
`pair_begin`, `pair_approve`, `token_issue`, `token_refresh`.

## Enumeration resistance

Password-reset returns a generic response regardless of whether the email
exists; rate-limit behaviour does not reveal email/account existence.

## Distributed correctness

Counters are stored keyed by `scope:key` with a window start; the atomic
upsert increments (or resets) the counter and returns the new count in one
statement, so concurrent workers agree on the limit without a shared Python
lock.
