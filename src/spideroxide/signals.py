from __future__ import annotations

import asyncio
import inspect
import logging
import weakref
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .exceptions import StopDownload
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
    try:
        parameters = inspect.signature(receiver).parameters.values()
    except (TypeError, ValueError):
        return kwargs
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return kwargs
    accepted_names = {
        parameter.name
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return {name: value for name, value in kwargs.items() if name in accepted_names}


def _ignored_exceptions(
    dont_log: type[BaseException] | Sequence[type[BaseException]] | None,
) -> tuple[type[BaseException], ...]:
    if dont_log is None:
        return (StopDownload,)
    if isinstance(dont_log, Sequence):
        return (*dont_log, StopDownload)
    return (dont_log, StopDownload)


def _twisted_failure(error: Exception) -> object:
    try:
        from twisted.python.failure import Failure
    except ImportError as missing:
        raise RuntimeError(
            "Scrapy-style synchronous signal failures require spideroxide[twisted]"
        ) from missing
    return Failure(error, type(error), error.__traceback__)


class SignalManager:
    def __init__(self, sender: object | None = None) -> None:
        self.sender = self if sender is None else sender
        self._receivers: dict[object, list[Callable[..., object]]] = defaultdict(list)
        self._weak_receivers: dict[object, list[weakref.ReferenceType[Callable[..., object]]]] = (
            defaultdict(list)
        )
        self._scoped_receivers: dict[object, list[tuple[object, Callable[..., object]]]] = (
            defaultdict(list)
        )
        self._weak_scoped_receivers: dict[
            object, list[tuple[object, weakref.ReferenceType[Callable[..., object]]]]
        ] = defaultdict(list)

    def connect(
        self,
        receiver: Callable[..., object],
        signal: object,
        *,
        sender: object | None = None,
        weak: bool = False,
    ) -> None:
        actual_sender = self.sender if sender is None else sender
        if receiver in self._matching_receivers(signal, actual_sender):
            return
        if weak:
            reference: weakref.ReferenceType[Callable[..., object]]
            if inspect.ismethod(receiver):
                reference = weakref.WeakMethod(receiver)
            else:
                reference = weakref.ref(receiver)
            if actual_sender is self.sender:
                self._weak_receivers[signal].append(reference)
            else:
                self._weak_scoped_receivers[signal].append((actual_sender, reference))
        elif actual_sender is self.sender:
            self._receivers[signal].append(receiver)
        else:
            self._scoped_receivers[signal].append((actual_sender, receiver))

    def disconnect(
        self, receiver: Callable[..., object], signal: object, *, sender: object | None = None
    ) -> bool:
        if sender is None or sender is self.sender:
            receivers = self._receivers.get(signal, [])
            if receiver in receivers:
                receivers.remove(receiver)
                return True
            weak_receivers = self._weak_receivers.get(signal, [])
            for index, reference in enumerate(weak_receivers):
                if reference() == receiver:
                    del weak_receivers[index]
                    return True
            return False
        scoped = self._scoped_receivers.get(signal, [])
        for index, (existing, candidate) in enumerate(scoped):
            if existing is sender and candidate == receiver:
                del scoped[index]
                return True
        weak_scoped = self._weak_scoped_receivers.get(signal, [])
        for index, (existing, reference) in enumerate(weak_scoped):
            if existing is sender and reference() == receiver:
                del weak_scoped[index]
                return True
        return False

    def disconnect_all(self, signal: object, *, sender: object | None = None) -> None:
        if sender is None or sender is self.sender:
            self._receivers.pop(signal, None)
            self._weak_receivers.pop(signal, None)
        else:
            self._scoped_receivers[signal] = [
                (existing, receiver)
                for existing, receiver in self._scoped_receivers.get(signal, ())
                if existing is not sender
            ]
            self._weak_scoped_receivers[signal] = [
                (existing, reference)
                for existing, reference in self._weak_scoped_receivers.get(signal, ())
                if existing is not sender
            ]

    def _matching_receivers(
        self, signal: object, sender: object
    ) -> tuple[Callable[..., object], ...]:
        receivers = tuple(self._receivers.get(signal, ())) if sender is self.sender else ()
        weak_receivers = self._weak_receivers.get(signal, ())
        alive_weak = [reference for reference in weak_receivers if reference() is not None]
        if len(alive_weak) != len(weak_receivers):
            self._weak_receivers[signal] = alive_weak
        weak_scoped = self._weak_scoped_receivers.get(signal, ())
        alive_scoped = [
            (existing, reference) for existing, reference in weak_scoped if reference() is not None
        ]
        if len(alive_scoped) != len(weak_scoped):
            self._weak_scoped_receivers[signal] = alive_scoped
        scoped = tuple(
            receiver
            for existing, receiver in self._scoped_receivers.get(signal, ())
            if existing is sender
        )
        return (
            receivers
            + tuple(reference() for reference in alive_weak if sender is self.sender)
            + scoped
            + tuple(reference() for existing, reference in alive_scoped if existing is sender)
        )

    async def wait_for(self, signal: object) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def handle() -> None:
            if not future.done():
                future.set_result(None)

        self.connect(handle, signal)
        try:
            await future
        finally:
            self.disconnect(handle, signal)

    async def send(
        self, signal: object, **kwargs: object
    ) -> list[tuple[Callable[..., object], object | SignalFailure]]:
        return await self._send_catch_log_awaitable(signal, **kwargs)

    async def _send_catch_log_awaitable(
        self,
        signal: object,
        *,
        dont_log: type[BaseException] | Sequence[type[BaseException]] | None = (),
        sender: object | None = None,
        **kwargs: object,
    ) -> list[tuple[Callable[..., object], object | SignalFailure]]:
        actual_sender = self.sender if sender is None else sender
        named = {**kwargs, "signal": signal, "sender": actual_sender}
        ignored = _ignored_exceptions(dont_log)
        responses: list[tuple[Callable[..., object], object | SignalFailure]] = []
        for receiver in self._matching_receivers(signal, actual_sender):
            try:
                response = await maybe_await(receiver(**_accepted_kwargs(receiver, named)))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not isinstance(error, ignored):
                    logger.exception(
                        "Error caught on signal handler: %s",
                        receiver,
                    )
                response = SignalFailure(error)
            responses.append((receiver, response))
        return responses

    def send_catch_log(
        self,
        signal: object,
        *,
        dont_log: type[BaseException] | Sequence[type[BaseException]] | None = (),
        sender: object | None = None,
        **kwargs: object,
    ) -> list[tuple[Callable[..., object], object]]:
        return self._send_sync(
            signal, dont_log=dont_log, sender=sender, failure=_twisted_failure, **kwargs
        )

    async def send_catch_log_async(
        self,
        signal: object,
        *,
        dont_log: type[BaseException] | Sequence[type[BaseException]] | None = (),
        sender: object | None = None,
        **kwargs: object,
    ) -> list[tuple[Callable[..., object], object]]:
        actual_sender = self.sender if sender is None else sender
        named = {**kwargs, "signal": signal, "sender": actual_sender}
        ignored = _ignored_exceptions(dont_log)

        async def invoke(receiver: Callable[..., object]) -> tuple[Callable[..., object], object]:
            try:
                result = await maybe_await(receiver(**_accepted_kwargs(receiver, named)))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not isinstance(error, ignored):
                    logger.exception("Error caught on signal handler: %s", receiver)
                result = error
            return receiver, result

        return list(
            await asyncio.gather(
                *(invoke(receiver) for receiver in self._matching_receivers(signal, actual_sender))
            )
        )

    def send_sync(
        self,
        signal: object,
        *,
        dont_log: type[BaseException] | Sequence[type[BaseException]] | None = (),
        sender: object | None = None,
        **kwargs: object,
    ) -> list[tuple[Callable[..., object], object | SignalFailure]]:
        return self._send_sync(
            signal, dont_log=dont_log, sender=sender, failure=SignalFailure, **kwargs
        )

    def _send_sync(
        self,
        signal: object,
        *,
        dont_log: type[BaseException] | Sequence[type[BaseException]] | None,
        sender: object | None,
        failure: Callable[[Exception], object],
        **kwargs: object,
    ) -> list[tuple[Callable[..., object], object]]:
        actual_sender = self.sender if sender is None else sender
        named = {**kwargs, "signal": signal, "sender": actual_sender}
        ignored = _ignored_exceptions(dont_log)
        responses: list[tuple[Callable[..., object], object]] = []
        for receiver in self._matching_receivers(signal, actual_sender):
            try:
                response = receiver(**_accepted_kwargs(receiver, named))
                if inspect.isawaitable(response):
                    close = getattr(response, "close", None)
                    if close is not None:
                        close()
                    raise TypeError(f"signal {signal!r} does not support asynchronous handlers")
            except Exception as error:
                if not isinstance(error, ignored):
                    logger.exception(
                        "Error caught on signal handler: %s",
                        receiver,
                    )
                response = failure(error)
            responses.append((receiver, response))
        return responses
