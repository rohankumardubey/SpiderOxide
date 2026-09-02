from __future__ import annotations

import heapq
import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .dupefilter import PythonDupeFilter


@dataclass(frozen=True, slots=True)
class Request:
    url: str
    method: str
    body: bytes
    priority: int
    sequence: int


class PythonScheduler:
    def __init__(self) -> None:
        self._dupe_filter = PythonDupeFilter()
        self._sequence = itertools.count()
        self._queue: list[tuple[int, int, Request]] = []

    def push(
        self,
        url: str,
        method: str = "GET",
        body: bytes = b"",
        priority: int = 0,
    ) -> bool:
        if self._dupe_filter.seen(url, method, body):
            return False
        self._enqueue(url, method, body, priority)
        return True

    def push_unchecked(
        self,
        url: str,
        method: str = "GET",
        body: bytes = b"",
        priority: int = 0,
    ) -> bool:
        self._enqueue(url, method, body, priority)
        return True

    def _enqueue(self, url: str, method: str, body: bytes, priority: int) -> None:
        sequence = next(self._sequence)
        request = Request(url, method, body, priority, sequence)
        # Negating priority makes heapq return larger priorities first; sequence keeps FIFO ties.
        heapq.heappush(self._queue, (-priority, sequence, request))

    def push_batch(self, requests: Iterable[Sequence[object]]) -> list[bool]:
        return [
            self.push(
                str(request[0]),
                str(request[1]),
                bytes(request[2]),
                int(request[3]),
            )
            for request in requests
        ]

    def pop(self) -> Request | None:
        if not self._queue:
            return None
        return heapq.heappop(self._queue)[2]

    def pop_batch(self, count: int) -> list[Request]:
        if count < 0:
            raise ValueError("count must be non-negative")
        output: list[Request] = []
        for _ in range(min(count, len(self._queue))):
            output.append(heapq.heappop(self._queue)[2])
        return output

    def __len__(self) -> int:
        return len(self._queue)
