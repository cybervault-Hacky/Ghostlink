"""Abstract storage backend contract.

A backend behaves like a persistent ``MutableMapping[str, Any]`` of
JSON-compatible values. Concrete mapping behaviour is implemented here in
terms of two primitives — :meth:`_read_all` and :meth:`_write_all` — so new
backends only implement bulk read/write semantics.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, MutableMapping
from typing import Any


class StorageBackend(MutableMapping[str, Any], ABC):
    """Mapping-style persistent key/value storage."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable backend identifier used in logs and errors."""

    @abstractmethod
    def _read_all(self) -> dict[str, Any]:
        """Return the full document. A missing document means ``{}``."""

    @abstractmethod
    def _write_all(self, data: dict[str, Any]) -> None:
        """Persist the full document durably and atomically."""

    # ------------------------------------------------------- mapping protocol

    def replace(self, document: dict[str, Any]) -> None:
        """Atomically replace the entire document in a single write.

        Unlike mutating the mapping key-by-key (which writes on every
        assignment), this persists ``document`` in one atomic write — the
        correct primitive for stores that must never expose a half-written
        intermediate state (e.g. the developer credential store).
        """
        self._write_all(document)

    def __getitem__(self, key: str) -> Any:
        return self._read_all()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        data = self._read_all()
        data[key] = value
        self._write_all(data)

    def __delitem__(self, key: str) -> None:
        data = self._read_all()
        del data[key]  # raises KeyError for missing keys, per Mapping contract
        self._write_all(data)

    def __iter__(self) -> Iterator[str]:
        return iter(self._read_all())

    def __len__(self) -> int:
        return len(self._read_all())
