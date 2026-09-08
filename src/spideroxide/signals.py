from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from collections.abc import Callable

from .utils import maybe_await

logger = logging.getLogger(__name__)

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
memusage_warning_reached = "memusage_warning_reached"
feed_slot_closed = "feed_slot_closed"
feed_exporter_closed = "feed_exporter_closed"


def _accepted_kwargs(
    receiver: Callable[..., object],
    kwargs: dict[str, object],
) -> dict[str, object]:
    parameters = inspect.signature(receiver).parameters.values()
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return kwargs
    accepted_names = {
        parameter.name
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return {name: value for name, value in kwargs.items() if name in accepted_names}


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
            try:
                response = await maybe_await(
                    receiver(**_accepted_kwargs(receiver, kwargs))
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception(
                    "Error caught on signal handler: %s",
                    receiver,
                )
                response = error
            responses.append((receiver, response))
        return responses
