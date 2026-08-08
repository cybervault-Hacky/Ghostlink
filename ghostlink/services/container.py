"""A small, explicit service container.

The container is the seam where Phase 2 plugs in transports, cryptography,
and room services: they register implementations once during bootstrap and
consumers request them by type or by name — never by importing globals.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, overload

from ghostlink.exceptions.services import ServiceNotRegisteredError

T = TypeVar("T")


class ServiceContainer:
    """Registry of application services with lazy factory support."""

    def __init__(self) -> None:
        self._instances: dict[Any, Any] = {}
        self._factories: dict[Any, Callable[[], Any]] = {}

    def register(self, key: Any, instance: Any) -> None:
        """Register an already-constructed ``instance`` under ``key``."""

        self._instances[key] = instance
        self._factories.pop(key, None)

    def register_factory(self, key: Any, factory: Callable[[], Any]) -> None:
        """Register a lazy ``factory``; the result is cached on first access."""

        self._factories[key] = factory
        self._instances.pop(key, None)

    @overload
    def get(self, key: type[T]) -> T: ...

    @overload
    def get(self, key: Any) -> Any: ...

    def get(self, key: Any) -> Any:
        """Resolve ``key`` to its service, raising if it was never registered."""

        instance = self._instances.get(key)
        if instance is not None:
            return instance
        factory = self._factories.get(key)
        if factory is None:
            known = ", ".join(self._describe(k) for k in self.keys()) or "(none)"
            raise ServiceNotRegisteredError(
                f"Service '{self._describe(key)}' is not registered.",
                hint=f"Registered services: {known}.",
            )
        instance = factory()
        self._instances[key] = instance
        return instance

    def has(self, key: Any) -> bool:
        return key in self._instances or key in self._factories

    def keys(self) -> tuple[Any, ...]:
        return tuple(self._instances) + tuple(self._factories)

    def clear(self) -> None:
        self._instances.clear()
        self._factories.clear()

    @staticmethod
    def _describe(key: Any) -> str:
        return getattr(key, "__name__", None) or str(key)
