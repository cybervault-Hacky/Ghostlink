"""GhostLink — a terminal-only encrypted messenger for Termux and Linux.

Phase 1 (Foundation) delivers the application shell: startup branding,
environment detection, configuration management, logging, storage
abstraction, theming, and the interactive menu system.

Phase 2 (Secure Networking) adds the real relay protocol, WebSocket
transport, connection management with supervised reconnection, heartbeat
and latency measurement, rooms, invites, and live status dashboards.

Phase 3 (Secure Messaging) adds authenticated end-to-end encryption
(fresh session key per conversation), validated message frames, delivery
acknowledgements and read receipts, typing indicators, optional message
history (off / session-only / encrypted-at-rest), and the interactive
terminal chat surface — all in the terminal, nothing leaves it.

Phase 4 (Secure File Transfer) adds chunked, end-to-end encrypted file
transfer over the same secure channel: sealed manifests, per-transfer
HKDF sub-keys, AEAD-sealed chunks with per-chunk acknowledgements and
a sliding send window, receiver-side bitmap resumes across reconnects,
SHA-256 verification before a byte becomes visible, and traversal-proof
downloads into a dedicated GhostLink directory.

Phase 5 (Ephemeral Identity & One-Time Invites) adds local, Ed25519
ephemeral identities (GL-… handles with GLFP-… verification
fingerprints, no accounts, no email or phone) and secure one-time join
invites: gl://join/<token> links minted locally, enforced by the relay
authority — monotonic-clock expiration, atomic single redemption,
creator-only revocation, and binding of each invite to the session it
creates. Peer fingerprints can be compared inside the chat for stronger
authentication.

Phase 6B (Secure Group Lifecycle) adds the group membership foundation:
owner-created groups (gl-group-…) of up to 8 members, group invites on
top of the Phase 5 invite authority, owner-countersigned admissions,
self-signed leaves, owner-signed removals and dissolutions, strictly
monotonic epochs, and verified event-driven roster sync — the relay
authorizes everything and never sees a key.

Phase 6C (Group Messaging + Pairwise-Mesh Encryption) adds end-to-end
group conversations on that foundation: every group message is sealed
separately for each authorized recipient over an identity-bound,
epoch-pinned pairwise link (Phase 3 X25519 + HKDF-SHA256 +
ChaCha20-Poly1305, no new primitives). The relay routes opaque
GROUP_FORWARD envelopes and never sees plaintext, keys, message ids, or
sequence numbers.

Phase 7 (Sender-Key Hardening) upgrades group messaging to O(1) per
message on opt-in ``senderkey-v1`` groups: each sender derives an
epoch-scoped hash-ratchet chain and distributes it over the existing
pairwise mesh; one ChaCha20-Poly1305 seal per message is broadcast to the
whole roster. Sender keys never leave memory except inside the mesh-AEAD
distribution envelope; the relay still sees ciphertext only. Removal,
re-join and reconnects rotate and re-distribute chains; out-of-order
delivery uses a bounded skipped-key cache; replay and cross-context
reuse are rejected.

Phase 8 (Reliability, Security Hardening & Adversarial Validation) hardens
GhostLink against hostile networks, relays, peers, storage, packets, clocks,
and delivery order. It adds a deterministic recovery coordinator (one
authoritative state and exactly-one in-flight resync/install per group), a
bounded GSKREQ abuse brake and sender-key recovery hardening, relay
connection-cap and per-source rate limits, a log-secret redaction backstop,
an extended ``--doctor``, a read-only ``ghostlink security-status``
diagnostic, and comprehensive adversarial / fuzz / crash-consistency tests.

Phase 9 (Production Readiness, Compatibility & Release Engineering) makes
GhostLink release-ready without weakening the security model. It lowers the
declared Python floor to 3.11 (verified green on 3.11.2), adds a formal
config/state schema-versioning layer that fails closed on documents from a
newer GhostLink, adds release tooling (``scripts/release_check.sh`` and
``scripts/scan_secrets.py``), documents backup/recovery guidance and an
explicit compatibility matrix, and expands the test suite with
compatibility, migration, package-content, protocol-downgrade and
CLI-reliability coverage.

Phase 12 (Developer API Platform & Termux Integration) adds a narrowly
scoped, authenticated, auditable, revocable developer API at
``/api/v1/developer/*`` plus a Termux client (``ghostlink developer
login/device/project/credential/security-status/doctor``). It introduces
least-privilege scopes, cryptographically random device identity, a
short-lived pairing flow, rotating bearer access/refresh tokens, server-side
project binding, and persistent credential/device revocation — with the
explicit guarantee that exactly one Owner exists and developer accounts can
never become Owner or transfer ownership.

Phase 11 (Production Portal Hardening & Launch Readiness) hardens the
Phase 10B Developer Portal into a launch-ready architecture: an
environment-driven production configuration layer (fail-closed in
production), a versioned database migration system with indexes, session
hardening (idle/absolute lifetime, password-change session invalidation),
WebAuthn hardening (single-use/expiring challenges, origin validation),
an email-provider abstraction (dev + SMTP), deterministic security
audit tooling (scripts/security_check.sh), metadata-only operational
logging, and expanded E2E/failure-recovery tests. No telemetry, no
payments, no new crypto.

Phase 10B (Developer Portal) adds a secure web application for GhostLink
developers: remote developer accounts, developer credentials with a full
lifecycle (create / rotate / revoke / verify), projects, sessions,
security activity, and settings. It is a real, tested, security-focused
portal: a dependency-light Python WSGI backend (stdlib + cryptography,
SQLite with a PostgreSQL-ready schema) and a React + TypeScript + Vite
frontend. Passwords are PBKDF2-hashed; developer secrets, recovery codes
and session/token values are stored only as salted verifiers; sessions are
revocable, requests are CSRF-protected and rate-limited, and audit logs are
metadata-only. No telemetry, no secret upload, no biometric data.

Phase 10A (Developer Account & Credential Infrastructure) adds a local,
production-grade Developer Account system: a developer identity
(``dev_…``) and cryptographically random API credentials (``gl_dev_…``)
that a future developer portal (Phase 10B) can authenticate against —
without ever exposing the local secret. Keys are generated with the
standard-library CSPRNG (256-bit secrets), never derived from predictable
identifiers, stored only as salted HKDF-SHA256 verification material
(never plaintext), versioned and fail-closed, with atomic storage, strict
permissions, rotation, revocation, rate-limited local verification, a
network-free boundary, and doctor/security-status integration. This module
makes **zero network requests** and uploads nothing.
"""

from __future__ import annotations

__version__ = "0.17.0"
__all__ = ["__version__", "version_info"]

_major, _minor, _patch = (int(part) for part in __version__.split("."))
version_info: tuple[int, int, int] = (_major, _minor, _patch)
