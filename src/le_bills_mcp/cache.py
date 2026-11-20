"""Tiny in-memory TTL cache."""

from __future__ import annotations

import time
from typing import Any


class TTLCache:
    def __init__(self, max_entries: int = 512) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._max = max_entries

    def get(self, key: str) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        expires, value = hit
        if expires < time.monotonic():
            del self._data[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if len(self._data) >= self._max:
            # Drop the entry closest to expiry.
            del self._data[min(self._data, key=lambda k: self._data[k][0])]
        self._data[key] = (time.monotonic() + ttl, value)

    def clear(self) -> None:
        self._data.clear()
