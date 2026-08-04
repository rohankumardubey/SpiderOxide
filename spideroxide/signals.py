from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from .utils import maybe_await

engine_started = "engine_started"
engine_stopped = "engine_stopped"
spider_opened = "spider_opened"
spider_closed = "spider_closed"
request_scheduled = "request_scheduled"
request_dropped = "request_dropped"
response_received = "response_received"
item_scraped = "item_scraped"
item_dropped = "item_dropped"
spider_error = "spider_error"


class SignalManager:
    def __init__(self) -> None:
        self._receivers: dict[str, list[Callable[..., object]]] = defaultdict(list)

    def connect(self, receiver: Callable[..., object], signal: str) -> None:
        if receiver not in self._receivers[signal]:
            self._receivers[signal].append(receiver)

    def disconnect(self, receiver: Callable[..., object], signal: str) -> bool:
        receivers = self._receivers.get(signal, [])
        if receiver not in receivers:
            return False
        receivers.remove(receiver)
        return True

    async def send(
        self, signal: str, **kwargs: object
    ) -> list[tuple[Callable[..., object], object]]:
        responses = []
        for receiver in tuple(self._receivers.get(signal, ())):
            response = await maybe_await(receiver(**kwargs))
            responses.append((receiver, response))
        return responses
