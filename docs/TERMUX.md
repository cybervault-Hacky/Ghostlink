# Termux (Phase 14)

## Status

**IMPLEMENTED & TESTED** for the local store and client behavior (SQLite-backed
local state, real local HTTP error handling). Runtime network calls against a
live portal are exercised by the portal backend E2E; a live public portal is
not available in this environment.

## Developer CLI

```
ghostlink developer init
ghostlink developer login
ghostlink developer logout
ghostlink developer whoami
ghostlink developer doctor
ghostlink developer device register
ghostlink developer device list
ghostlink developer device revoke
ghostlink developer project list
ghostlink developer project use
ghostlink developer credential status
ghostlink developer security-status
```

## Reliability

The client (`ghostlink/developer_portal/client.py`) behaves as follows:

- **Network failure** (unreachable host, DNS, timeout) → `PortalClientError`
  with `code="network"` and a clear message. It is **never** treated as
  successful authentication.
- **HTTP 401** → `code="unauthorized"`; **403** → forbidden; **429** →
  `code="rate_limited"`; **500** → `code="internal"`; **503** →
  `code="rate_limit_store_unavailable"` (or the server-provided code).
- **Malformed server response** (2xx that is not valid JSON) → fails closed
  with `code="invalid_response"` (never treated as success).
- Tokens are sent only in the `Authorization: Bearer` header — never in URLs.
- Error messages never contain tokens/credentials.

The permanent credential is shown once at pairing/issuance; the CLI never
prints it by default thereafter. The local token store is `0600`/`0700`,
atomic, symlink-refusing, schema-versioned, and corruption-detected.

## Production-only functionality

Commands such as `ghostlink db status|migrate|verify`,
`ghostlink backup create|verify|list`, and `ghostlink system health|readiness`
require the portal backend on the same host and a configured `DATABASE_URL`.
On a Termux device (no portal backend) they return a clear deterministic error
(`exit 3`) rather than failing silently.
