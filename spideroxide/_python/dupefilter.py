from __future__ import annotations

from collections.abc import Iterable, Sequence

from .fingerprint import fingerprint


class PythonDupeFilter:
    def __init__(self) -> None:
        self._fingerprints: set[bytes] = set()

    def seen(
        self,
        url: str,
        method: str = "GET",
        body: bytes = b"",
    ) -> bool:
        request_fingerprint = fingerprint(url, method, body)
        if request_fingerprint in self._fingerprints:
            return True
        self._fingerprints.add(request_fingerprint)
        return False

    def seen_batch(self, requests: Iterable[Sequence[object]]) -> list[bool]:
        return [
            self.seen(str(request[0]), str(request[1]), bytes(request[2])) for request in requests
        ]

    def __len__(self) -> int:
        return len(self._fingerprints)
