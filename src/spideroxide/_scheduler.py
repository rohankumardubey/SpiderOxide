from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from typing import Literal

from .api import DupeFilter
from .http import Request
from .settings import Settings

QueueOrder = Literal["fifo", "lifo"]

_MEMORY_QUEUE_ORDERS: dict[str, QueueOrder] = {
    "scrapy.squeues.FifoMemoryQueue": "fifo",
    "scrapy.squeues.LifoMemoryQueue": "lifo",
}
_DISK_QUEUE_ORDERS: dict[str, QueueOrder] = {
    "scrapy.squeues.MarshalFifoDiskQueue": "fifo",
    "scrapy.squeues.MarshalLifoDiskQueue": "lifo",
    "scrapy.squeues.PickleFifoDiskQueue": "fifo",
    "scrapy.squeues.PickleLifoDiskQueue": "lifo",
}


def _resolve_queue_order(
    settings: Settings,
    name: str,
    supported: dict[str, QueueOrder],
    *,
    optional: bool = False,
) -> QueueOrder | None:
    value = settings.get(name)
    if value is None and optional:
        return None
    if not isinstance(value, str) or value not in supported:
        choices = ", ".join(sorted(supported))
        suffix = " or None" if optional else ""
        raise ValueError(f"unsupported {name} value {value!r}; expected one of {choices}{suffix}")
    return supported[value]


@dataclass(frozen=True, slots=True)
class SchedulerQueueConfig:
    memory: QueueOrder
    disk: QueueOrder
    start_memory: QueueOrder | None
    start_disk: QueueOrder | None

    @classmethod
    def from_settings(cls, settings: Settings) -> SchedulerQueueConfig:
        return cls(
            memory=_resolve_queue_order(
                settings,
                "SCHEDULER_MEMORY_QUEUE",
                _MEMORY_QUEUE_ORDERS,
            ),
            disk=_resolve_queue_order(
                settings,
                "SCHEDULER_DISK_QUEUE",
                _DISK_QUEUE_ORDERS,
            ),
            start_memory=_resolve_queue_order(
                settings,
                "SCHEDULER_START_MEMORY_QUEUE",
                _MEMORY_QUEUE_ORDERS,
                optional=True,
            ),
            start_disk=_resolve_queue_order(
                settings,
                "SCHEDULER_START_DISK_QUEUE",
                _DISK_QUEUE_ORDERS,
                optional=True,
            ),
        )


class EngineScheduler:
    def __init__(self, config: SchedulerQueueConfig) -> None:
        self._config = config
        self._dupe_filter = DupeFilter()
        self._sequence = itertools.count()
        self._normal: list[tuple[int, int, int, Request]] = []
        self._start: list[tuple[int, int, int, Request]] = []

    @staticmethod
    def _tie(sequence: int, order: QueueOrder) -> int:
        return sequence if order == "fifo" else -sequence

    def push_request(self, request: Request) -> bool:
        if not request.dont_filter and self._dupe_filter.seen_request(request):
            return False
        sequence = next(self._sequence)
        is_start_request = bool(request.meta.get("is_start_request", False))
        use_start_queue = is_start_request and self._config.start_memory is not None
        order = self._config.start_memory if use_start_queue else self._config.memory
        assert order is not None
        entry = (-request.priority, self._tie(sequence, order), sequence, request)
        heapq.heappush(self._start if use_start_queue else self._normal, entry)
        return True

    def pop(self) -> Request | None:
        if self._normal and (not self._start or self._normal[0][0] <= self._start[0][0]):
            return heapq.heappop(self._normal)[3]
        if self._start:
            return heapq.heappop(self._start)[3]
        return None

    def __len__(self) -> int:
        return len(self._normal) + len(self._start)
