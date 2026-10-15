from __future__ import annotations

from collections.abc import Mapping


class StatsCollector:
    def __init__(self) -> None:
        self._stats: dict[str, object] = {}

    def get_value(self, key: str, default: object = None, spider: object | None = None) -> object:
        return self._stats.get(key, default)

    def set_value(self, key: str, value: object, spider: object | None = None) -> None:
        self._stats[key] = value

    def inc_value(self, key: str, count: int | float = 1, spider: object | None = None) -> None:
        current = self._stats.get(key, 0)
        if not isinstance(current, (int, float)):
            raise TypeError(f"stat {key!r} is not numeric")
        self._stats[key] = current + count

    def max_value(self, key: str, value: int | float, spider: object | None = None) -> None:
        current = self._stats.get(key)
        if current is None or value > current:
            self._stats[key] = value

    def min_value(self, key: str, value: int | float, spider: object | None = None) -> None:
        current = self._stats.get(key)
        if current is None or value < current:
            self._stats[key] = value

    def get_stats(self, spider: object | None = None) -> Mapping[str, object]:
        return dict(self._stats)

    def clear_stats(self, spider: object | None = None) -> None:
        self._stats.clear()
