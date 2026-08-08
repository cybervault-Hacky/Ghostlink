"""Service container failures."""

from __future__ import annotations

from ghostlink.exceptions.base import GhostLinkError


class ServiceNotRegisteredError(GhostLinkError):
    """A service was requested from the container that was never registered."""

    error_title = "Service not registered"
