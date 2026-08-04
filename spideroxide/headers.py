from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, MutableMapping

HeaderValue = str | bytes
HeaderValues = HeaderValue | Iterable[HeaderValue]


def _value_bytes(value: HeaderValue) -> bytes:
    return value if isinstance(value, bytes) else value.encode("latin-1")


class Headers(MutableMapping[str, bytes]):
    """Case-insensitive HTTP headers with support for repeated values."""

    def __init__(self, values: Mapping[str, HeaderValues] | None = None) -> None:
        self._values: dict[str, tuple[str, list[bytes]]] = {}
        if values:
            for name, value in values.items():
                if isinstance(value, (str, bytes)):
                    self.setlist(name, [value])
                else:
                    self.setlist(name, value)

    @staticmethod
    def _key(name: str) -> str:
        if not isinstance(name, str):
            raise TypeError("header names must be strings")
        return name.lower()

    def __getitem__(self, name: str) -> bytes:
        values = self._values[self._key(name)][1]
        return values[-1]

    def __setitem__(self, name: str, value: bytes) -> None:
        self.setlist(name, [value])

    def __delitem__(self, name: str) -> None:
        del self._values[self._key(name)]

    def __iter__(self) -> Iterator[str]:
        return (display_name for display_name, _ in self._values.values())

    def __len__(self) -> int:
        return len(self._values)

    def getlist(self, name: str) -> list[bytes]:
        entry = self._values.get(self._key(name))
        return [] if entry is None else list(entry[1])

    def setlist(self, name: str, values: Iterable[HeaderValue]) -> None:
        encoded = [_value_bytes(value) for value in values]
        if not encoded:
            self._values.pop(self._key(name), None)
            return
        self._values[self._key(name)] = (name, encoded)

    def appendlist(self, name: str, value: HeaderValue) -> None:
        key = self._key(name)
        if key not in self._values:
            self._values[key] = (name, [])
        self._values[key][1].append(_value_bytes(value))

    def to_http_pairs(self) -> list[tuple[str, str]]:
        return [
            (name, value.decode("latin-1"))
            for name, values in self._values.values()
            for value in values
        ]

    def to_raw_pairs(self) -> list[tuple[str, bytes]]:
        return [(name, value) for name, values in self._values.values() for value in values]

    def copy(self) -> Headers:
        copied = Headers()
        for name, values in self._values.values():
            copied.setlist(name, values)
        return copied

    def __repr__(self) -> str:
        contents = {name: list(values) for name, values in self._values.values()}
        return f"Headers({contents!r})"
