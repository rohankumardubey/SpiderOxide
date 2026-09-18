from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from .utils import maybe_await

logger = logging.getLogger(__name__)

engine_started = "engine_started"
engine_stopped = "engine_stopped"
scheduler_empty = "scheduler_empty"
spider_opened = "spider_opened"
spider_idle = "spider_idle"
spider_closed = "spider_closed"
request_scheduled = "request_scheduled"
request_dropped = "request_dropped"
request_reached_downloader = "request_reached_downloader"
request_left_downloader = "request_left_downloader"
response_received = "response_received"
response_downloaded = "response_downloaded"
headers_received = "headers_received"
bytes_received = "bytes_received"
robots_parsed = "robots_parsed"
item_scraped = "item_scraped"
item_dropped = "item_dropped"
item_error = "item_error"
spider_error = "spider_error"
memusage_warning_reached = "memusage_warning_reached"
feed_slot_closed = "feed_slot_closed"
feed_exporter_closed = "feed_exporter_closed"


@dataclass(frozen=True, slots=True)
class SignalFailure:
    exception: Exception


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
    ) -> list[tuple[Callable[..., object], object | SignalFailure]]:
        return await self.send_catch_log(signal, **kwargs)

    async def send_catch_log(
        self,
        signal: str,
        *,
        dont_log: type[BaseException] | tuple[type[BaseException], ...] = (),
        **kwargs: object,
    ) -> list[tuple[Callable[..., object], object | SignalFailure]]:
        responses = []
        for receiver in tuple(self._receivers.get(signal, ())):
            try:
                response = await maybe_await(receiver(**_accepted_kwargs(receiver, kwargs)))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not isinstance(error, dont_log):
                    logger.exception(
                        "Error caught on signal handler: %s",
                        receiver,
                    )
                response = SignalFailure(error)
            responses.append((receiver, response))
        return responses

    def send_sync(
        self,
        signal: str,
        *,
        dont_log: type[BaseException] | tuple[type[BaseException], ...] = (),
        **kwargs: object,
    ) -> list[tuple[Callable[..., object], object | SignalFailure]]:
        responses = []
        for receiver in tuple(self._receivers.get(signal, ())):
            try:
                response = receiver(**_accepted_kwargs(receiver, kwargs))
                if inspect.isawaitable(response):
                    close = getattr(response, "close", None)
                    if close is not None:
                        close()
                    raise TypeError(f"signal {signal!r} does not support asynchronous handlers")
            except Exception as error:
                if not isinstance(error, dont_log):
                    logger.exception(
                        "Error caught on signal handler: %s",
                        receiver,
                    )
                response = SignalFailure(error)
            responses.append((receiver, response))
        return responses
