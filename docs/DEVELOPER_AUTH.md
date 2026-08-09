# GhostLink Developer Authentication (Phase 12)

## Model

Developers authenticate to the portal API with short-lived bearer access
tokens and rotating refresh tokens. Permanent credentials are used only once
at token issuance and are never stored by the Termux CLI.

## Ownership

GhostLink has **exactly one Owner**. Developer accounts are never Owners and
can never:

* become or promote themselves to Owner,
* transfer ownership,
* create another Owner,
* delete or modify the Owner identity,
* bypass Owner security controls.

There is no multi-owner functionality, no ownership transfer, and no
"co-owner" role. No developer API scope or endpoint grants Owner privileges.

## Status

IMPLEMENTED and TESTED (owner-scope absence + escalation rejection).
