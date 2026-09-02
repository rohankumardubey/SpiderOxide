from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, MutableMapping

HeaderName = str | bytes
HeaderValue = str | bytes | int | float
HeaderValues = HeaderValue | Iterable[HeaderValue] | None


def _name_text(name: HeaderName) -> str:
    if isinstance(name, bytes):
        return name.decode("latin-1")
    if not isinstance(name, str):
        raise TypeError("header names must be strings or bytes")
    return name


def _value_bytes(value: HeaderValue, encoding: str) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value).encode(encoding)


def _normalize_values(value: HeaderValues, encoding: str) -> list[bytes]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, int, float)):
        return [_value_bytes(value, encoding)]
    return [_value_bytes(item, encoding) for item in value]


class Headers(MutableMapping[str, bytes]):
    """Case-insensitive HTTP headers with support for repeated values."""

    def __init__(
        self,
        values: Mapping[HeaderName, HeaderValues]
        | Iterable[tuple[HeaderName, HeaderValues]]
        | None = None,
        *,
        encoding: str = "latin-1",
    ) -> None:
        self.encoding = encoding
        self._values: dict[str, tuple[str, list[bytes]]] = {}
        if isinstance(values, Headers):
            for name, entries in values._values.values():
                self.setlist(name, entries)
        elif values:
            entries = values.items() if isinstance(values, Mapping) else values
            for name, value in entries:
                self.appendlist(name, value)

    @staticmethod
    def _key(name: HeaderName) -> str:
        return _name_text(name).lower()

    def __getitem__(self, name: HeaderName) -> bytes:
        values = self._values[self._key(name)][1]
        return values[-1]

    def __setitem__(self, name: HeaderName, value: HeaderValues) -> None:
        self.setlist(name, value)

    def __delitem__(self, name: HeaderName) -> None:
        del self._values[self._key(name)]

    def __iter__(self) -> Iterator[str]:
        return (display_name for display_name, _ in self._values.values())

    def __len__(self) -> int:
        return len(self._values)

    def getlist(self, name: HeaderName) -> list[bytes]:
        entry = self._values.get(self._key(name))
        return [] if entry is None else list(entry[1])

    def setlist(self, name: HeaderName, values: HeaderValues) -> None:
        display_name = _name_text(name)
        encoded = _normalize_values(values, self.encoding)
        if not encoded:
            self._values.pop(self._key(name), None)
            return
        self._values[self._key(name)] = (display_name, encoded)

    def appendlist(self, name: HeaderName, value: HeaderValues) -> None:
        encoded = _normalize_values(value, self.encoding)
        if not encoded:
            return
        key = self._key(name)
        if key not in self._values:
            self._values[key] = (_name_text(name), [])
        self._values[key][1].extend(encoded)

    def to_scrapy_dict(self) -> dict[bytes, list[bytes]]:
        return {name.encode("latin-1"): list(values) for name, values in self._values.values()}

    def to_http_pairs(self) -> list[tuple[str, str]]:
        return [
            (name, value.decode("latin-1"))
            for name, values in self._values.values()
            for value in values
        ]

    def to_raw_pairs(self) -> list[tuple[str, bytes]]:
        return [(name, value) for name, values in self._values.values() for value in values]

    def copy(self) -> Headers:
        copied = Headers(encoding=self.encoding)
        for name, values in self._values.values():
            copied.setlist(name, values)
        return copied

    def __repr__(self) -> str:
        contents = {name: list(values) for name, values in self._values.values()}
        return f"Headers({contents!r})"
