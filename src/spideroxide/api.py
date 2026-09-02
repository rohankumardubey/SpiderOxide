from __future__ import annotations

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
        self._original_requests: dict[int, PriorityRequest] = {}
        self._next_sequence = 0

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
        inserted = bool(self._implementation.push(url, method, body, priority))
        self._record_sequences((inserted,))
        return inserted

    def push_request(self, request: PriorityRequest) -> bool:
        data = request_data(request)
        inserted = bool(
            self._implementation.push_unchecked(*data)
            if _bypass_duplicate_filter(request)
            else self._implementation.push(*data)
        )
        sequence = self._record_sequences((inserted,))[0]
        if sequence is not None:
            self._original_requests[sequence] = request
        return inserted

    def push_batch(self, requests: Iterable[Sequence[object]]) -> list[bool]:
        inserted = list(self._implementation.push_batch(list(requests)))
        self._record_sequences(inserted)
        return inserted

    def push_requests(self, requests: Iterable[PriorityRequest]) -> list[bool]:
        originals = list(requests)
        if any(_bypass_duplicate_filter(request) for request in originals):
            return [self.push_request(request) for request in originals]
        data = [request_data(request) for request in originals]
        inserted = list(self._implementation.push_batch(data))
        sequences = self._record_sequences(inserted)
        for request, sequence in zip(originals, sequences, strict=True):
            if sequence is not None:
                self._original_requests[sequence] = request
        return inserted

    def _record_sequences(self, inserted: Iterable[bool]) -> list[int | None]:
        sequences = []
        for accepted in inserted:
            if not accepted:
                sequences.append(None)
                continue
            sequences.append(self._next_sequence)
            self._next_sequence += 1
        return sequences

    def _restore_request(self, request: ScheduledRequest) -> ScheduledRequest:
        sequence = getattr(request, "sequence", None)
        if not isinstance(sequence, int):
            return request
        return self._original_requests.pop(sequence, request)

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
