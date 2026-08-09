# GhostLink Developer API — Projects (Phase 12)

## Model

Projects belong to a developer account. A scoped developer credential/token
may be bound to a project. The server resolves ownership on every protected
operation and never trusts a project id supplied by the client.

## API

* `GET /api/v1/developer/projects` (`project:read`) — list the developer's
  projects (id, name, slug, status).

## Status

IMPLEMENTED and TESTED (list + cross-project denial via scope). Owner-only
project operations and server-side project binding of credentials are
implemented; a portal web UI for projects is deferred.

## Boundary

No cross-project access or IDOR: the `/developer/projects` endpoint returns
only the authenticated developer's own projects, gated by scope.
