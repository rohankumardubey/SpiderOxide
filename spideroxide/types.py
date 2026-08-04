from __future__ import annotations

from typing import Protocol

RequestData = tuple[str, str, bytes, int]


class FingerprintRequest(Protocol):
    url: str
    method: str
    body: bytes


class PriorityRequest(FingerprintRequest, Protocol):
    priority: int


class ScheduledRequest(PriorityRequest, Protocol):
    pass


def request_data(request: PriorityRequest) -> RequestData:
    return (request.url, request.method, bytes(request.body), request.priority)
