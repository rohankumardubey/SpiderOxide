"""Thin Python defaults around the native extension."""

from ._native import Request, RustScheduler
from ._native import RustDupeFilter as _NativeDupeFilter
from ._native import fingerprint as _fingerprint
from ._native import fingerprint_batch as _fingerprint_batch


def _native_requests(requests: object) -> list[tuple[str, str, bytes, int]]:
    return [
        (
            str(request[0]),
            str(request[1]),
            bytes(request[2]),
            int(request[3]) if len(request) > 3 else 0,
        )
        for request in requests
    ]


def fingerprint(url: str, method: str = "GET", body: bytes = b"") -> bytes:
    return _fingerprint(url, method, body)


def fingerprint_batch(requests: object) -> list[bytes]:
    return _fingerprint_batch(_native_requests(requests))


class RustDupeFilter:
    def __init__(self) -> None:
        self._implementation = _NativeDupeFilter()

    def seen(self, url: str, method: str = "GET", body: bytes = b"") -> bool:
        return bool(self._implementation.seen(url, method, body))

    def seen_batch(self, requests: object) -> list[bool]:
        return list(self._implementation.seen_batch(_native_requests(requests)))

    def __len__(self) -> int:
        return int(len(self._implementation))


__all__ = [
    "Request",
    "RustDupeFilter",
    "RustScheduler",
    "fingerprint",
    "fingerprint_batch",
]
