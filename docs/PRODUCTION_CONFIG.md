# GhostLink Developer Portal — Production Configuration (Phase 11)

The portal is configured entirely through environment variables. There are
no hardcoded production secrets, database credentials, signing keys, email
credentials, or WebAuthn identifiers.

## Environments

| `APP_ENV` | Behavior |
| --- | --- |
| `development` | Safe defaults; dev email adapter; insecure cookies allowed for local HTTP. |
| `staging` | Requires a real DB and session secret; real email provider. |
| `production` | **Fail closed**: missing required values or development conveniences raise at startup. |

Anything other than these three values is rejected.

## Required / optional variables

| Variable | Dev default | Production | Notes |
| --- | --- | --- | --- |
| `APP_ENV` | `development` | required | one of development/staging/production |
| `DATABASE_URL` | `portal.db` | required | SQLite path (dev) or Postgres URL (prod) |
| `SESSION_SECRET` | dev fallback | **required** | must not equal the dev default |
| `PORTAL_SECURE_COOKIES` | `false` | must be `true` | enforced |
| `EMAIL_PROVIDER` | `dev` | must be real (`smtp`) | enforced |
| `EMAIL_SMTP_HOST` | — | required when smtp | |
| `EMAIL_SMTP_PORT` | `587` | optional | |
| `EMAIL_SMTP_USER` / `_PASSWORD` | — | as required by provider | |
| `EMAIL_FROM` | — | recommended | |
| `WEBAUTHN_RP_ID` | `localhost` | **required** | |
| `WEBAUTHN_ORIGIN` | localhost | **required** | |
| `PORTAL_TRUSTED_PROXY` | `false` | set true behind proxy | |

Optional rate-limit overrides (testing): `RATE_LIMIT_<SCOPE>=<count>:<window>`.

## Fail-closed rules (production)

* No `EMAIL_PROVIDER=dev`.
* `PORTAL_SECURE_COOKIES` must be `true`.
* `SESSION_SECRET` must be set and not the development default.
* `DATABASE_URL`, `WEBAUTHN_RP_ID`, `WEBAUTHN_ORIGIN` required.
* `EMAIL_SMTP_HOST` required when `EMAIL_PROVIDER=smtp`.

No secret value is ever printed or logged at startup. See
[docs/DEPLOYMENT.md](DEPLOYMENT.md) for how these map onto a real deployment.
