from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence

from .backend import BackendChoice, BackendImplementation, resolve_backend
from .types import FingerprintRequest, PriorityRequest, ScheduledRequest, request_data


def _bypass_duplicate_filter(request: object) -> bool:
    try:
        return bool(request.dont_filter)  # type: ignore[attr-defined]
    except AttributeError:
        return False


def fingerprint(
    url: str,
    method: str = "GET",
    body: bytes = b"",
    *,
    backend: BackendChoice | str | None = None,
) -> bytes:
    implementation = resolve_backend(backend)
    return implementation.fingerprint(url, method, body)


def fingerprint_request(
    request: FingerprintRequest,
    *,
    backend: BackendChoice | str | None = None,
) -> bytes:
    return fingerprint(
        request.url,
        request.method,
        bytes(request.body),
        backend=backend,
    )


def fingerprint_batch(
    requests: Iterable[Sequence[object]],
    *,
    backend: BackendChoice | str | None = None,
) -> list[bytes]:
    implementation = resolve_backend(backend)
    materialized = list(requests)
    return implementation.fingerprint_batch(materialized)


def fingerprint_requests(
    requests: Iterable[PriorityRequest],
    *,
    backend: BackendChoice | str | None = None,
) -> list[bytes]:
    return fingerprint_batch(
        (request_data(request) for request in requests),
        backend=backend,
    )


class DupeFilter:
    def __init__(self, backend: BackendChoice | str | None = None) -> None:
        self._backend: BackendImplementation = resolve_backend(backend)
        self._implementation = self._backend.dupe_filter_type()

    @property
    def backend_name(self) -> str:
        return self._backend.name

    def seen(self, url: str, method: str = "GET", body: bytes = b"") -> bool:
        return bool(self._implementation.seen(url, method, body))

    def seen_request(self, request: FingerprintRequest) -> bool:
        return self.seen(request.url, request.method, bytes(request.body))

    def seen_batch(self, requests: Iterable[Sequence[object]]) -> list[bool]:
        return list(self._implementation.seen_batch(list(requests)))

    def seen_requests(self, requests: Iterable[PriorityRequest]) -> list[bool]:
        return self.seen_batch(request_data(request) for request in requests)

    def __len__(self) -> int:
        return int(len(self._implementation))


class Scheduler:
    def __init__(self, backend: BackendChoice | str | None = None) -> None:
        self._backend: BackendImplementation = resolve_backend(backend)
        self._implementation = self._backend.scheduler_type()
        self._original_requests: dict[tuple[bytes, int], deque[PriorityRequest]] = {}

    @property
    def backend_name(self) -> str:
        return self._backend.name

    def push(
        self,
        url: str,
        method: str = "GET",
        body: bytes = b"",
        priority: int = 0,
    ) -> bool:
        return bool(self._implementation.push(url, method, body, priority))

    def push_request(self, request: PriorityRequest) -> bool:
        data = request_data(request)
        inserted = (
            bool(self._implementation.push_unchecked(*data))
            if _bypass_duplicate_filter(request)
            else self.push(*data)
        )
        if inserted:
            request_fingerprint = self._backend.fingerprint(data[0], data[1], data[2])
            key = (request_fingerprint, request.priority)
            self._original_requests.setdefault(key, deque()).append(request)
        return inserted

    def push_batch(self, requests: Iterable[Sequence[object]]) -> list[bool]:
        return list(self._implementation.push_batch(list(requests)))

    def push_requests(self, requests: Iterable[PriorityRequest]) -> list[bool]:
        originals = list(requests)
        if any(_bypass_duplicate_filter(request) for request in originals):
            return [self.push_request(request) for request in originals]
        data = [request_data(request) for request in originals]
        inserted = self.push_batch(data)
        fingerprints = self._backend.fingerprint_batch(data)
        for request, request_fingerprint, accepted in zip(
            originals, fingerprints, inserted, strict=True
        ):
            if accepted:
                key = (request_fingerprint, request.priority)
                self._original_requests.setdefault(key, deque()).append(request)
        return inserted

    def _restore_request(self, request: ScheduledRequest) -> ScheduledRequest:
        request_fingerprint = self._backend.fingerprint(
            request.url,
            request.method,
            bytes(request.body),
        )
        key = (request_fingerprint, request.priority)
        originals = self._original_requests.get(key)
        if not originals:
            return request
        original = originals.popleft()
        if not originals:
            del self._original_requests[key]
        return original

    def pop(self) -> ScheduledRequest | None:
        request = self._implementation.pop()
        if request is None:
            return None
        return self._restore_request(request)

    def pop_batch(self, count: int) -> list[ScheduledRequest]:
        if count < 0:
            raise ValueError("count must be non-negative")
        requests = list(self._implementation.pop_batch(count))
        return [self._restore_request(request) for request in requests]

    def __len__(self) -> int:
        return int(len(self._implementation))
