"""GhostLink ↔ Developer Portal integration (Phase 12).

A narrowly scoped local client that lets a Termux developer installation
authenticate to the GhostLink Developer Portal via short-lived bearer
tokens, register/revoke devices, list projects, and manage scoped
credentials — without exposing permanent secrets unnecessarily.

The local token store persists only short-lived access tokens and rotating
refresh tokens (at 0600 with restrictive permissions). Permanent developer
credentials are shown once at pairing/rotation and are never stored by this
module. This module makes HTTP requests only to the configured portal URL
and only on explicit developer commands — no telemetry, no background sync.
"""
