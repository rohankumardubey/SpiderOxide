"""Asyncio-native counterparts to Scrapy's event-loop utilities."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable
from typing import TypeVar

_T = TypeVar("_T")

logger = logging.getLogger(__name__)


async def collect_asyncgen(result: AsyncIterator[_T]) -> list[_T]:
    return [item async for item in result]


async def as_async_generator(
    it: Iterable[_T] | AsyncIterator[_T],
) -> AsyncGenerator[_T, None]:
    if isinstance(it, AsyncIterator):
        async for item in it:
            yield item
    else:
        for item in it:
            yield item


def is_asyncio_available() -> bool:
    """Return True inside SpiderOxide's asyncio loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError as error:
        raise RuntimeError(
            "is_asyncio_available() called without an installed reactor or running asyncio loop."
        ) from error
    return True


class AsyncioLoopingCall:
    """Call a synchronous function on a fixed-period asyncio schedule."""

    def __init__(self, func: Callable[..., object], *args: object, **kwargs: object) -> None:
        self._func = func
        self._args = args
        self._kwargs = kwargs
        self._task: asyncio.Task[None] | None = None
        self.interval: float | None = None
        self._start_time: float | None = None

    @property
    def running(self) -> bool:
        return self._start_time is not None

    def start(self, interval: float, now: bool = True) -> None:
        if self.running:
            raise RuntimeError("AsyncioLoopingCall already running")
        if interval <= 0:
            raise ValueError("Interval must be greater than 0")
        loop = asyncio.get_running_loop()
        self.interval = interval
        self._start_time = time.monotonic()
        if now:
            self._call()
        if self.running:
            self._task = loop.create_task(self._loop())

    def _to_sleep(self) -> float:
        assert self.interval is not None and self._start_time is not None
        elapsed = time.monotonic() - self._start_time
        return self.interval - (elapsed % self.interval)

    async def _loop(self) -> None:
        try:
            while self.running:
                await asyncio.sleep(self._to_sleep())
                if self.running:
                    try:
                        self._call()
                    except TypeError:
                        logger.exception("Error calling the AsyncioLoopingCall function")
        finally:
            if self._task is asyncio.current_task():
                self._task = None

    def stop(self) -> None:
        self.interval = self._start_time = None
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _call(self) -> None:
        try:
            result = self._func(*self._args, **self._kwargs)
        except Exception:
            logger.exception("Error calling the AsyncioLoopingCall function")
            self.stop()
            return
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            self.stop()
            raise TypeError("The AsyncioLoopingCall function must not return an awaitable")


def create_looping_call(
    func: Callable[..., object], *args: object, **kwargs: object
) -> AsyncioLoopingCall:
    return AsyncioLoopingCall(func, *args, **kwargs)


class CallLaterResult:
    """A cancelable asyncio delayed-call handle."""

    def __init__(self) -> None:
        self._timer_handle: asyncio.TimerHandle | None = None

    @classmethod
    def from_asyncio(cls, timer_handle: asyncio.TimerHandle) -> CallLaterResult:
        result = cls()
        result._timer_handle = timer_handle
        return result

    def cancel(self) -> None:
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None


def call_later(delay: float, func: Callable[..., object], *args: object) -> CallLaterResult:
    return CallLaterResult.from_asyncio(asyncio.get_running_loop().call_later(delay, func, *args))


async def sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def run_in_thread(func: Callable[..., _T], *args: object, **kwargs: object) -> _T:
    return await asyncio.to_thread(func, *args, **kwargs)
